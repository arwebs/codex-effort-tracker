import unittest
from tracker.activity import classify_interval, resolve_title


class ActivityTests(unittest.TestCase):
    def test_idle_threshold_splits_interval(self):
        sample = dict(at=298, foreground=True, last_input=0, thread_id=None, quality='unassigned')
        result = classify_interval(sample, dict(sample, at=303), 300)
        self.assertEqual([(x['category'], x['end']-x['start']) for x in result], [('active', 2), ('idle', 3)])

    def test_sleep_or_sampler_gap_is_not_active(self):
        sample = dict(at=10, foreground=True, last_input=10, thread_id='a', quality='title')
        self.assertEqual(classify_interval(sample, dict(sample, at=100))[0]['category'], 'unavailable')

    def test_ambiguous_title_cannot_identify_thread(self):
        threads = {'a': {'name': 'Fix'}, 'b': {'name': 'Fix'}}
        self.assertIsNone(resolve_title('Fix - Codex', threads))
        self.assertEqual(resolve_title('Unique - Codex', {'c': {'name': 'Unique'}}), 'c')
        self.assertIsNone(resolve_title('ChatGPT', threads))

    def test_changed_focus_is_not_charged_to_previous_thread(self):
        a = dict(at=10, foreground=True, last_input=10, thread_id='a', quality='title')
        b = dict(a, at=15, thread_id='b')
        self.assertEqual(classify_interval(a,b)[0]['category'], 'unavailable')
