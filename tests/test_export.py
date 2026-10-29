import csv
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from tracker.storage import Store
from tracker.export import build_rows, write_csv, write_dashboard_data, export_all
import json


class ExportTests(unittest.TestCase):
    def test_dashboard_snapshot_keeps_unknowns_zeroes_and_one_capture_time(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        folder=Path(temp.name)
        store=Store(folder/'db')
        self.addCleanup(store.close)
        store.thread('r',dict(name='</script> & private title',last_record_at=10))
        store.event('r','turn_start','t','t',10,{})
        store.event('r','turn_end','t','t',10,{'status':'completed','duration_ms':0})
        export_all(store,folder)
        script=(folder/'dashboard-data.js').read_text(encoding='utf-8')
        self.assertTrue(script.startswith('window.CODEX_EFFORT_DATA = '))
        data=json.loads(script[len('window.CODEX_EFFORT_DATA = '):].strip().removesuffix(';'))
        self.assertEqual(data['threads'][0]['thread_name'],'</script> & private title')
        self.assertIsNone(data['threads'][0]['human_active_estimated_seconds'])
        self.assertEqual(data['threads'][0]['agent_active_seconds'],0)
        self.assertEqual(data['generated_at'],data['threads'][0]['exported_at'])
        self.assertEqual(data['generated_at'],data['turns'][0]['exported_at'])
        self.assertFalse(data['observer']['running'])

    def test_busy_dashboard_snapshot_preserves_previous_data(self):
        with tempfile.TemporaryDirectory() as temp:
            file=Path(temp)/'dashboard-data.js'
            write_dashboard_data(file,[],[],{})
            original=file.read_bytes()
            with patch('tracker.export.os.replace',side_effect=PermissionError('busy snapshot')):
                with self.assertRaises(PermissionError):
                    write_dashboard_data(file,[{'thread_id':'new'}],[],{})
            self.assertEqual(file.read_bytes(),original)
            self.assertEqual(list(Path(temp).glob('*.tmp')),[])

    def test_dashboard_exports_coalesced_timestamped_host_activity(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            store=Store(folder/'db')
            self.addCleanup(store.close)
            store.db.executemany('INSERT INTO activity(start,end,category,thread_id,quality) VALUES(?,?,?,?,?)',[
                (10,15,'active',None,'host_unverified'),
                (15,20,'active',None,'host_unverified'),
                (21,25,'active',None,'host_unverified'),
                (25,30,'idle',None,'host_unverified'),
            ])
            export_all(store,folder,session_gap=120)
            data=json.loads((folder/'dashboard-data.js').read_text(encoding='utf-8').split(' = ',1)[1].strip().removesuffix(';'))
            store.close()
            self.assertEqual(data['session_gap_seconds'],120)
            self.assertEqual(len(data['activity']),3)
            self.assertEqual(data['activity'][0],dict(started_at='1970-01-01T00:00:10.000+00:00',ended_at='1970-01-01T00:00:20.000+00:00',category='active',thread_id=None,quality='host_unverified'))
            self.assertEqual(data['activity'][1]['started_at'],'1970-01-01T00:00:21.000+00:00')
            self.assertEqual(data['activity'][2]['category'],'idle')

    def test_child_effort_and_unknown_human_values(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp)/'db')
            self.addCleanup(store.close)
            for tid in ('root','a','b'):
                store.thread(tid,dict(parent_thread_id='root' if tid!='root' else None,last_record_at=600))
                store.event(tid,'turn_start','t','t',0,{})
                store.event(tid,'turn_end','t','t',600,{'status':'completed','duration_ms':600000})
            threads,turns=build_rows(store,now=610)
            root=next(r for r in threads if r['thread_id']=='root')
            self.assertEqual(root['agent_active_seconds'],600)
            self.assertEqual(root['descendant_agent_active_seconds'],1200)
            self.assertEqual(root['tree_active_elapsed_seconds'],600)
            self.assertIsNone(root['human_active_estimated_seconds'])
            self.assertEqual(len(turns),3)
            store.close()

    def test_open_turn_uses_evidence_bound_not_export_clock(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp)/'db')
            self.addCleanup(store.close)
            store.thread('r',dict(last_record_at=10))
            store.event('r','turn_start','t','t',0,{})
            store.event('r','observation','t','t',10,{})
            threads,_=build_rows(store,now=1000)
            self.assertEqual(threads[0]['agent_active_seconds'],10)
            self.assertIn('open_turn_lower_bound',threads[0]['coverage'])
            store.close()

    def test_missing_terminal_does_not_absorb_later_turn(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store=Store(Path(temp.name)/'db')
        self.addCleanup(store.close)
        store.thread('r',dict(last_record_at=1000))
        store.event('r','turn_start','old','old',0,{})
        store.event('r','observation','old','old',10,{})
        store.event('r','turn_start','new','new',900,{})
        store.event('r','turn_end','new','new',1000,{'status':'completed','duration_ms':100000})
        rows,turns=build_rows(store,now=2000)
        self.assertEqual(rows[0]['agent_active_seconds'],110)
        self.assertEqual(rows[0]['agent_idle_seconds'],0)
        self.assertEqual(rows[0]['unknown_gap_seconds'],890)
        self.assertEqual(rows[0]['status'],'idle')
        self.assertEqual(next(t for t in turns if t['turn_id']=='old')['active_seconds'],10)

    def test_csv_formula_safety_and_blank_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            file=Path(temp)/'out.csv'
            write_csv(file,[dict(name='=RUN()',unknown=None,measured=0)])
            with file.open(encoding='utf-8-sig',newline='') as stream:
                row=next(csv.DictReader(stream))
            self.assertEqual(row,{'name':"'=RUN()",'unknown':'','measured':'0'})

    def test_busy_export_preserves_previous_csv(self):
        with tempfile.TemporaryDirectory() as temp:
            file=Path(temp)/'out.csv'
            write_csv(file,[{'name':'previous'}])
            original=file.read_bytes()
            with patch('tracker.export.os.replace',side_effect=PermissionError('file held by Excel')):
                with self.assertRaises(PermissionError):
                    write_csv(file,[{'name':'new'}])
            self.assertEqual(file.read_bytes(),original)
            self.assertEqual(list(Path(temp).glob('*.tmp')),[])
