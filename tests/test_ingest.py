import json
import tempfile
import unittest
from compression import zstd
from pathlib import Path
from tracker.storage import Store
from tracker.ingest import ingest_file


def event(kind, payload, ordinal, timestamp=100):
    return dict(type=kind, payload=payload, ordinal=ordinal, timestamp=timestamp)


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'data.sqlite')
        self.addCleanup(self.store.close)
        self.path = self.root / 'rollout.jsonl'

    def write(self, records):
        self.path.write_text(''.join(json.dumps(r) + '\n' for r in records), encoding='utf-8')

    def test_child_inheritance_duplicate_usage_and_message_provenance(self):
        usage = dict(thread_id='child', turn_id='t', response_id='r', usage=dict(input_tokens=100, output_tokens=10, total_tokens=110))
        self.write([
            event('session_meta', dict(id='child', parent_thread_id='root', subagent_history_start_ordinal=4), 0),
            event('session_meta', dict(id='root'), 1),
            event('event_msg', dict(type='task_started', turn_id='parent'), 2),
            event('compacted', dict(window_id='inherited'), 3),
            event('event_msg', dict(type='task_started', turn_id='t'), 4),
            event('turn_context', dict(turn_id='t', model='m', effort='high'), 5),
            event('token_usage_record', usage, 6),
            event('token_usage_record', usage, 7),
            event('response_item', dict(type='message', id='u', role='user', content=[dict(type='input_text', text='hello')], internal_chat_message_metadata_passthrough=dict(turn_id='t', content_item_kinds=['user.text'])), 8),
            event('response_item', dict(type='message', id='d', role='user', content=[dict(type='input_text', text='injected')], internal_chat_message_metadata_passthrough=dict(turn_id='t', content_item_kinds=['agents_md.instructions'])), 9),
            event('event_msg', dict(type='task_complete', turn_id='t', duration_ms=10000), 10, 110),
        ])
        ingest_file(self.store, self.path, 'child')
        ingest_file(self.store, self.path, 'child')
        events = self.store.events('child')
        self.assertEqual(sum(e['kind'] == 'token' for e in events), 1)
        self.assertEqual(sum(e['kind'] == 'turn_start' for e in events), 1)
        self.assertEqual(sum(e['kind'] == 'compact' for e in events), 0)
        self.assertEqual(sum(e['kind'] == 'user_message' for e in events), 0)
        self.assertEqual(sum(e['kind'] == 'nonhuman_message' for e in events), 2)

    def test_transcript_compression_preserves_history_and_is_incremental(self):
        self.write([
            event('session_meta',dict(id='root'),0),
            event('event_msg',dict(type='task_started',turn_id='t'),1),
            event('event_msg',dict(type='task_complete',turn_id='t',duration_ms=10000),2,110),
        ])
        ingest_file(self.store,self.path,'root')
        before=self.store.events('root')
        compressed=Path(str(self.path)+'.zst')
        compressed.write_bytes(zstd.compress(self.path.read_bytes()))
        self.path.unlink()
        ingest_file(self.store,self.path,'root')
        self.assertEqual(self.store.events('root'),before)
        self.assertEqual(ingest_file(self.store,self.path,'root'),0)
        self.assertEqual(ingest_file(self.store,compressed,'root'),0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM files').fetchone()[0],1)

    def test_changed_compressed_transcript_is_rebuilt_and_corruption_surfaces(self):
        first=json.dumps(event('session_meta',dict(id='root'),0))+'\n'
        compressed=Path(str(self.path)+'.zst')
        compressed.write_bytes(zstd.compress(first.encode()))
        ingest_file(self.store,self.path,'root')
        appended=first+json.dumps(event('event_msg',dict(type='task_started',turn_id='new'),1))+'\n'
        compressed.write_bytes(zstd.compress(appended.encode()))
        self.assertGreater(ingest_file(self.store,self.path,'root'),0)
        self.assertEqual(sum(e['kind']=='turn_start' for e in self.store.events('root')),1)
        compressed.write_bytes(b'not a zstd stream')
        with self.assertRaises(zstd.ZstdError):
            ingest_file(self.store,self.path,'root')

    def test_partial_line_is_read_after_completion(self):
        self.write([event('session_meta', dict(id='root'), 0)])
        full = json.dumps(event('event_msg', dict(type='task_started', turn_id='t'), 1)) + '\n'
        with self.path.open('a', encoding='utf-8') as f:
            f.write(full[:20])
        ingest_file(self.store, self.path, 'root')
        self.assertEqual(len(self.store.events('root')), 0)
        with self.path.open('a', encoding='utf-8') as f:
            f.write(full[20:])
        ingest_file(self.store, self.path, 'root')
        self.assertEqual(sum(e['kind']=='turn_start' for e in self.store.events('root')), 1)

    def test_message_text_is_not_retained(self):
        self.write([event('session_meta', dict(id='root'), 0), event('response_item', dict(type='message', id='a', role='assistant', phase='final_answer', content=[dict(type='output_text', text='PRIVATE SENTENCE')]), 1)])
        ingest_file(self.store, self.path, 'root')
        self.assertNotIn('PRIVATE SENTENCE', json.dumps(self.store.events('root')))

    def test_child_without_boundary_does_not_count_parent_history(self):
        self.write([
            event('session_meta',dict(id='child',parent_thread_id='root'),0),
            event('event_msg',dict(type='task_started',turn_id='parent'),1),
            event('event_msg',dict(type='task_complete',turn_id='parent',duration_ms=10000),2,110),
            event('token_usage_record',dict(thread_id='child',turn_id='own',response_id='resp',usage={'total_tokens':5}),3,120),
            event('event_msg',dict(type='task_complete',turn_id='own',duration_ms=5000),4,125),
        ])
        ingest_file(self.store,self.path,'child')
        self.assertFalse(any(e['turn_id']=='parent' for e in self.store.events('child')))


if __name__ == '__main__':
    unittest.main()
