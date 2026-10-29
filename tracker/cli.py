"""Local commands and a hidden, per-user observer process."""
import argparse
import contextlib
import hashlib
import json
import math
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from .activity import record_sample, sample_windows
from .code_changes import snapshot, net_changes, git_root
from .export import export_all
from .ingest import ingest_file, sync_metadata
from compression.zstd import ZstdError
from .metrics import epoch, iso
from .storage import Store

HOOK_FIELDS=('session_id','turn_id','hook_event_name','transcript_path','cwd','model','agent_id','agent_type','reason','trigger')
HOOK_EVENTS={'SessionStart','UserPromptSubmit','Stop','Interrupt','PostCompact','SubagentStart','SubagentStop','SessionEnd'}


def atomic_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        os.replace(temp,path)
    finally:
        temp.unlink(missing_ok=True)


def baseline_path(config,tid):
    return Path(config['data_dir'])/'baselines'/(hashlib.sha256(tid.encode()).hexdigest()+'.json')


def capture_baseline(config,tid,cwd,method,snapshot_value=None):
    path=baseline_path(config,tid)
    if path.exists() or not cwd:
        return
    captured=time.time()
    value=snapshot(cwd) if snapshot_value is None else snapshot_value
    if value:
        value=dict(value)
        value.update(thread_id=tid,captured_at=captured,method=method)
        # Exclusive create avoids replacing a baseline recorded by a simultaneous hook.
        path.parent.mkdir(parents=True,exist_ok=True)
        try:
            with path.open('x',encoding='utf-8') as stream:
                json.dump(value,stream)
        except FileExistsError:
            pass


def receive_hook(config,payload):
    event=payload.get('hook_event_name')
    if event not in HOOK_EVENTS:
        return None
    data={k:v for k,v in payload.items() if k in HOOK_FIELDS and isinstance(v,(str,int,float,bool))}
    data.update(event_id=uuid.uuid4().hex,received_at=time.time())
    atomic_json(Path(config['data_dir'])/'inbox'/(data['event_id']+'.json'),data)
    # Baseline scans belong to the observer, never the synchronous hook path.
    return data['event_id']


def drain_hooks(store,config):
    inbox=Path(config['data_dir'])/'inbox'
    warnings=[]
    for path in sorted(inbox.glob('*.json')):
        try:
            data=json.loads(path.read_text(encoding='utf-8'))
            if (not isinstance(data,dict) or not isinstance(data.get('event_id'),str) or not data['event_id']
                    or not isinstance(data.get('received_at'),(int,float)) or isinstance(data['received_at'],bool)
                    or not math.isfinite(data['received_at']) or data.get('hook_event_name') not in HOOK_EVENTS
                    or any(v is not None and not isinstance(v,(str,int,float,bool)) for k,v in data.items() if k in HOOK_FIELDS)):
                raise ValueError('Invalid hook metadata')
            try:
                iso(data['received_at'])
            except (OSError,OverflowError,ValueError):
                raise ValueError('Unrepresentable hook timestamp') from None
        except (ValueError,UnicodeError,TypeError,OverflowError) as exc:
            # Retain the exact bytes for diagnosis; one bad record must not stop exports.
            quarantine=Path(config['data_dir'])/'quarantined-hooks'
            warning=dict(file=path.name,error=type(exc).__name__,quarantined=False)
            try:
                quarantine.mkdir(parents=True,exist_ok=True)
                path.replace(quarantine/(uuid.uuid4().hex+'-'+path.name))
                warning['quarantined']=True
            except OSError:
                pass  # Leave unreadable/unmovable input in place for the next cycle.
            warnings.append(warning)
            continue
        except OSError as exc:
            warnings.append(dict(file=path.name,error=type(exc).__name__,quarantined=False))
            continue
        event_id=data['event_id']
        store.db.execute('INSERT OR IGNORE INTO hooks VALUES(?,?,?)',(event_id,data['received_at'],json.dumps(data)))
        tid=data.get('session_id')
        event=data.get('hook_event_name')
        if tid and event=='SessionEnd':
            store.event(tid,'session_end',event_id,None,data['received_at'],{},'hook')
        if tid and event=='SubagentStart' and data.get('agent_id'):
            store.thread(data['agent_id'],dict(parent_thread_id=tid,agent_role=data.get('agent_type')))
        store.db.commit()
        try:
            path.unlink()
        except OSError as exc:
            warnings.append(dict(file=path.name,error=type(exc).__name__,quarantined=False))
    return warnings


