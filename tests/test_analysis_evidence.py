import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from session_doctor.analysis_evidence import build_evidence
from session_doctor.store import SCHEMA


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'root'
        self.root.mkdir()
        self.con = sqlite3.connect(':memory:')
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        for sid, source, parent in [('main', 'cli', None), ('child', 'subagent', 'main'), ('fork', 'vscode', 'main')]:
            self.con.execute('INSERT INTO sessions(id,title,source,parent_id,created) VALUES(?,?,?,?,?)',
                             (sid, sid, source, parent, '2026-09-20T00:00:00Z'))

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def write(self, sid, rows):
        path = self.root / f'{sid}.jsonl'
        path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding='utf-8')
        self.con.execute('INSERT INTO files(path,dev,ino,size,session_id) VALUES(?,?,?,?,?)',
                         (str(path), 0, 0, path.stat().st_size, sid))
        return path

    def row(self, typ, payload, second):
        return {'type': typ, 'payload': payload, 'timestamp': f'2026-09-20T00:00:{second:02d}Z'}

    def msg(self, role, text, second):
        return self.row('response_item', {'type': 'message', 'role': role,
                                         'content': [{'type': 'input_text' if role == 'user' else 'output_text', 'text': text}]}, second)

    def test_goal_plan_correction_counterevidence_and_read_only(self):
        rows = [self.row('session_meta', {'id': 'main', 'timestamp': '2026-09-20T00:00:00Z'}, 0),
                self.msg('user', '# AGENTS.md instructions', 1),
                self.msg('user', '只需要修复页面按钮', 2),
                self.msg('assistant', '方案：引入新框架重做所有页面', 3)]
        rows += [self.msg('assistant', f'中间普通进度 {i}', 4 + i) for i in range(25)]
        rows += [self.msg('user', '纠正：不要重做所有页面，只修按钮', 40),
                 self.msg('assistant', '收到，已收敛到按钮修改', 41),
                 self.msg('assistant', '反证：已有组件可复用，修复后验证通过', 42)]
        self.write('main', rows)
        self.write('child', [self.row('session_meta', {'id': 'child'}, 0), self.msg('assistant', '子任务结论', 3)])
        self.write('fork', [self.row('session_meta', {'id': 'fork'}, 0), self.msg('assistant', 'fork不应纳入', 3)])
        self.con.execute("INSERT INTO usage(id,session_id,input,cached,output,reasoning,total,requests) VALUES('u','main',10,2,3,1,13,1)")
        before = self.con.total_changes
        evidence = build_evidence(self.con, 'main', [self.root])
        self.assertEqual(self.con.total_changes, before)
        self.assertEqual(evidence['scope']['session_ids'], ['main', 'child'])
        self.assertEqual(evidence['metrics']['total'], 13)
        texts = '\n'.join(x['text'] for x in evidence['events'])
        for expected in ('只需要修复页面按钮', '引入新框架', '纠正', '已收敛', '反证', '子任务结论'):
            self.assertIn(expected, texts)
        self.assertNotIn('fork不应纳入', texts)
        self.assertEqual(len({x['id'] for x in evidence['events']}), len(evidence['events']))
        self.assertTrue(all(x['source']['line'] > 0 for x in evidence['events']))

    def test_native_mirror_mcp_json_and_secrets(self):
        rows = [self.row('session_meta', {'id': 'main'}, 0),
                self.msg('user', '检查接口', 1),
                self.row('event_msg', {'type': 'item_completed', 'item': {'type': 'UserMessage', 'text': '检查接口'}}, 1),
                self.row('event_msg', {'type': 'item_completed', 'item': {
                    'type': 'McpToolCall', 'id': 'call1', 'server': 'demo', 'tool': 'inspect',
                    'arguments': {'Authorization': 'Bearer visible-secret'},
                    'result': {'content': [{'type': 'text', 'text': '{"finding":"结构化结果"}'},
                                           {'type': 'image', 'data': 'SECRET_IMAGE_BYTES', 'mimeType': 'image/png'}]},
                    'status': 'success'}}, 2),
                self.row('event_msg', {'type': 'item_completed', 'item': {
                    'type': 'CommandExecution', 'id': 'call2', 'command': ['mysql', '--password=secret-value', '-p', 'another-secret', '-pjoined-secret'],
                    'parsed_cmd': [{'type': 'read', 'path': 'changed.py'}], 'status': 'failed',
                    'exit_code': 1, 'stderr': 'Authorization: Bearer error-secret'}}, 3)]
        self.write('main', rows)
        data = build_evidence(self.con, 'main', [self.root])
        text = json.dumps(data, ensure_ascii=False)
        self.assertIn('结构化结果', text)
        self.assertIn('changed.py', text)
        for secret in ('visible-secret', 'secret-value', 'another-secret', 'joined-secret', 'error-secret', 'SECRET_IMAGE_BYTES'):
            self.assertNotIn(secret, text)
        self.assertEqual(sum(x['kind'] == 'user' for x in data['events']), 1)

    def test_native_text_turns_context_file_changes_and_timing(self):
        rows = [self.row('session_meta', {'id': 'main'}, 0),
                self.row('turn_context', {'turn_id': 'a'}, 1),
                self.msg('user', '继续', 2),
                self.row('event_msg', {'type': 'item_completed', 'turn_id': 'a', 'item': {
                    'type': 'UserMessage', 'content': [{'type': 'text', 'text': '继续'}]}}, 2),
                self.row('turn_context', {'turn_id': 'b'}, 3),
                self.msg('user', '继续', 4),
                self.msg('user', '<codex_delegation>worker通知</codex_delegation>', 5),
                self.msg('user', '## My request for Codex:\n实现按钮', 6),
                self.row('event_msg', {'type': 'item_completed', 'item': {
                    'type': 'FileChange', 'id': 'f1', 'changes': {'button.py': {'patch': 'secret patch'}}, 'status': 'completed'}}, 7),
                self.row('event_msg', {'type': 'item_completed', 'item': {
                    'type': 'CollabAgentToolCall', 'id': 'c1', 'tool': 'wait', 'agent_thread_id': 'agent-1', 'status': 'completed'}}, 8),
                self.row('event_msg', {'type': 'item_completed', 'item': {'type': 'ContextCompaction', 'id': 'co1'}}, 9),
                self.row('event_msg', {'type': 'task_complete', 'duration_ms': 1234, 'time_to_first_token_ms': 123}, 10),
                self.row('event_msg', {'type': 'item_completed', 'item': {
                    'type': 'CommandExecution', 'id': 'grep', 'command': ['grep', '-p', 'plain-argument'],
                    'status': 'completed'}}, 11)]
        self.write('main', rows)
        data = build_evidence(self.con, 'main', [self.root])
        self.assertEqual([x['text'] for x in data['events'] if x['kind'] == 'user'], ['继续', '继续', '实现按钮'])
        self.assertEqual(sum(x['kind'] == 'context' for x in data['events']), 1)
        text = json.dumps(data, ensure_ascii=False)
        self.assertIn('button.py', text)
        self.assertNotIn('secret patch', text)
        self.assertIn('agent-1', text)
        self.assertIn('1234', text)
        self.assertIn('123', text)
        self.assertIn('-p plain-argument', text)

    def test_late_correction_chain_survives_count_and_char_limits(self):
        rows = [self.row('session_meta', {'id': 'main'}, 0), self.msg('user', '只做原始目标', 1)]
        rows += [self.msg('assistant', f'早期方案 {i} ' + '填充' * 300, i % 60) for i in range(230)]
        rows += [self.msg('assistant', '纠正前公开方案：大量抽象', 56),
                 self.msg('user', '过度设计：生产 MVP 只需要小改', 57),
                 self.msg('assistant', '收敛到最小修改，反证是现有组件可用', 58),
                 self.msg('user', '撤回额外签名验签要求', 59),
                 self.msg('assistant', '按撤回后的目标完成', 59)]
        self.write('main', rows)
        data = build_evidence(self.con, 'main', [self.root], '过度设计 生产 MVP')
        text = '\n'.join(e['text'] for e in data['events'])
        for value in ('只做原始目标', '纠正前公开方案', '过度设计', '收敛到最小修改', '撤回额外签名验签', '按撤回后的目标完成'):
            self.assertIn(value, text)
        self.assertLessEqual(len(json.dumps(data, ensure_ascii=False)), 64000)
        self.assertTrue(data['coverage']['partial'])

    def test_coverage_caps_and_unknown_session(self):
        rows = [self.row('session_meta', {'id': 'main'}, 0)]
        rows += [self.msg('assistant', f'Progress {i} unique', i % 60) for i in range(200)]
        self.write('main', rows)
        data = build_evidence(self.con, 'main', [self.root])
        self.assertLessEqual(len(data['events']), 160)
        self.assertTrue(data['coverage']['partial'])
        self.assertEqual(data['coverage']['omitted_events'], 40)
        self.assertLessEqual(len(json.dumps(data, ensure_ascii=False)), 64000)
        with self.assertRaises(ValueError): build_evidence(self.con, 'missing', [self.root])


if __name__ == '__main__':
    unittest.main()
