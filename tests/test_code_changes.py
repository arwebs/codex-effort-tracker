import subprocess
import tempfile
import unittest
import os
import sys
import time
import ctypes
from unittest.mock import patch
from pathlib import Path
from tracker.code_changes import snapshot, net_changes, git_root


class CodeTests(unittest.TestCase):
    def test_home_and_drive_roots_are_not_baseline_workspaces(self):
        for root in (Path.home().resolve(),Path(Path.home().anchor)):
            with self.subTest(root=str(root)),patch('tracker.code_changes.run_command',return_value=subprocess.CompletedProcess([],0,str(root)+'\n','')) as git:
                self.assertIsNone(snapshot(root))
                self.assertEqual(git.call_count,1)

    @unittest.skipUnless(os.name=='nt','Windows Git wrapper timeout regression')
    def test_git_timeout_stops_descendants_and_returns_promptly(self):
        from tracker.code_changes import run_command
        with tempfile.TemporaryDirectory() as temp:
            marker=Path(temp)/'child.pid'
            child='import time;time.sleep(60)'
            parent='import subprocess,sys,time;from pathlib import Path;p=subprocess.Popen([sys.executable,"-c",'+repr(child)+']);Path('+repr(str(marker))+').write_text(str(p.pid));time.sleep(60)'
            began=time.monotonic()
            with self.assertRaises(TimeoutError):
                run_command([sys.executable,'-c',parent],timeout=1)
            self.assertLess(time.monotonic()-began,8)
            self.assertTrue(marker.exists())
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.OpenProcess.argtypes=[ctypes.c_ulong,ctypes.c_bool,ctypes.c_ulong]
            kernel.OpenProcess.restype=ctypes.c_void_p
            kernel.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong)]
            kernel.CloseHandle.argtypes=[ctypes.c_void_p]
            handle=kernel.OpenProcess(0x1000,False,int(marker.read_text()))
            if handle:
                try:
                    code=ctypes.c_ulong()
                    self.assertTrue(kernel.GetExitCodeProcess(handle,ctypes.byref(code)))
                    self.assertNotEqual(code.value,259,'Timed-out scan child must not remain alive')
                finally:
                    kernel.CloseHandle(handle)

    def test_dirty_baseline_and_generated_binary_exclusions(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            subprocess.run(['git','init','-q',temp],check=True)
            (root/'a.py').write_text('original\nuser dirty\n')
            (root/'node_modules').mkdir()
            (root/'node_modules'/'skip.js').write_text('ignored\n')
            (root/'binary.cs').write_bytes(b'a\x00b')
            baseline=snapshot(root)
            (root/'a.py').write_text('original\nuser dirty\nnew\n')
            self.assertEqual(net_changes(baseline,snapshot(root)), (1,0))
            self.assertEqual(set(baseline['files']), {'a.py'})
