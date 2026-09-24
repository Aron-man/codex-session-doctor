import datetime as dt
import tempfile
import unittest
from pathlib import Path

from session_doctor import quota_estimate, server, store


UTC = dt.timezone.utc
RESET = dt.datetime(2026, 9, 30, tzinfo=UTC).timestamp()
NOW = dt.datetime(2026, 9, 23, 12, tzinfo=UTC)


class EstimateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'first'
        self.home.mkdir()
        self.con = store.connect(self.tmp.name)
        self.addCleanup(self.con.close)
        self.con.execute("INSERT INTO sessions(id) VALUES ('target')")
        self.con.commit()
        self.snapshot = {'weekly': {'used_percent': 20, 'resets_at': RESET,
                                    'window_minutes': 10080},
                         'updated_at': NOW.isoformat(), 'stale': False}
        self.seq = 0

    def add(self, sid='target', model='gpt-6-sol', input=100000, cached=0,
            output=0, reasoning=0, when=None, root=None):
        self.seq += 1
        when = when or NOW - dt.timedelta(hours=1)
        root = root or self.home
        self.con.execute('''INSERT INTO usage(id,session_id,timestamp,model,input,cached,output,reasoning,total,path)
                            VALUES (?,?,?,?,?,?,?,?,?,?)''',
                         (str(self.seq), sid, when.isoformat(), model, input, cached,
                          output, reasoning, input + output, str(root / 'log.jsonl')))
        self.con.commit()

    def estimate(self, **kwargs):
        return quota_estimate.estimate(self.con, 'target', self.home,
                                       self.snapshot, now=kwargs.get('now', NOW))

    def test_share_is_percentage_of_entire_week(self):
        self.add(input=100000)
        self.add(sid='other', input=900000)
        result = self.estimate()
        self.assertEqual(result['percent'], 2)
        self.assertEqual(result['session_weight'], 5)
        self.assertEqual(result['total_weight'], 50)
        self.assertEqual(result['account_used_percent'], 20)

    def test_model_cache_output_and_reasoning_weights(self):
        self.add(input=100000, cached=80000, output=10000, reasoning=5000)
        self.add(sid='other', model='gpt-5.6-terra', input=100000, cached=0)
        result = self.estimate()
        self.assertAlmostEqual(result['session_weight'], 3.9)
        self.assertAlmostEqual(result['total_weight'], 8.9)
        self.assertAlmostEqual(result['percent'], 20 * 3.9 / 8.9)

    def test_window_as_of_and_first_root(self):
        start = dt.datetime.fromtimestamp(RESET - 10080 * 60, UTC)
        self.add(when=start, input=100000)
        self.add(sid='other', when=NOW, input=100000)
        self.add(sid='other', when=NOW + dt.timedelta(microseconds=1), input=1000000)
        self.add(sid='other', when=start - dt.timedelta(microseconds=1), input=1000000)
        self.add(sid='other', root=Path(self.tmp.name) / 'second', input=1000000)
        result = self.estimate()
        self.assertEqual(result['percent'], 10)
        self.assertEqual(result['window_start'], start.timestamp())
        self.assertEqual(result['as_of'], NOW.isoformat())

    def test_unknown_and_coverage(self):
        self.add(input=100)
        self.add(sid='other', model='new-model', input=100)
        result = self.estimate()
        self.assertEqual(result['known_token_coverage_percent'], 50)
        self.assertEqual(result['unknown_models'], ['new-model'])
        self.assertEqual(result['percent'], 20)
        self.add(model='new-model', input=100)
        self.assertIsNone(self.estimate()['percent'])

    def test_unavailable_and_zero(self):
        self.assertIsNone(self.estimate()['percent'])  # no denominator
        self.assertEqual(self.estimate()['state'], 'unobserved')
        self.add(sid='other')
        self.assertEqual(self.estimate()['percent'], 0)
        self.assertEqual(self.estimate()['state'], 'unobserved')
        self.snapshot['weekly']['used_percent'] = None
        self.assertIsNone(self.estimate()['percent'])
        self.snapshot['weekly']['used_percent'] = 20
        self.snapshot['weekly']['resets_at'] = NOW.timestamp()
        self.assertIn('重置', self.estimate()['reason'])
        self.snapshot['weekly']['resets_at'] = RESET
        self.snapshot['weekly'] = None
        self.assertIsNone(self.estimate()['percent'])
        self.assertEqual(self.estimate()['state'], 'unavailable')

    def test_historical_usage_outside_week_is_not_displayed_as_zero(self):
        self.add(when=NOW-dt.timedelta(days=9))
        self.add(sid='other')
        result=self.estimate()
        self.assertEqual(result['state'], 'out_of_window')
        self.assertEqual(result['percent'], 0)  # legacy numeric compatibility
        self.assertIn('本周期无已记录用量', result['reason'])

    def test_stale_snapshot_and_api(self):
        self.add()
        self.snapshot['stale'] = True
        result = self.estimate()
        self.assertTrue(result['stale'])
        self.assertEqual(result['percent'], 20)
        class Cache:
            def snapshot(inner):
                return self.snapshot
        api_result = server.api('/api/session', {'id': ['target']},
                                self.tmp.name, [self.home], Cache())
        self.assertEqual(api_result['weekly_quota_estimate']['percent'], 20)
        self.assertEqual(api_result['session']['total'], 100000)


if __name__ == '__main__':
    unittest.main()
