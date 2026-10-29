import copy
import unittest
from install import merge_hooks, remove_hooks


class InstallTests(unittest.TestCase):
    def test_idempotent_merge_preserves_other_hooks(self):
        original={'description':'user owned','hooks':{'SessionStart':[{'hooks':[{'type':'command','command':'existing'}]}]}}
        before=copy.deepcopy(original)
        command="& 'python' 'owned/run_tracker.py' --config 'owned/settings.json' hook"
        merged=merge_hooks(original,command)
        self.assertEqual(original,before)
        self.assertEqual(merge_hooks(merged,command),merged)
        self.assertEqual(remove_hooks(merged,command),original)
