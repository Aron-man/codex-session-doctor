import datetime as dt
import tempfile
import unittest
from pathlib import Path

from session_doctor import quota_estimate, server, store


UTC = dt.timezone.utc


class SessionHierarchyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'codex'
        self.home.mkdir()
        self.con = store.connect(self.tmp.name)
        self.addCleanup(self.con.close)
        self.now = dt.datetime.now(UTC).replace(microsecond=0)
        self.snapshot = {'weekly': {'used_percent': 40, 'resets_at': (self.now + dt.timedelta(days=2)).timestamp(),
                                    'window_minutes': 10080}, 'updated_at': self.now.isoformat()}
        self.seq = 0

    def session(self, sid, source='cli', parent=None, created=None):
        when = (created or self.now).isoformat()
        self.con.execute('INSERT INTO sessions(id,title,source,parent_id,created,last_active) VALUES (?,?,?,?,?,?)',
                         (sid, sid, source, parent, when, when))
        self.con.commit()

    def usage(self, sid, amount=100000, model='gpt-6-sol', when=None):
        self.seq += 1
        self.con.execute('''INSERT INTO usage(id,session_id,timestamp,model,input,cached,output,reasoning,total,path)
          VALUES (?,?,?,?,?,?,?,?,?,?)''', (str(self.seq), sid, (when or self.now).isoformat(), model,
          amount, 0, 0, 0, amount, str(self.home / 'log.jsonl')))
        self.con.commit()

    def api(self, path, query):
        class Cache:
            def snapshot(inner):
                return self.snapshot
        return server.api(path, query, self.tmp.name, [self.home], Cache())

    def test_nested_groups_pagination_search_and_fork(self):
        old = self.now - dt.timedelta(days=20)
        self.session('parent', created=old)
        self.session('child', 'subagent', 'parent')
        self.session('grandchild', 'subagent', 'child')
        self.session('fork', 'vscode', 'parent')
        self.session('other')
        for sid in ('child', 'grandchild', 'fork', 'other'):
            self.usage(sid)
        grouped = self.api('/api/sessions', {'grouped': ['1'], 'days': ['7'], 'limit': ['2']})
        self.assertEqual(grouped['total'], 3)
        parent = next(row for row in grouped['items'] if row['id'] == 'parent') if any(row['id'] == 'parent' for row in grouped['items']) else None
        if parent is None:
            parent = self.api('/api/sessions', {'grouped': ['1'], 'days': ['7'], 'q': ['grandchild']})['items'][0]
        self.assertEqual(parent['group_usage']['total'], 200000)
        self.assertEqual(parent['self_usage']['total'], 0)
        self.assertEqual(parent['descendant_count'], 2)
        self.assertEqual(parent['children'][0]['children'][0]['id'], 'grandchild')
        self.assertEqual(parent['group_weekly_quota_estimate']['percent'], 20)
        self.assertEqual(parent['weekly_quota_estimate']['state'], 'unobserved')
        searched = self.api('/api/sessions', {'grouped': ['1'], 'q': ['grandchild']})
        self.assertEqual(searched['total'], 1)
        self.assertEqual(searched['items'][0]['id'], 'parent')
        self.assertTrue(searched['items'][0]['children'][0]['search_path'])
        flat = self.api('/api/sessions', {'q': ['grandchild']})
        self.assertEqual(flat['items'][0]['id'], 'grandchild')
        self.assertEqual(flat['items'][0]['total'], 100000)
        self.assertEqual(self.api('/api/sessions', {'grouped': ['1'], 'limit': ['1'], 'offset': ['3']})['items'], [])
        detail = self.api('/api/session', {'id': ['parent']})
        self.assertEqual(detail['descendant_count'], 2)
        self.assertEqual(detail['group_weekly_quota_estimate']['percent'], 20)
        self.assertEqual(detail['weekly_quota_estimate']['state'], 'unobserved')

    def test_orphan_cycle_and_unknown_model(self):
        self.session('orphan', 'subagent', 'missing')
        self.session('a', 'subagent', 'b')
        self.session('b', 'subagent', 'a')
        self.session('known')
        for sid in ('orphan', 'a', 'b', 'known'):
            self.usage(sid)
        roots = self.api('/api/sessions', {'grouped': ['1']})['items']
        self.assertEqual(len(roots), 3)
        self.assertIn('缺失', next(x for x in roots if x['id'] == 'orphan')['hierarchy_warning'])
        cycle = next(x for x in roots if x['id'] == 'a')
        self.assertEqual(cycle['group_usage']['total'], 200000)
        self.assertIn('循环', cycle['hierarchy_warning'])
        self.assertAlmostEqual(sum(x['group_weekly_quota_estimate']['session_weight'] for x in roots), 20)
        self.assertAlmostEqual(sum(x['group_weekly_quota_estimate']['percent'] for x in roots), 40)
        self.usage('b', 100, 'unknown-model')
        roots = self.api('/api/sessions', {'grouped': ['1']})['items']
        cycle = next(x for x in roots if x['id'] == 'a')
        self.assertIsNone(cycle['group_weekly_quota_estimate']['percent'])
        self.assertIn('unknown-model', cycle['group_weekly_quota_estimate']['reason'])
        self.assertEqual(cycle['weekly_quota_estimate']['state'], 'estimated')

    def test_out_of_window_history_and_single_allocation(self):
        self.session('old', created=self.now - dt.timedelta(days=15))
        self.usage('old', when=self.now - dt.timedelta(days=15))
        self.session('current')
        self.usage('current')
        allocation = quota_estimate.allocate(self.con, self.home, self.snapshot, now=self.now)
        self.assertEqual(quota_estimate.for_sessions(allocation, ['old'])['state'], 'out_of_window')
        self.assertEqual(quota_estimate.for_sessions(allocation, ['current'])['percent'], 40)
        self.assertEqual(self.api('/api/session', {'id': ['old']})['weekly_quota_estimate']['state'], 'out_of_window')


if __name__ == '__main__':
    unittest.main()
