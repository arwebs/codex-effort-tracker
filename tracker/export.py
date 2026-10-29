"""Rebuildable CSV views; direct metrics never include inherited child totals."""
import csv
import json
import os
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from .ingest import TOKEN_FIELDS
from .metrics import iso, timing, union_seconds, merge_intervals


def local_iso(value):
    if value is None:
        return ''
    # Windows Python installations do not necessarily contain IANA tzdata.
    # The host is America/New_York; astimezone uses Windows' DST-aware rules.
    return datetime.fromtimestamp(value,timezone.utc).astimezone().isoformat(timespec='milliseconds')


def token_sums(events):
    records=[e['data'] for e in events if e['kind']=='token']
    return {key:sum(r[key] for r in records) if records and all(isinstance(r.get(key),(int,float)) for r in records) else None for key in TOKEN_FIELDS}


def kind(meta):
    source=meta.get('source',{})
    if isinstance(source,str):
        try:
            source=json.loads(source)
        except (ValueError,TypeError):
            source={}
    subagent=source.get('subagent') if isinstance(source,dict) else None
    if isinstance(subagent,dict) and subagent.get('other')=='guardian':
        return 'internal_approval'
    return 'task_agent' if meta.get('parent_thread_id') else 'root'


def build_rows(store, now=None, session_gap=1800):
    now=time.time() if now is None else now
    metadata=store.all_threads()
    by_thread=defaultdict(list)
    for e in store.events():
        by_thread[e['thread_id']].append(e)
    thread_rows=[]
    turn_rows=[]
    intervals={}
    human=defaultdict(lambda:defaultdict(float))
    qualities=defaultdict(set)
    for r in store.db.execute('SELECT thread_id,category,quality,SUM(end-start) AS seconds FROM activity GROUP BY thread_id,category,quality'):
        human[r['thread_id']][r['category']]+=r['seconds']
        qualities[r['thread_id']].add(r['quality'])
    for tid,meta in metadata.items():
        events=by_thread[tid]
        turn_events=defaultdict(list)
        for e in events:
            if e['turn_id']:
                turn_events[e['turn_id']].append(e)
        spans=[]
        confirmed_ends=set()
        this_turns=[]
        flags=set()
        for turn_id,items in turn_events.items():
            starts=[e for e in items if e['kind']=='turn_start']
            ends=[e for e in items if e['kind']=='turn_end']
            if not starts and not ends:
                continue
            start=starts[0]['ts'] if starts else None
            end=ends[-1]['ts'] if ends else None
            status=ends[-1]['data'].get('status','unknown') if ends else 'incomplete'
            if ends:
                duration=ends[-1]['data'].get('duration_ms')
                if isinstance(duration,(int,float)) and duration>=0 and end is not None:
                    start=end-duration/1000
                elif start is None:
                    start=ends[-1]['data'].get('started_at')
            observed_end=end
            if end is not None:
                confirmed_ends.add(end)
            if end is None:
                observed_end=max((e['ts'] for e in items if e['ts'] is not None),default=None)
                flags.add('open_turn_lower_bound')
            if start is not None and observed_end is not None and observed_end>=start:
                spans.append((start,observed_end))
            settings=sorted({(e['data'].get('model') or 'unknown',e['data'].get('effort') or 'unknown') for e in items if e['kind']=='context'})
            row=dict(schema_version=1,exported_at=iso(now),thread_id=tid,turn_id=turn_id,
                     root_turn_id=starts[0]['data'].get('root_turn_id') if starts else None,status=status,
                     started_at=iso(start),completed_at=iso(end),observed_through_at=iso(observed_end),
                     started_at_local=local_iso(start),completed_at_local=local_iso(end),
                     active_seconds=(observed_end-start) if start is not None and observed_end is not None and observed_end>=start else None,
                     model=';'.join(sorted({s[0] for s in settings})),effort=';'.join(sorted({s[1] for s in settings})),
                     model_effort_combinations=json.dumps(settings),messages_sent=sum(e['kind']=='user_message' for e in items),
                     messages_received=sum(e['kind']=='assistant_message' for e in items),
                     nonhuman_messages=sum(e['kind']=='nonhuman_message' for e in items),
                     unclassified_messages=sum(e['kind']=='unclassified_message' for e in items),
                     chat_words=sum(e['data'].get('words',0) for e in items if e['kind']=='assistant_message'),
                     compaction_count=sum(e['kind']=='compact' for e in items),
                     coverage='recorded' if ends else 'open_turn_lower_bound')
            row.update(token_sums(items))
            if row['total_tokens'] is None:
                row['coverage']+=';tokens_unavailable'
            this_turns.append(row)
        intervals[tid]=spans
        values=timing(spans,session_gap)
        unknown_gap=0
        merged=merge_intervals(spans)
        for previous,current in zip(merged,merged[1:]):
            gap=current[0]-previous[1]
            if previous[1] not in confirmed_ends:
                unknown_gap+=gap
                values['idle' if gap<=session_gap else 'dormant']-=gap
        if unknown_gap:
            flags.add('unknown_gaps_after_incomplete_turns')
        if not this_turns:
            flags.add('no_local_turn_intervals')
        if meta.get('ingest_warnings'):
            flags.add('malformed_records')
        if meta.get('ownership_incomplete'):
            flags.add('history_ownership_incomplete')
        if any(e['kind']=='unclassified_message' for e in events):
            flags.add('message_provenance_incomplete')
        times=[e['ts'] for e in events if e['ts'] is not None]
        ended=[e['ts'] for e in events if e['kind']=='turn_end' and e['ts'] is not None]
        settings=sorted({(e['data'].get('model') or 'unknown',e['data'].get('effort') or 'unknown') for e in events if e['kind']=='context'})
        row=dict(schema_version=1,exported_at=iso(now),row_type='thread',thread_id=tid,
                 parent_thread_id=meta.get('parent_thread_id'),root_thread_id=tid,
                 thread_name=meta.get('name') or meta.get('title') or meta.get('agent_nickname') or '',
                 project_id=meta.get('project_id'),project_name=meta.get('project_name',''),cwd=meta.get('cwd',''),
                 agent_kind=kind(meta),agent_role=meta.get('agent_role'),agent_path=meta.get('agent_path'),
                 status=('incomplete' if this_turns and max(this_turns,key=lambda t:t['observed_through_at'])['status']=='incomplete' else ('archived' if meta.get('archived') else 'idle')),
                 first_started_at=iso(min(a for a,b in spans)) if spans else '',
                 last_activity_at=iso(max(times)) if times else '',last_completed_at=iso(max(ended)) if ended else '',
                 observed_through_at=iso(meta.get('last_record_at')),
                 work_session_count=values['sessions'],agent_active_seconds=values['active'] if spans else None,
                 agent_idle_seconds=values['idle'] if spans else None,inactive_gap_seconds=values['dormant'] if spans else None,
                 unknown_gap_seconds=unknown_gap,
                 agent_idle_provisional_seconds=0,
                 human_active_estimated_seconds=human[tid].get('active',0) if tid in qualities else None,
                 human_idle_estimated_seconds=human[tid].get('idle',0) if tid in qualities else None,
                 human_coverage=';'.join(sorted(qualities[tid])) or 'not_observed',
                 messages_sent=sum(e['kind']=='user_message' for e in events),messages_received=sum(e['kind']=='assistant_message' for e in events),
                 nonhuman_messages=sum(e['kind']=='nonhuman_message' for e in events),unclassified_messages=sum(e['kind']=='unclassified_message' for e in events),
                 agent_messages=sum(e['kind']=='agent_message' for e in events),turns=len(this_turns),
                 completed_turns=sum(t['status']=='completed' for t in this_turns),interrupted_turns=sum(t['status']=='interrupted' for t in this_turns),
                 failed_turns=sum(t['status']=='failed' for t in this_turns),incomplete_turns=sum(t['status']=='incomplete' for t in this_turns),
                 compaction_count=sum(e['kind']=='compact' for e in events),chat_words=sum(e['data'].get('words',0) for e in events if e['kind']=='assistant_message'),
                 model=';'.join(sorted({s[0] for s in settings})),effort=';'.join(sorted({s[1] for s in settings})),model_effort_combinations=json.dumps(settings),
                 code_lines_added=meta.get('code_lines_added'),code_lines_removed=meta.get('code_lines_removed'),
                 workspace_lines_added=meta.get('workspace_lines_added'),workspace_lines_removed=meta.get('workspace_lines_removed'),
                 code_baseline_at=iso(meta.get('code_baseline_at')),code_attribution=meta.get('code_attribution','no_baseline'),
                 coverage=';'.join(sorted(flags)) or 'recorded')
        row.update(token_sums(events))
        if row['total_tokens'] is None:
            row['coverage']+=';tokens_unavailable'
        # Live trailing idle is bounded by this observer run, and is explicitly provisional.
        observer_start=store.setting('observer_started_at')
        heartbeat=store.setting('heartbeat')
        if ended and observer_start and heartbeat and now-heartbeat<20 and max(ended)>=observer_start and row['status']=='idle':
            gap=now-max(ended)
            interruptions=store.db.execute("SELECT count(*) FROM activity WHERE end>? AND category='unavailable'",(max(ended),)).fetchone()[0]
            closed=any(e['kind']=='session_end' and e['ts']>=max(ended) for e in events)
            if 0<=gap<=session_gap and not interruptions and not closed:
                row['agent_idle_provisional_seconds']=gap
        thread_rows.append(row)
        turn_rows.extend(this_turns)
    rows_by_id={r['thread_id']:r for r in thread_rows}
    for row in thread_rows:
        tid=row['thread_id']
        chain={tid}
        parent=row['parent_thread_id']
        while parent and parent not in chain:
            chain.add(parent)
            row['root_thread_id']=parent
            parent=metadata.get(parent,{}).get('parent_thread_id')
        children=[t for t,m in metadata.items() if m.get('parent_thread_id')==tid]
        row['agents_invoked']=sum(kind(metadata[t])!='internal_approval' for t in children)
        row['internal_agents_invoked']=sum(kind(metadata[t])=='internal_approval' for t in children)
        descendants=set()
        stack=list(children)
        while stack:
            child=stack.pop()
            if child in descendants or child==tid:
                continue
            descendants.add(child)
            stack.extend(t for t,m in metadata.items() if m.get('parent_thread_id')==child)
        row['descendant_agent_active_seconds']=sum(rows_by_id[t]['agent_active_seconds'] or 0 for t in descendants)
        row['descendant_internal_active_seconds']=sum(rows_by_id[t]['agent_active_seconds'] or 0 for t in descendants if kind(metadata[t])=='internal_approval')
        row['tree_active_elapsed_seconds']=union_seconds([span for t in descendants|{tid} for span in intervals.get(t,[])])
        if any(rows_by_id[t]['agent_active_seconds'] is None or 'lower_bound' in rows_by_id[t]['coverage'] for t in descendants):
            row['coverage']+=';descendant_time_incomplete'
    if None in human:
        thread_rows.append(dict(schema_version=1,exported_at=iso(now),row_type='unassigned_host_activity',thread_id=None,
                                thread_name='Unassigned foreground host activity',human_active_estimated_seconds=human[None].get('active',0),
                                human_idle_estimated_seconds=human[None].get('idle',0),away_seconds=human[None].get('away',0),
                                unavailable_seconds=human[None].get('unavailable',0),human_coverage=';'.join(sorted(qualities[None]))))
    return thread_rows,turn_rows


