import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tracker.cli import receive_hook, drain_hooks, instance_lock
from tracker.storage import Store
from unittest.mock import patch
from tracker.cli import watch
from tracker.cli import reconcile
from tracker.cli import refresh_code,baseline_path
from compression import zstd


class CliTests(unittest.TestCase):
    def test_failed_repository_scan_is_not_retried_for_each_thread(self):
        with tempfile.TemporaryDirectory() as temp:
            config=dict(data_dir=temp,installed_at=0)
            store=Store(Path(temp)/'db')
            try:
                for tid in ('first','second'):
                    store.thread(tid,dict(cwd=str(Path(temp)/'repo'),last_record_at=10))
                    file=baseline_path(config,tid)
                    file.parent.mkdir(exist_ok=True)
                    file.write_text(json.dumps(dict(root='repo',captured_at=1,complete=True,files={})),encoding='utf-8')
                store.db.commit()
                with patch('tracker.cli.snapshot',side_effect=TimeoutError('slow repository')) as scan:
                    refresh_code(store,config)
                self.assertEqual(scan.call_count,1)
                self.assertTrue(all(t['code_attribution']=='baseline_unavailable' for t in store.all_threads().values()))
            finally:
                store.close()

    def test_failed_cwd_lookup_is_not_retried_for_each_new_thread(self):
        with tempfile.TemporaryDirectory() as temp:
            config=dict(data_dir=temp,installed_at=0)
            store=Store(Path(temp)/'db')
            try:
                for tid in ('first','second'):
                    store.thread(tid,dict(cwd='same-workspace',last_record_at=10))
                store.db.commit()
                with patch('tracker.cli.git_root',side_effect=TimeoutError('slow lookup')) as lookup:
                    refresh_code(store,config)
                self.assertEqual(lookup.call_count,1)
                self.assertTrue(all(t['code_attribution']=='baseline_unavailable' for t in store.all_threads().values()))
            finally:
                store.close()

    def test_new_thread_baselines_reuse_one_snapshot_per_repository(self):
        with tempfile.TemporaryDirectory() as temp:
            config=dict(data_dir=temp,installed_at=0)
            store=Store(Path(temp)/'db')
            try:
                for tid in ('first','second'):
                    store.thread(tid,dict(cwd=str(Path(temp)/'repo'),last_record_at=10))
                store.db.commit()
                value=dict(root=str(Path(temp)/'repo'),complete=True,files={'a.py':['hash']})
                with patch('tracker.cli.git_root',return_value=Path(value['root'])),patch('tracker.cli.snapshot',return_value=value) as scan:
                    refresh_code(store,config)
                self.assertEqual(scan.call_count,1)
                before=[json.loads(baseline_path(config,tid).read_text()) for tid in ('first','second')]
                self.assertEqual([r['thread_id'] for r in before],['first','second'])
                self.assertEqual(before[0]['files'],before[1]['files'])
            finally:
                store.close()

    def test_code_scans_do_not_hold_a_write_lock_between_workspaces(self):
        with tempfile.TemporaryDirectory() as temp:
            config=dict(data_dir=temp,installed_at=0)
            store=Store(Path(temp)/'db')
            sampler=Store(Path(temp)/'db')
            sampler.db.execute('PRAGMA busy_timeout=10')
            try:
                for tid in ('first','second'):
                    store.thread(tid,dict(cwd=str(Path(temp)/tid),last_record_at=10))
                    file=baseline_path(config,tid)
                    file.parent.mkdir(exist_ok=True)
                    file.write_text(json.dumps(dict(root=tid,captured_at=1,complete=True,files={})),encoding='utf-8')
                store.db.commit()
                calls=[]
                def scan(root):
                    sampler.setting('heartbeat',123)
                    sampler.db.commit()
                    calls.append(root)
                    return dict(complete=True,files={})
                with patch('tracker.cli.snapshot',side_effect=scan):
                    refresh_code(store,config)
                self.assertEqual(calls,['first','second'])
            finally:
                sampler.close()
                store.close()

    def test_truncated_archive_preserves_history_and_does_not_block_other_exports(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            bad=folder/'bad.jsonl'
            good=folder/'good.jsonl'
            def record(kind,payload):
                return json.dumps(dict(type=kind,payload=payload,timestamp=100))+'\n'
            raw=record('session_meta',dict(id='bad'))+record('event_msg',dict(type='task_started',turn_id='new'))
            Path(str(bad)+'.zst').write_bytes(zstd.compress(raw.encode())[:-3])
            good.write_text(record('session_meta',dict(id='good'))+record('event_msg',dict(type='task_started',turn_id='good-turn')),encoding='utf-8')
            found=[dict(id='bad',rollout_path=str(bad)),dict(id='good',rollout_path=str(good))]
            store=Store(folder/'metrics.sqlite')
            try:
                for item in found:
                    store.thread(item['id'],item)
                store.event('bad','turn_start','old','old',10,{},str(bad.resolve()))
                store.db.commit()
            finally:
                store.close()
            config=dict(data_dir=temp,output_dir=str(folder/'exports'),codex_home=temp)
            with patch('tracker.cli.sync_metadata',return_value=found):
                result=reconcile(config,include_code=False)
            self.assertEqual(result['errors'],[dict(thread_id='bad',error='EOFError')])
            self.assertTrue((folder/'exports/dashboard-data.js').exists())
            store=Store(folder/'metrics.sqlite')
            try:
                self.assertEqual([r['turn_id'] for r in store.events('bad')],['old'])
                self.assertTrue(any(r['kind']=='turn_start' for r in store.events('good')))
            finally:
                store.close()

    def test_invalid_hook_is_quarantined_without_blocking_valid_hooks(self):
        with tempfile.TemporaryDirectory() as temp:
            config=dict(data_dir=temp)
            inbox=Path(temp)/'inbox'
            inbox.mkdir()
            corrupt=inbox/'000-corrupt.json'
            raw=b'\0'*266
            corrupt.write_bytes(raw)
            (inbox/'001-schema.json').write_text('{"event_id": []}',encoding='utf-8')
            receive_hook(config,dict(session_id='root',hook_event_name='SessionEnd'))
            store=Store(Path(temp)/'db')
            try:
                warnings=drain_hooks(store,config)
                self.assertEqual(store.db.execute('SELECT count(*) FROM hooks').fetchone()[0],1)
                self.assertEqual(len(warnings),2)
                self.assertFalse(list(inbox.glob('*.json')))
                preserved=list((Path(temp)/'quarantined-hooks').glob('*.json'))
                self.assertEqual(len(preserved),2)
                self.assertIn(raw,[p.read_bytes() for p in preserved])
                self.assertEqual(drain_hooks(store,config),[])
                self.assertEqual(len(store.events('root')),1)
            finally:
                store.close()

    def test_unreadable_hook_is_reported_and_retried_without_blocking_export(self):
        with tempfile.TemporaryDirectory() as temp:
            config=dict(data_dir=temp)
            receive_hook(config,dict(session_id='root',hook_event_name='Stop'))
            store=Store(Path(temp)/'db')
            try:
                with patch('tracker.cli.Path.read_text',side_effect=PermissionError('locked')):
                    warnings=drain_hooks(store,config)
                self.assertEqual(len(warnings),1)
                self.assertTrue(list((Path(temp)/'inbox').glob('*.json')))
                self.assertEqual(drain_hooks(store,config),[])
                self.assertEqual(store.db.execute('SELECT count(*) FROM hooks').fetchone()[0],1)
            finally:
                store.close()

    def test_unrepresentable_hook_timestamps_are_quarantined_before_journaling(self):
        with tempfile.TemporaryDirectory() as temp:
            config=dict(data_dir=temp)
            inbox=Path(temp)/'inbox'
            inbox.mkdir()
            for index,stamp in enumerate([10**400,1e300,float('nan'),float('inf')]):
                (inbox/f'{index}.json').write_text(json.dumps(dict(event_id=str(index),received_at=stamp,session_id='root',hook_event_name='SessionEnd')),encoding='utf-8')
            receive_hook(config,dict(session_id='root',hook_event_name='Stop'))
            store=Store(Path(temp)/'db')
            try:
                warnings=drain_hooks(store,config)
                self.assertEqual(len(warnings),4)
                self.assertEqual(store.db.execute('SELECT count(*) FROM hooks').fetchone()[0],1)
                self.assertEqual(store.events('root'),[])
                self.assertEqual(len(list((Path(temp)/'quarantined-hooks').glob('*.json'))),4)
            finally:
                store.close()

    def test_concurrent_hooks_keep_metadata_but_drop_prompt(self):
        with tempfile.TemporaryDirectory() as temp:
            config=dict(data_dir=temp)
            payload=dict(session_id='r',turn_id='t',hook_event_name='UserPromptSubmit',prompt='PRIVATE PROMPT',last_assistant_message='PRIVATE OUTPUT')
            with ThreadPoolExecutor(max_workers=5) as pool:
                list(pool.map(lambda _:receive_hook(config,payload),range(10)))
            files=list((Path(temp)/'inbox').glob('*.json'))
            self.assertEqual(len(files),10)
            self.assertNotIn('PRIVATE',''.join(p.read_text() for p in files))
            store=Store(Path(temp)/'db')
            try:
                drain_hooks(store,config)
                drain_hooks(store,config)
                self.assertEqual(store.db.execute('SELECT count(*) FROM hooks').fetchone()[0],10)
            finally:
                store.close()

    def test_only_one_watcher_can_own_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            with instance_lock(Path(temp)/'watch.lock') as acquired:
                self.assertTrue(acquired)
                with instance_lock(Path(temp)/'watch.lock') as duplicate:
                    self.assertFalse(duplicate)

    def test_transient_sampling_failure_does_not_kill_watcher(self):
        with tempfile.TemporaryDirectory() as temp:
            calls=[]
            def sample(_):
                calls.append(1)
                if len(calls)==1:
                    raise OSError('transient input failure')
                (Path(temp)/'paused').touch()
                return dict(at=100,foreground=False,quality='other_app',last_input=90)
            config=dict(data_dir=temp,sample_interval_seconds=0.001,export_interval_seconds=60)
            with patch('tracker.cli.sample_windows',side_effect=sample),patch('tracker.cli.reconcile',return_value={}):
                watch(config)
            self.assertEqual(len(calls),2)
            store=Store(Path(temp)/'metrics.sqlite')
            try:
                self.assertEqual(store.db.execute('SELECT count(*) FROM activity').fetchone()[0],0)
            finally:
                store.close()