@contextlib.contextmanager
def instance_lock(path):
    path.parent.mkdir(parents=True,exist_ok=True)
    stream=path.open('a+b')
    acquired=False
    try:
        if stream.tell()==0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            acquired=True
        except OSError:
            pass
        yield acquired
    finally:
        if acquired:
            stream.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(stream,fcntl.LOCK_UN)
        stream.close()


def refresh_code(store,config):
    if not config.get('code_baselines',True):
        return
    threads=store.all_threads()
    snapshots={}
    roots={}
    failed_cwds=set()
    for tid,meta in threads.items():
        # Only baseline threads seen during the installation window, never every historical checkout.
        if not meta.get('cwd') or meta.get('parent_thread_id') or (meta.get('last_record_at') or 0)<config.get('installed_at',0):
            continue
        path=baseline_path(config,tid)
        try:
            if not path.exists():
                cwd=meta['cwd']
                if cwd in failed_cwds:
                    store.thread(tid,{'code_attribution':'baseline_unavailable'})
                    continue
                if cwd not in roots:
                    failed_cwds.add(cwd)
                    roots[cwd]=git_root(cwd)
                    failed_cwds.discard(cwd)
                root=roots[cwd]
                if root is None:
                    store.thread(tid,{'code_attribution':'not_git_workspace'})
                    continue
                root=str(root)
                if root not in snapshots:
                    snapshots[root]=None
                    snapshots[root]=snapshot(root)
                if not snapshots[root]:
                    store.thread(tid,{'code_attribution':'baseline_unavailable'})
                    continue
                capture_baseline(config,tid,cwd,'observer_checkpoint',snapshot_value=snapshots[root])
            if not path.exists():
                store.thread(tid,{'code_attribution':'not_git_workspace'})
                continue
            before=json.loads(path.read_text(encoding='utf-8'))
            root=before['root']
            if root not in snapshots:
                snapshots[root]=None
                snapshots[root]=snapshot(root)
            if snapshots[root] is None:
                store.thread(tid,{'code_attribution':'baseline_unavailable'})
                continue
            added,removed=net_changes(before,snapshots[root])
            store.thread(tid,dict(workspace_lines_added=added,workspace_lines_removed=removed,
                                 code_baseline_at=before['captured_at'],code_attribution='workspace_checkpoint_unattributed',
                                 code_lines_added=None,code_lines_removed=None))
        except (OSError,ValueError,RuntimeError,TimeoutError):
            store.thread(tid,{'code_attribution':'baseline_unavailable'})
        finally:
            # Do not hold a writer lock while the next workspace is being scanned.
            store.db.commit()
    # Counts remain workspace observations: process/thread exclusivity cannot prove authorship.
    store.db.commit()


def reconcile(config,include_code=True):
    data_dir=Path(config['data_dir'])
    with instance_lock(data_dir/'reconcile.lock') as acquired:
        if not acquired:
            return {'status':'another_reconcile_running'}
        store=Store(data_dir/'metrics.sqlite')
        errors=[]
        def progress(stage):
            atomic_json(data_dir/'reconcile-progress.json',dict(at=iso(time.time()),stage=stage))
        try:
            progress('hooks')
            hook_warnings=drain_hooks(store,config)
            since=epoch(config.get('initial_since')) or 0
            progress('metadata')
            found=sync_metadata(store,config['codex_home'],since)
            read_count=0
            progress('transcripts')
            for meta in found:
                if not meta.get('rollout_path'):
                    continue
                try:
                    read_count+=ingest_file(store,meta['rollout_path'],meta['id'])
                except (OSError,ValueError,EOFError,ZstdError) as exc:
                    errors.append({'thread_id':meta['id'],'error':type(exc).__name__})
                    store.db.rollback()
            if include_code:
                progress('code_changes')
                refresh_code(store,config)
            progress('export')
            result=export_all(store,Path(config['output_dir']),config.get('session_gap_seconds',1800))
            result.update(new_records=read_count,errors=errors,hook_warnings=hook_warnings,completed_at=iso(time.time()))
            store.setting('last_export',result)
            store.db.commit()
            progress('complete')
            return result
        finally:
            store.close()