def safe_cell(value):
    if value is None:
        return ''
    if isinstance(value,float):
        return round(value,3)
    if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@','\t','\r')):
        return "'"+value
    return value


def write_csv(path, rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    fields=list(dict.fromkeys(k for row in rows for k in row)) or ['schema_version','exported_at','thread_id']
    fd,temp=tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({k:safe_cell(v) for k,v in row.items()})
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def dashboard_activity(store):
    """Merge adjacent samples without bridging observation gaps or qualities."""
    groups=defaultdict(list)
    for row in store.db.execute('SELECT start,end,category,thread_id,quality FROM activity ORDER BY start'):
        groups[(row['category'],row['thread_id'],row['quality'])].append((row['start'],row['end']))
    rows=[]
    for (category,thread_id,quality),spans in groups.items():
        for start,end in merge_intervals(spans):
            rows.append(dict(started_at=iso(start),ended_at=iso(end),category=category,thread_id=thread_id,quality=quality))
    return sorted(rows,key=lambda row:row['started_at'])


def write_dashboard_data(path, threads, turns, observer=None, generated_at=None, activity=None, session_gap=1800):
    """Classic script snapshot supports direct file:// viewing without fetch."""
    path.parent.mkdir(parents=True,exist_ok=True)
    capture=generated_at or next((row.get('exported_at') for row in threads+turns if row.get('exported_at')),iso(time.time()))
    payload=dict(schema_version=1,generated_at=capture,observer=observer or {},threads=threads,turns=turns,
                 activity=activity or [],session_gap_seconds=session_gap)
    serialized=json.dumps(payload,ensure_ascii=False,separators=(',',':'),allow_nan=False)
    serialized=serialized.replace('\u2028','\\u2028').replace('\u2029','\\u2029')
    fd,temp=tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='') as stream:
            stream.write('window.CODEX_EFFORT_DATA = '+serialized+';\n')
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def export_all(store, output_dir, session_gap=1800):
    threads,turns=build_rows(store,session_gap=session_gap)
    write_csv(output_dir/'threads.csv',threads)
    write_csv(output_dir/'turns.csv',turns)
    heartbeat=store.setting('heartbeat')
    observer=dict(heartbeat=iso(heartbeat),running=bool(heartbeat and 0<=time.time()-heartbeat<20),
                  hook_events=store.db.execute('SELECT count(*) FROM hooks').fetchone()[0])
    write_dashboard_data(output_dir/'dashboard-data.js',threads,turns,observer,activity=dashboard_activity(store),session_gap=session_gap)
    return dict(threads=sum(r.get('row_type')=='thread' for r in threads),turns=len(turns))
