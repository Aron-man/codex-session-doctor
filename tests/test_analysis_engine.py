import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from session_doctor.analysis_engine import AnalysisService, RESULT_SCHEMA, validate_result


EVIDENCE = {
    'schema_version': 'analysis-evidence-v1', 'session_id': 'session-1',
    'title': 'test', 'focus': '', 'generated_at': '2026-09-24T00:00:00Z',
    'scope': {'session_ids': ['session-1'], 'included_sessions': 1, 'total_sessions': 1},
    'coverage': {'partial': False, 'notes': [], 'scanned_files': 1,
                 'selected_events': 1, 'omitted_events': 0},
    'metrics': {'scope': 'included_sessions_history', 'input': 1, 'cached': 0,
                'output': 1, 'reasoning': 0, 'total': 2, 'requests': 1},
    'events': [{'id': 'E1', 'session_id': 'session-1', 'turn_id': None,
                'timestamp': '2026-09-24T00:00:00Z', 'kind': 'user',
                'text': 'Do the task', 'source': {'path': '/tmp/test.jsonl', 'line': 1},
                'details': {}}],
}

RESULT = {
    'summary': 'One grounded issue', 'findings': [{
        'category': 'goal_drift', 'title': 'Changed goal', 'status': 'open',
        'severity': 'medium', 'confidence': 'medium', 'goal': 'Do the task',
        'problem': 'The response changed the task', 'evidence_refs': ['E1'],
        'counterevidence_refs': [],
        'recommendation': {'action': 'Check scope', 'reason': 'Match the request',
                           'alternative': 'Ask once', 'tradeoff': 'Costs a turn',
                           'validation': 'Compare result to request',
                           'principle_refs': ['goal_scope']},
        'limitations': []}], 'uncertainties': []}


class BlockingStdin:
    def __init__(self, release):
        self.release = release
        self.entered = threading.Event()

    def write(self, _data):
        self.entered.set()
        self.release.wait(2)

    def close(self):
        pass


class StubProcess:
    def __init__(self, release):
        self.pid = 12345
        self.stdin = BlockingStdin(release)
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = -15
        self.stdin.release.set()
        return self.returncode


class AnalysisEngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        (self.data_dir / 'doctor.sqlite3').touch()
        patcher = patch('session_doctor.analysis_evidence.build_evidence',
                        side_effect=lambda *_args: copy.deepcopy(EVIDENCE))
        patcher.start()
        self.addCleanup(patcher.stop)

    def service(self, runner):
        return AnalysisService(self.data_dir, [self.tmp.name], runner=runner)

    def await_status(self, service, job_id, statuses=('completed', 'failed', 'cancelled')):
        until = time.monotonic() + 3
        while time.monotonic() < until:
            job = service.get(job_id)
            if job['status'] in statuses:
                return job
            time.sleep(.01)
        self.fail('job did not finish')

    def test_preview_success_usage_reuse_and_restart(self):
        calls = []

        def runner(prompt, directory, cancel):
            calls.append((prompt, directory, cancel))
            return copy.deepcopy(RESULT), {'input_tokens': 10,
                                           'cached_input_tokens': 2, 'output_tokens': 3}

        service = self.service(runner)
        preview = service.preview('session-1')
        self.assertEqual([], calls)
        self.assertEqual('medium', preview['effort'])
        self.assertIn('不是具体专家', preview['prompt'])
        first = service.start(preview['preview_id'])
        completed = self.await_status(service, first['id'])
        self.assertEqual('completed', completed['status'])
        self.assertEqual(10, completed['usage']['input_tokens'])
        self.assertEqual(1, len(calls))
        again = service.preview('session-1')
        reused = service.start(again['preview_id'])
        self.assertEqual(first['id'], reused['id'])
        self.assertTrue(reused['reused'])
        self.assertEqual(1, len(calls))
        self.assertEqual(first['id'], self.service(runner).latest('session-1')['id'])
        self.assertIn('Changed goal', service.repair_prompt(first['id'], 0)['prompt'])

    def test_invalid_structure_and_unknown_references_fail(self):
        for mutation in ('extra', 'evidence', 'principle', 'missing_principle', 'enum'):
            with self.subTest(mutation=mutation):
                result = copy.deepcopy(RESULT)
                finding = result['findings'][0]
                if mutation == 'extra':
                    finding['invented'] = 'x'
                elif mutation == 'evidence':
                    finding['evidence_refs'] = ['E999']
                elif mutation == 'principle':
                    finding['recommendation']['principle_refs'] = ['made_up']
                elif mutation == 'missing_principle':
                    finding['recommendation']['principle_refs'] = []
                else:
                    finding['status'] = 'fixed'
                with self.assertRaises(ValueError):
                    validate_result(result, EVIDENCE)
        self.assertEqual(1, RESULT_SCHEMA['properties']['findings']['items']
                         ['properties']['recommendation']['properties']
                         ['principle_refs']['minItems'])

    def test_invalid_result_retains_known_usage(self):
        result = copy.deepcopy(RESULT)
        result['findings'][0]['evidence_refs'] = ['unknown']
        usage = {'input_tokens': 91, 'cached_input_tokens': 10, 'output_tokens': 8}
        service = self.service(lambda *_: (result, usage))
        job_id = service.start(service.preview('session-1')['preview_id'])['id']
        job = self.await_status(service, job_id)
        self.assertEqual('failed', job['status'])
        self.assertEqual(usage, job['usage'])
        self.assertIsNone(job['result'])
        self.assertEqual(usage, self.service(lambda *_: None).get(job_id)['usage'])

    def test_estimate_distinguishes_unicode_and_excludes_cli_overhead(self):
        service = self.service(lambda *_: None)
        preview = service.preview('session-1')
        prompt = preview['prompt']
        ascii_chars = sum(ord(char) < 128 for char in prompt)
        self.assertEqual((ascii_chars + 3) // 4 + len(prompt) - ascii_chars,
                         preview['estimate']['approx_input_tokens'])
        self.assertIn('Codex CLI 系统指令', preview['estimate']['note'])

    def test_failed_job_can_be_retried_and_missing_usage_is_null(self):
        count = 0

        def runner(*_):
            nonlocal count
            count += 1
            if count == 1:
                raise TimeoutError('diagnosis timed out')
            return copy.deepcopy(RESULT), None

        service = self.service(runner)
        preview = service.preview('session-1')
        first = service.start(preview['preview_id'])
        self.assertEqual('failed', self.await_status(service, first['id'])['status'])
        second = service.start(preview['preview_id'])
        self.assertNotEqual(first['id'], second['id'])
        self.assertIsNone(self.await_status(service, second['id'])['usage'])

    def test_cancel_and_one_running_job(self):
        entered = threading.Event()
        release = threading.Event()

        def runner(_prompt, _directory, cancelled):
            entered.set()
            release.wait(2)
            if cancelled.is_set():
                raise RuntimeError('cancelled')
            return copy.deepcopy(RESULT), None

        service = self.service(runner)
        preview = service.preview('session-1')
        first = service.start(preview['preview_id'])
        self.assertTrue(entered.wait(1))
        self.assertEqual(first['id'], service.start(preview['preview_id'])['id'])
        different = service.preview('session-1', focus='different')
        different['evidence']['focus'] = 'different'
        service._previews[different['preview_id']]['evidence']['focus'] = 'different'
        with self.assertRaises(ValueError):
            service.start(different['preview_id'])
        self.assertEqual('cancelled', service.cancel(first['id'])['status'])
        with self.assertRaises(ValueError):
            service.start(different['preview_id'])
        release.set()
        self.assertEqual('cancelled', self.await_status(service, first['id'])['status'])

    def test_interrupted_job_is_not_replayed(self):
        service = self.service(lambda *_: None)
        directory = service.jobs_dir / 'old-job'
        directory.mkdir()
        job = {'id': 'old-job', 'session_id': 'session-1', 'status': 'running',
               'created_at': '2026-09-24T00:00:00Z', 'finished_at': None,
               'model': 'gpt-6-sol', 'effort': 'medium', 'usage': None,
               'result': None, 'error': None, 'evidence': EVIDENCE,
               'guidance': [], '_fingerprint': 'x'}
        (directory / 'job.json').write_text(json.dumps(job))
        restarted = self.service(lambda *_: self.fail('must not run'))
        self.assertEqual('interrupted', restarted.get('old-job')['status'])
        self.assertEqual('interrupted', json.loads((directory / 'job.json').read_text())['status'])

    def test_default_runner_timeout_during_stdin_write(self):
        release = threading.Event()
        process = StubProcess(release)
        service = AnalysisService(self.data_dir, [self.tmp.name])
        directory = service.jobs_dir / 'stub-timeout'
        directory.mkdir()
        killed = []

        def killpg(pid, sig):
            killed.append((pid, sig))
            process.wait()

        with patch('session_doctor.analysis_engine.subprocess.Popen', return_value=process) as popen, \
             patch('session_doctor.analysis_engine.os.killpg', side_effect=killpg), \
             patch('session_doctor.analysis_engine.TIMEOUT_SECONDS', .03), \
             patch('session_doctor.analysis_engine.POLL_SECONDS', .005):
            with self.assertRaisesRegex(TimeoutError, '诊断超过'):
                service._codex_runner('large prompt', directory, threading.Event())
        self.assertTrue(process.stdin.entered.is_set())
        self.assertTrue(killed)
        args = popen.call_args.args[0]
        self.assertIn('--enable', args)
        self.assertIn('skip_host_skill_discovery', args)
        for feature in ('plugins', 'remote_plugin', 'multi_agent', 'shell_tool',
                        'hooks', 'skill_search', 'skill_mcp_dependency_install', 'shell_snapshot'):
            self.assertIn(('--disable', feature), list(zip(args, args[1:])))

    def test_default_runner_cancel_during_stdin_write(self):
        release = threading.Event()
        process = StubProcess(release)
        service = AnalysisService(self.data_dir, [self.tmp.name])
        directory = service.jobs_dir / 'stub-cancel'
        directory.mkdir()
        cancelled = threading.Event()
        killed = []

        def killpg(pid, sig):
            killed.append((pid, sig))
            process.wait()

        def do_cancel():
            self.assertTrue(process.stdin.entered.wait(1))
            cancelled.set()

        trigger = threading.Thread(target=do_cancel)
        trigger.start()
        with patch('session_doctor.analysis_engine.subprocess.Popen', return_value=process), \
             patch('session_doctor.analysis_engine.os.killpg', side_effect=killpg), \
             patch('session_doctor.analysis_engine.POLL_SECONDS', .005):
            with self.assertRaisesRegex(RuntimeError, '诊断已取消'):
                service._codex_runner('large prompt', directory, cancelled)
        trigger.join(1)
        self.assertTrue(killed)

    def test_default_runner_enforces_output_limit(self):
        release = threading.Event()
        process = StubProcess(release)
        service = AnalysisService(self.data_dir, [self.tmp.name])
        directory = service.jobs_dir / 'stub-output'
        directory.mkdir()
        killed = []

        def popen(_args, **kwargs):
            kwargs['stdout'].write(b'x' * 33)
            kwargs['stdout'].flush()
            return process

        def killpg(pid, sig):
            killed.append((pid, sig))
            process.wait()

        with patch('session_doctor.analysis_engine.subprocess.Popen', side_effect=popen), \
             patch('session_doctor.analysis_engine.os.killpg', side_effect=killpg), \
             patch('session_doctor.analysis_engine.OUTPUT_LIMIT_BYTES', 32), \
             patch('session_doctor.analysis_engine.POLL_SECONDS', .005):
            with self.assertRaisesRegex(ValueError, '超过 2MiB'):
                service._codex_runner('prompt', directory, threading.Event())
        self.assertTrue(killed)

    def test_shutdown_cancels_and_reaps_own_subprocess(self):
        release = threading.Event()
        process = StubProcess(release)
        service = AnalysisService(self.data_dir, [self.tmp.name])
        killed = []

        def killpg(pid, sig):
            killed.append((pid, sig))
            process.wait()

        with patch('session_doctor.analysis_engine.subprocess.Popen', return_value=process), \
             patch('session_doctor.analysis_engine.os.killpg', side_effect=killpg), \
             patch('session_doctor.analysis_engine.POLL_SECONDS', .005):
            job_id = service.start(service.preview('session-1')['preview_id'])['id']
            self.assertTrue(process.stdin.entered.wait(1))
            service.shutdown()
        self.assertTrue(killed)
        self.assertEqual(-15, process.returncode)
        self.assertEqual('cancelled', service.get(job_id)['status'])
        self.assertEqual('cancelled', json.loads(
            (service.jobs_dir / job_id / 'job.json').read_text())['status'])
        self.assertFalse(service._threads)
        with self.assertRaisesRegex(ValueError, '已停止'):
            service.start(service.preview('session-1')['preview_id'])


if __name__ == '__main__':
    unittest.main()
