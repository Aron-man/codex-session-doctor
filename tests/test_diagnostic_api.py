import tempfile
import unittest
from pathlib import Path

from session_doctor import server, store


class DiagnosticApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'root'
        self.root.mkdir()
        con = store.connect(self.tmp.name)
        con.execute("INSERT INTO sessions(id,title,created,last_active) VALUES ('s','demo','2026-09-20T00:00:00Z','2026-09-20T01:00:00Z')")
        for iid, actionable in [('tool', 1), ('observation', 0)]:
            con.execute('''INSERT INTO issues(id,session_id,kind,severity,title,evidence,suggestion,
                timestamp,path,line,actionable,category,confidence,target_path,tool)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (iid, 's', 'repeated_read' if actionable else 'large_output',
                 'warning' if actionable else 'info', iid, 'evidence', 'suggestion',
                 '2026-09-20T00:00:00Z', '/tmp/log', 1, actionable,
                 'tool' if actionable else 'observation', 'high' if actionable else 'info',
                 '/tmp/target', 'exec_command'))
        con.commit()
        con.close()

    def test_default_actionable_and_observation_view(self):
        base = {'days': ['0']}
        main = server.api('/api/issues', base, self.tmp.name, [self.root])
        self.assertEqual([x['id'] for x in main['items']], ['tool'])
        observations = server.api('/api/issues', {**base, 'view': ['observations']},
                                  self.tmp.name, [self.root])
        self.assertEqual([x['id'] for x in observations['items']], ['observation'])
        self.assertIn('error', server.api('/api/issues', {'view': ['bad']},
                                          self.tmp.name, [self.root]))

    def test_config_cache_is_reused_for_listing_and_prompt(self):
        target = str(Path(self.tmp.name) / 'SKILL.md')
        class Cache:
            count = 0

            def snapshot(self):
                self.count += 1
                return {'checked_at': '2026-09-23T00:00:00Z', 'scope': ['temporary'],
                        'files_checked': 1, 'errors': [], 'observations': [],
                        'findings': [{'id': 'config:one', 'kind': 'missing_skill',
                                      'title': '路径缺失', 'evidence': '文件不存在',
                                      'suggestion': '核对路径',
                                      'target_path': target,
                                      'target_line': 1}]}

        cache = Cache()
        listing = server.api('/api/config-audit', {}, self.tmp.name,
                             [self.root], config_cache=cache)
        self.assertEqual(listing['files_checked'], 1)
        prompt = server.api('/api/issue-prompt', {'id': ['config:one']},
                            self.tmp.name, [self.root], config_cache=cache)
        self.assertIn('目标文件', prompt['prompt'])
        self.assertEqual(cache.count, 2)
        self.assertEqual(server.api('/api/issue-prompt', {'id': ['config:missing']},
                                    self.tmp.name, [self.root], config_cache=cache)['error'],
                         '配置问题不存在')


if __name__ == '__main__':
    unittest.main()