def watch(config):
    data_dir=Path(config['data_dir'])
    with instance_lock(data_dir/'watch.lock') as acquired:
        if not acquired:
            return
        if (data_dir/'paused').exists():
            return
        store=Store(data_dir/'metrics.sqlite')
        store.setting('observer_started_at',time.time())
        store.setting('watch_pid',os.getpid())
        store.db.commit()
        stopped=threading.Event()
        def export_loop():
            while not stopped.is_set():
                try:
                    reconcile(config)
                except Exception as exc:
                    atomic_json(data_dir/'last_error.json',dict(at=iso(time.time()),component='reconcile',error=type(exc).__name__))
                stopped.wait(config.get('export_interval_seconds',60))
        worker=threading.Thread(target=export_loop,daemon=True)
        worker.start()
        previous=None
        failures=0
        try:
            while not (data_dir/'paused').exists():
                delay=config.get('sample_interval_seconds',5)
                try:
                    current=sample_windows(store.all_threads())
                    record_sample(store,previous,current,config.get('human_idle_seconds',300))
                    store.setting('heartbeat',time.time())
                    store.db.commit()
                    previous=current
                    failures=0
                except Exception as exc:
                    # Losing an observation must break continuity, not fabricate engagement.
                    previous=None
                    failures+=1
                    try:
                        store.db.rollback()
                    except Exception:
                        store.close()
                        store=Store(data_dir/'metrics.sqlite')
                    try:
                        atomic_json(data_dir/'last_sample_error.json',dict(at=iso(time.time()),error=type(exc).__name__))
                    except OSError:
                        pass
                    delay=min(30,delay*(2**min(failures,4)))
                if stopped.wait(delay):
                    break
        finally:
            stopped.set()
            worker.join(timeout=20)
            store.setting('stopped_at',time.time())
            store.db.commit()
            store.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description='Local Codex time and effort tracker')
    parser.add_argument('--config',required=True,type=Path)
    parser.add_argument('command',choices=['hook','reconcile','watch','status','pause','resume','backfill'])
    parser.add_argument('--since',help='ISO date or timestamp for historical backfill')
    parser.add_argument('--skip-code',action='store_true')
    args=parser.parse_args(argv)
    config=json.loads(args.config.read_text(encoding='utf-8-sig'))
    data_dir=Path(config['data_dir'])
    data_dir.mkdir(parents=True,exist_ok=True)
    if args.command=='hook':
        try:
            payload=json.load(sys.stdin)
            receive_hook(config,payload)
        except Exception as exc:
            atomic_json(data_dir/'last_hook_error.json',dict(at=iso(time.time()),error=type(exc).__name__))
        print('{}')
        return
    if args.command=='watch':
        watch(config)
        return
    if args.command=='pause':
        (data_dir/'paused').touch()
        print('Pause requested; the observer exits after its current sample.')
        return
    if args.command=='resume':
        (data_dir/'paused').unlink(missing_ok=True)
        import subprocess
        python=Path(sys.executable).with_name('pythonw.exe') if os.name=='nt' else Path(sys.executable)
        runner=Path(__file__).resolve().parents[1]/'run_tracker.py'
        subprocess.Popen([str(python),str(runner),'--config',str(args.config.resolve()),'watch'],
                         creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        print('Observer start requested.')
        return
    if args.command=='backfill':
        if not args.since or epoch(args.since) is None:
            parser.error('backfill requires a valid --since ISO timestamp')
        config['initial_since']=args.since
    if args.command in ('reconcile','backfill'):
        print(json.dumps(reconcile(config,not args.skip_code),indent=2))
        return
    store=Store(data_dir/'metrics.sqlite')
    try:
        heartbeat=store.setting('heartbeat')
        status=dict(observer_running=bool(heartbeat and time.time()-heartbeat<20 and not (data_dir/'paused').exists()),
                    paused=(data_dir/'paused').exists(),heartbeat=iso(heartbeat),last_sample=store.setting('last_sample'),
                    last_export=store.setting('last_export'),hook_events=store.db.execute('SELECT count(*) FROM hooks').fetchone()[0],
                    pending_hooks=len(list((data_dir/'inbox').glob('*.json'))),output_dir=config['output_dir'])
        print(json.dumps(status,indent=2))
    finally:
        store.close()
