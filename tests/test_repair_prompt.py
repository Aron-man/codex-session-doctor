import tempfile
import unittest
import json

from session_doctor import repair_prompt, server, store


class RepairPromptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(self.tmp.name)
        self.sid = 'session-1'
        self.con.execute('INSERT INTO sessions(id,title,cwd,parent_id) VALUES(?,?,?,?)',
                         (self.sid, '原任务 token="secret"', '/tmp/work', 'parent'))
        self.con.execute('INSERT INTO sessions(id,title,cwd) VALUES(?,?,?)',
                         ('other', '别的任务', '/tmp/other'))

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def issue(self, kind):
        iid = 'issue-' + kind
        self.con.execute('''INSERT INTO issues(id,session_id,kind,severity,title,evidence,
            suggestion,timestamp,turn_id,path,line,estimated_tokens)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
            (iid, self.sid, kind, 'info' if kind in ('large_context', 'low_cache') else 'warning',
             'token="hidden"', '估算 100 token', '建议检查', '2026-09-23T00:00:01Z',
             'turn-1', '/tmp/log.jsonl', 20, 100))
        return iid

    def call(self, cid, sid='session-1', line=20, command='rg token="secret"'):
        self.con.execute('''INSERT INTO calls(id,session_id,timestamp,turn_id,name,command,
            output_chars,output_hash,estimated_tokens,status,path,line)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
            (cid, sid, '2026-09-23T00:00:01Z', 'turn-1', 'exec', command, 25000,
             'same', 6250, 'failed', '/tmp/log.jsonl', line))

    def usage(self, uid, sid='session-1', line=20):
        self.con.execute('''INSERT INTO usage(id,session_id,timestamp,turn_id,model,input,
            cached,output,reasoning,total,purpose,path,line,requests)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (uid, sid, '2026-09-23T00:00:01Z', 'turn-1', 'm', 160000, 100,
             2, 0, 160002, 'model', '/tmp/log.jsonl', line, 1))

    def test_five_kinds_are_targeted_and_bounded(self):
        expected = {
            'repeated_read': '复用已读内容',
            'repeated_failure': '首次失败的退出错误',
            'large_output': '限制字段、分页',
            'large_context': '先判断大上下文是否为原任务所必需',
            'low_cache': '核对模型切换、请求间隔及前缀变化',
        }
        self.call('anchor')
        self.usage('usage')
        for kind, text in expected.items():
            with self.subTest(kind=kind):
                result = repair_prompt.generate(self.con, self.issue(kind))
                prompt = result['prompt']
                self.assertIn(text, prompt)
                self.assertIn('实际 workdir', prompt)
                self.assertIn('日志 path:line 是取证入口', prompt)
                self.assertIn('不强制改文件', prompt)
                self.assertIn('不要自动 commit、push 或部署', prompt)
                self.assertLess(len(prompt), 12000)
                self.assertNotIn('token="secret"', prompt)
                self.assertNotIn('token="hidden"', prompt)
                self.assertEqual(result['session_id'], self.sid)
                self.assertTrue(result['generated_at'].endswith('Z'))

    def test_related_calls_stay_in_session_and_match_command(self):
        iid = self.issue('repeated_read')
        self.call('anchor')
        self.call('matching', line=21)
        self.call('irrelevant', line=22, command='cat unrelated')
        self.call('foreign', sid='other', line=23, command='foreign-secret')
        prompt = repair_prompt.generate(self.con, iid)['prompt']
        self.assertNotIn('foreign-secret', prompt)
        self.assertNotIn('cat unrelated', prompt)
        self.assertEqual(prompt.count('"command"'), 2)
        self.assertIn('"line": 21', prompt)

    def test_usage_stays_in_session_and_unknown_id(self):
        iid = self.issue('large_context')
        self.usage('own')
        self.usage('foreign', sid='other', line=99)
        prompt = repair_prompt.generate(self.con, iid)['prompt']
        self.assertEqual(prompt.count('"model":'), 1)
        self.assertNotIn('"line": 99', prompt)
        self.assertEqual(repair_prompt.generate(self.con, '')['error'], '缺少问题 ID')
        self.assertEqual(repair_prompt.generate(self.con, 'missing')['error'], '问题不存在')
        self.con.commit()
        self.assertEqual(server.api('/api/issue-prompt', {'id': [iid]}, self.tmp.name, [])['issue_id'], iid)

    def test_long_dynamic_fields_keep_valid_json_and_prompt_under_budget(self):
        iid = self.issue('repeated_failure')
        long = 'x' * 10000
        self.con.execute('UPDATE sessions SET title=?,cwd=?,parent_id=? WHERE id=?',
                         (long, long, long, self.sid))
        self.con.execute('UPDATE issues SET title=?,evidence=?,suggestion=?,path=?,turn_id=? WHERE id=?',
                         (long, long, long, '/tmp/log.jsonl', 'turn-1', iid))
        for i in range(5):
            self.call('long-' + str(i), line=20+i, command=long)
            self.con.execute('UPDATE calls SET name=?,status=? WHERE id=?',
                             (long, 'failed', 'long-' + str(i)))
        prompt = repair_prompt.generate(self.con, iid)['prompt']
        self.assertLessEqual(len(prompt), 12000)
        import json
        evidence = json.loads(prompt.split('```json\n', 1)[1].split('\n```', 1)[0])
        self.assertEqual(len(evidence['related_records']), 5)
        self.assertEqual(len(evidence['related_records'][0]['command']), 500)
        self.assertIn('验收与回报', prompt)

    def test_new_tool_and_skill_kinds_use_target_not_log_as_edit_location(self):
        for kind, phrase in [('skill_repeated_read','重复读取'),
                             ('skill_read_discarded','stdout 重定向'),
                             ('skill_read_failed','退出错误'),
                             ('tool_output_truncated','真实截断标记')]:
            with self.subTest(kind=kind):
                iid=self.issue(kind)
                self.con.execute('''UPDATE issues SET category=?,confidence=?,target_path=?,
                    target_line=?,tool=?,command=? WHERE id=?''',
                    ('skill' if kind.startswith('skill') else 'tool','high',
                     '/tmp/work/SKILL.md',7,'exec_command','cat SKILL.md',iid))
                prompt=repair_prompt.generate(self.con,iid)['prompt']
                self.assertIn(phrase,prompt)
                self.assertIn('"target_path": "/tmp/work/SKILL.md"',prompt)
                self.assertIn('"target_line": 7',prompt)
                self.assertIn('日志 path:line 是取证入口',prompt)

    def test_config_prompt_is_bounded_and_does_not_require_session(self):
        finding={'id':'config:demo','kind':'missing_skill','severity':'warning',
                 'confidence':'high','title':'缺少 skill','evidence':'路径不存在',
                 'suggestion':'核对路径','target_path':'/tmp/work/SKILL.md',
                 'target_line':4,'details':{'token':'secret'}}
        result=repair_prompt.generate_config(finding)
        self.assertEqual(result['issue_id'],'config:demo')
        self.assertIn('/tmp/work/SKILL.md',result['prompt'])
        self.assertNotIn('secret',result['prompt'])
        self.assertIn('不要自动 commit、push 或部署',result['prompt'])

    def test_native_event_evidence_uses_only_issue_event_ids(self):
        iid=self.issue('skill_read_failed')
        self.con.execute('UPDATE issues SET details_json=? WHERE id=?',
                         (json.dumps({'event_ids':['event-own']}),iid))
        for event_id, command in [('event-own','cat SKILL.md'),
                                  ('event-other','unrelated-secret')]:
            self.con.execute('''INSERT INTO tool_events(id,session_id,turn_id,timestamp,
                tool,command,target_paths,skill_paths,status,exit_code,path,line)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (event_id,self.sid,'turn-1','2026-09-23T00:00:01Z',
                 'exec_command',command,'["/tmp/work/SKILL.md"]',
                 '["/tmp/work/SKILL.md"]','failed',1,'/tmp/log.jsonl',20))
        prompt=repair_prompt.generate(self.con,iid)['prompt']
        self.assertIn('cat SKILL.md',prompt)
        self.assertNotIn('unrelated-secret',prompt)
        self.assertIn('native_tool_events',prompt)


if __name__ == '__main__':
    unittest.main()
