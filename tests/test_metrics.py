import unittest
from tracker.metrics import timing, union_seconds, prose_words


class TimingTests(unittest.TestCase):
    def test_overlap_and_session_boundary(self):
        result = timing([(0, 100), (50, 150), (1950, 2000), (4001, 4011)])
        self.assertEqual(result, dict(active=210, idle=1800, dormant=2001, sessions=2))

    def test_parallel_children_are_not_elapsed_sum(self):
        self.assertEqual(union_seconds([(0, 600), (0, 600)]), 600)

    def test_words_exclude_fenced_and_inline_code(self):
        self.assertEqual(prose_words('Hello world. `x = 3`\n```python\nsecret = 4\n```\nDone.'), 3)


if __name__ == '__main__':
    unittest.main()
