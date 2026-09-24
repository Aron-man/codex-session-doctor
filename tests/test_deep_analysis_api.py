import json
import signal
import unittest
from unittest import mock

from session_doctor import server


class FakeAnalysis:
    def __init__(self):
        self.calls = []

    def preview(self, session_id, focus='', include_children=True):
        self.calls.append(('preview', session_id, focus, include_children))
        return {'preview_id': 'preview-1', 'prompt': 'inspect'}

    def latest(self, session_id):
        self.calls.append(('latest', session_id))
        return None

    def get(self, job_id):
        self.calls.append(('get', job_id))
        if not job_id:
            raise ValueError('missing job')
        return self.job(job_id, 'failed' if job_id == 'failed' else 'completed')

    @staticmethod
    def job(job_id, status):
        return {'id': job_id, 'session_id': 's', 'status': status,
                'created_at': '2026-09-24T00:00:00Z', 'finished_at': '2026-09-24T00:01:00Z',
                'model': 'gpt-6-sol', 'effort': 'medium',
                'usage': {'input_tokens': 12, 'cached_input_tokens': 2, 'output_tokens': 4},
                'result': {'summary': 'done', 'findings': [], 'uncertainties': []} if status == 'completed' else None,
                'error': 'runner failed' if status == 'failed' else None,
                'evidence': {'events': []}, 'guidance': [], 'reused': False}

    def repair_prompt(self, job_id, index):
        self.calls.append(('repair_prompt', job_id, index))
        return {'title': 'repair', 'prompt': 'review'}

    def start(self, preview_id):
        self.calls.append(('start', preview_id))
        if preview_id == 'failed':
            return self.job('failed', 'failed')
        return self.job('job-1', 'queued')

    def cancel(self, job_id):
        self.calls.append(('cancel', job_id))
        return {'id': job_id, 'status': 'cancelled'}

    def shutdown(self):
        self.calls.append(('shutdown',))


class DeepAnalysisApiTest(unittest.TestCase):
    def setUp(self):
        self.service = FakeAnalysis()

    def api(self, path, query):
        return server.api(path, query, '/unused', [], analysis_service=self.service)

    def test_get_routes_do_not_start_model(self):
        self.assertEqual(self.api('/api/deep-analysis/preview', {'session_id': ['s'], 'focus': ['drift'], 'include_children': ['0']})['preview_id'], 'preview-1')
        self.assertEqual(self.api('/api/deep-analysis/latest', {'session_id': ['s']}), {'job': None})
        self.assertEqual(self.api('/api/deep-analysis', {'id': ['job-1']})['status'], 'completed')
        self.assertEqual(self.api('/api/deep-analysis/prompt', {'id': ['job-1'], 'index': ['2']})['prompt'], 'review')
        self.assertEqual(self.service.calls, [('preview', 's', 'drift', False), ('latest', 's'), ('get', 'job-1'), ('repair_prompt', 'job-1', 2)])
        self.assertIn('error', self.api('/api/deep-analysis/preview', {}))
        self.assertIn('error', self.api('/api/deep-analysis/prompt', {'index': ['bad']}))
        self.assertEqual(server.api('/api/deep-analysis/latest', {'session_id': ['s']}, '/unused', []),
                         {'error': '深度诊断服务未初始化'})

    def test_post_routes_and_request_restrictions(self):
        headers = {'Host': '127.0.0.1:8768', 'Origin': 'http://127.0.0.1:8768', 'Content-Type': 'application/json; charset=utf-8'}
        request = lambda path, data, headers=headers: server._post_request(path, headers, json.dumps(data).encode(), self.service)
        self.assertEqual(request('/api/deep-analysis', {'preview_id': 'preview-1'})[1]['status'], 'queued')
        self.assertEqual(request('/api/deep-analysis/cancel', {'id': 'job-1'})[1]['status'], 'cancelled')
        self.assertEqual(self.service.calls, [('start', 'preview-1'), ('cancel', 'job-1')])
        self.assertEqual(request('/api/deep-analysis', {}, headers | {'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(request('/api/deep-analysis', {}, headers | {'Origin': 'http://localhost:8768'})[0], 403)
        self.assertEqual(request('/api/deep-analysis', {}, headers | {'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(server._post_request('/api/deep-analysis', headers, b'x' * 8193, self.service)[0], 413)
        self.assertEqual(server._post_request('/api/deep-analysis', headers, b'{', self.service)[0], 400)
        self.assertEqual(request('/api/deep-analysis', {})[0], 400)
        self.assertEqual(request('/api/deep-analysis/other', {'preview_id': 'x'})[0], 404)
        self.assertEqual(self.service.calls, [('start', 'preview-1'), ('cancel', 'job-1')])

    def test_job_error_field_is_not_api_error(self):
        for job_id in ('job-1', 'failed'):
            job = self.api('/api/deep-analysis', {'id': [job_id]})
            self.assertFalse(server._error_envelope(job))
            self.assertEqual(job['error'], 'runner failed' if job_id == 'failed' else None)
        headers = {'Host': '127.0.0.1:8768', 'Origin': 'http://127.0.0.1:8768', 'Content-Type': 'application/json'}
        status, job = server._post_request('/api/deep-analysis', headers, b'{"preview_id":"failed"}', self.service)
        self.assertEqual(status, 200)
        self.assertEqual(job['status'], 'failed')
        self.assertTrue(server._error_envelope({'error': 'bad request'}))

    def test_serve_cleanup_on_normal_exit_and_sigterm(self):
        previous = signal.getsignal(signal.SIGTERM)
        for mode in ('normal', 'sigterm'):
            with self.subTest(mode=mode):
                events = []
                self.service.calls.clear()

                class FakeHttp:
                    def __init__(self, *_args):
                        pass

                    def serve_forever(self):
                        events.append('serve')
                        if mode == 'sigterm':
                            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

                    def server_close(self):
                        events.append('close')

                with mock.patch.object(server, '_analysis_service', return_value=self.service), \
                     mock.patch.object(server, '_config_audit', return_value=object()), \
                     mock.patch.object(server.quota, 'QuotaCache', return_value=mock.Mock(run=lambda: None)), \
                     mock.patch.object(server, 'ThreadingHTTPServer', FakeHttp), \
                     mock.patch.object(server.threading, 'Thread'):
                    if mode == 'sigterm':
                        with self.assertRaises(SystemExit):
                            server.serve('/unused', ['/unused'])
                    else:
                        server.serve('/unused', ['/unused'])
                self.assertEqual(self.service.calls, [('shutdown',)])
                self.assertEqual(events, ['serve', 'close'])
                self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_serve_non_main_thread_skips_signal_registration(self):
        self.service.calls.clear()

        class FakeHttp:
            def __init__(self, *_args):
                pass

            def serve_forever(self):
                pass

            def server_close(self):
                pass

        with mock.patch.object(server, '_analysis_service', return_value=self.service), \
             mock.patch.object(server, '_config_audit', return_value=object()), \
             mock.patch.object(server.quota, 'QuotaCache', return_value=mock.Mock(run=lambda: None)), \
             mock.patch.object(server, 'ThreadingHTTPServer', FakeHttp), \
             mock.patch.object(server.threading, 'Thread'), \
             mock.patch.object(server.threading, 'current_thread', return_value=object()), \
             mock.patch.object(server.signal, 'signal') as set_signal:
            server.serve('/unused', ['/unused'])
        set_signal.assert_not_called()
        self.assertEqual(self.service.calls, [('shutdown',)])


if __name__ == '__main__':
    unittest.main()
