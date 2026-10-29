"""Incremental adapter for locally observed Codex rollout formats."""
import hashlib
import json
import math
import sqlite3
from compression import zstd
from pathlib import Path
from .metrics import epoch, iso, prose_words

TOKEN_FIELDS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens', 'reasoning_output_tokens', 'total_tokens')


def sync_metadata(store, codex_home, since):
    paths = sorted(Path(codex_home).glob('state_*.sqlite'), key=lambda p: int(p.stem.split('_')[-1]), reverse=True)
    if not paths:
        raise RuntimeError('No local Codex thread database found')
    db = sqlite3.connect(paths[0].as_uri() + '?mode=ro', uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        columns = {r[1] for r in db.execute('PRAGMA table_info(threads)')}
        wanted = ['id', 'rollout_path', 'title', 'name', 'cwd', 'model', 'reasoning_effort', 'source', 'thread_source', 'project_id', 'agent_nickname', 'agent_role', 'agent_path', 'archived', 'updated_at', 'created_at', 'cli_version', 'originator']
        fields = ','.join(k for k in wanted if k in columns)
        projects = {}
        try:
            projects = dict(db.execute('SELECT id,name FROM projects'))
        except sqlite3.OperationalError:
            pass
        # Already tracked threads stay in the catalogue even if their timestamps do not change.
        known = store.all_threads()
        found = []
        for row in db.execute('SELECT ' + fields + ' FROM threads'):
            data = dict(row)
            if data.get('updated_at', 0) < since and data['id'] not in known:
                continue
            data['project_name'] = projects.get(data.get('project_id'), '')
            store.thread(data['id'], data)
            found.append(data)
        try:
            known_ids = set(store.all_threads())
            for parent, child in db.execute('SELECT parent_thread_id,child_thread_id FROM thread_spawn_edges'):
                if child in known_ids:
                    store.thread(child, {'parent_thread_id': parent})
        except sqlite3.OperationalError:
            pass
        store.db.commit()
        return found
    finally:
        db.close()


def ingest_file(store, path, tid):
    path = Path(path)
    # Keep a stable logical source when Codex compresses an inactive transcript.
    if path.suffix=='.zst':
        path=path.with_suffix('')
    source = str(path.resolve())
    row = store.db.execute('SELECT offset,state FROM files WHERE path=?', (source,)).fetchone()
    offset, state = (row[0], json.loads(row[1])) if row else (0, {})
    physical=path if path.exists() else Path(str(path)+'.zst')
    stat=physical.stat()
    compressed=physical.suffix=='.zst'
    same_archive=(compressed and state.get('compressed') and state.get('file_id')==stat.st_ino
                  and state.get('stored_size')==stat.st_size and state.get('stored_mtime_ns')==stat.st_mtime_ns
                  and state.get('adapter_version')==2)
    if same_archive:
        return 0
    if (compressed or state.get('compressed',False)!=compressed or offset>stat.st_size
            or state.get('file_id',stat.st_ino)!=stat.st_ino or state.get('adapter_version')!=2):
        store.db.execute('DELETE FROM events WHERE source=?', (source,))
        offset, state = 0, {}
    state['file_id'] = stat.st_ino
    state['adapter_version'] = 2
    state.update(compressed=compressed,stored_size=stat.st_size,stored_mtime_ns=stat.st_mtime_ns)
    records = 0
    with (zstd.open(physical,'rb') if compressed else physical.open('rb')) as stream:
        stream.seek(offset)
        while True:
            start = stream.tell()
            line = stream.readline()
            if not line or not line.endswith(b'\n'):
                offset = start
                break
            offset = stream.tell()
            try:
                record = json.loads(line)
                if not isinstance(record, dict) or not isinstance(record.get('payload'), dict):
                    raise ValueError('unsupported record')
                normalize(store, tid, record, state, source, start)
                records += 1
            except (ValueError, TypeError, KeyError, OverflowError):
                state['malformed_records'] = state.get('malformed_records', 0) + 1
    store.db.execute('INSERT OR REPLACE INTO files VALUES(?,?,?)', (source, offset, json.dumps(state)))
    store.thread(tid, {'last_record_at': state.get('last_record_at'), 'ingest_warnings': state.get('malformed_records', 0), 'history_boundary': state.get('cutoff'), 'ownership_incomplete': state.get('ownership_incomplete',False), 'adapter': 'local-rollout-v2'})
    store.db.commit()
    return records


def normalize(store, tid, record, state, source, byte_offset):
    p = record['payload']
    typ, sub = record.get('type'), p.get('type')
    ts = epoch(record.get('timestamp'))
    if ts is not None:
        try:
            iso(ts)
        except (OSError,OverflowError) as exc:
            raise ValueError('Unsupported record timestamp') from exc
    if typ == 'session_meta':
        if p.get('id') == tid and not state.get('own_meta'):
            state.update(own_meta=True, cutoff=p.get('subagent_history_start_ordinal'),
                         created=epoch(p.get('timestamp')), parent=p.get('parent_thread_id'),
                         fork=p.get('forked_from_id'), source=p.get('source'))
            store.thread(tid, {k: p[k] for k in ['parent_thread_id', 'forked_from_id', 'cwd', 'source', 'history_mode', 'cli_version'] if k in p})
        return
    if not state.get('own_meta'):
        return
    cutoff = state.get('cutoff')
    if cutoff is not None and record.get('ordinal', -1) < cutoff:
        return
    if p.get('thread_id') and p['thread_id'] != tid:
        return
    origin=state.get('source')
    subagent=origin.get('subagent') if isinstance(origin,dict) else None
    isolated_guardian=isinstance(subagent,dict) and subagent.get('other')=='guardian' and not state.get('fork')
    # Without a history boundary, timestamps alone cannot distinguish replayed records.
    if (state.get('fork') or state.get('parent')) and not isolated_guardian and cutoff is None and not state.get('local_started'):
        state['ownership_incomplete']=True
        if p.get('thread_id')==tid:
            state['local_started'] = True
            state['turn']=p.get('turn_id')
        else:
            return
    # Validate nested values before modifying state or journaling observations.
    if typ=='token_usage_record':
        usage=p.get('usage',{})
        if not isinstance(usage,dict):
            raise ValueError('Unsupported token usage')
        for key in TOKEN_FIELDS:
            value=usage.get(key)
            if value is not None and (isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0):
                raise ValueError('Unsupported token count')
    if typ=='response_item' and sub=='message':
        metadata=p.get('internal_chat_message_metadata_passthrough')
        if metadata is not None and not isinstance(metadata,dict):
            raise ValueError('Unsupported message metadata')
        kinds=(metadata or {}).get('content_item_kinds')
        if kinds is not None and (not isinstance(kinds,list) or any(not isinstance(kind,str) for kind in kinds)):
            raise ValueError('Unsupported message provenance')
        content=p.get('content',[])
        if not isinstance(content,list) or any(not isinstance(item,dict) or not isinstance(item.get('text',''),str) for item in content):
            raise ValueError('Unsupported message content')
    if ts is not None:
        state['last_record_at'] = max(ts, state.get('last_record_at') or ts)
    turn = p.get('turn_id') or state.get('turn')
    if turn and ts is not None:
        store.event(tid,'observation',turn,turn,ts,{},source)
    key = p.get('id') or p.get('response_id') or str(record.get('ordinal', byte_offset))

    def put(kind, data, event_key=key, at=ts):
        store.event(tid, kind, event_key, turn, at, data, source)

    if typ == 'event_msg' and sub == 'task_started':
        state['turn'] = turn = p.get('turn_id') or 'unknown-' + key
        put('turn_start', {'root_turn_id': p.get('root_turn_id')}, turn, ts or epoch(p.get('started_at')))
    elif typ == 'event_msg' and sub in ('task_complete', 'turn_aborted', 'task_failed'):
        status = {'task_complete': 'completed', 'turn_aborted': 'interrupted', 'task_failed': 'failed'}[sub]
        put('turn_end', {'status': status, 'duration_ms': p.get('duration_ms'), 'started_at': epoch(p.get('started_at'))}, turn or key)
    elif typ == 'turn_context':
        state['turn'] = turn
        put('context', {'model': p.get('model'), 'effort': p.get('effort'), 'root_turn_id': p.get('root_turn_id')})
    elif typ == 'token_usage_record':
        usage = p.get('usage', {})
        if p.get('response_id') and p.get('thread_id') == tid:
            put('token', {k: usage.get(k) for k in TOKEN_FIELDS}, p['response_id'])
    elif typ == 'compacted':
        put('compact', {}, p.get('compaction_response_id') or p.get('window_id') or key)
    elif typ == 'response_item' and sub == 'message':
        meta = p.get('internal_chat_message_metadata_passthrough') or {}
        turn = meta.get('turn_id') or turn
        kinds = meta.get('content_item_kinds') or []
        text = '\n'.join(c.get('text', '') for c in p.get('content', []) if isinstance(c, dict))
        if p.get('role') == 'assistant' and p.get('phase') in ('commentary', 'final_answer', None):
            put('assistant_message', {'words': prose_words(text), 'phase': p.get('phase')})
        elif p.get('role') == 'user':
            source_value = state.get('source')
            is_agent = state.get('parent') or isinstance(source_value, dict) and 'subagent' in source_value
            if is_agent:
                category = 'nonhuman_message'
            elif 'user.text' in kinds:
                category = 'user_message'
            elif kinds:
                category = 'nonhuman_message'
            else:
                category = 'unclassified_message'
            put(category, {})
    elif typ == 'response_item' and sub == 'agent_message':
        put('agent_message', {'direction': 'sent' if p.get('author') == tid else 'unknown'})
