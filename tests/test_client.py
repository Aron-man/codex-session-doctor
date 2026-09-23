import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from session_doctor import __version__, client, quota, server


DOCTOR = Path(__file__).resolve().parent.parent / 'doctor.py'


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'empty-codex'
        self.root.mkdir()
        self.data = self.base / 'data'
        self.port = free_port()

    def cli(self, command, *extra):
        return subprocess.run(
            [sys.executable, str(DOCTOR), command, '--data-dir', str(self.data),
             '--codex-home', str(self.root), '--port', str(self.port), *extra],
            capture_output=True, text=True, timeout=45,
        )

    def startup_failure_details(self, result):
        log = self.data / 'server.log'
        if not log.exists():
            return result.stderr
        with log.open('rb') as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 4096))
            tail = stream.read().decode('utf-8', errors='replace')
        return f'{result.stderr}\nserver.log (last 4096 bytes):\n{tail}'

    def test_version_defaults_and_health_without_database(self):
        result = subprocess.run([sys.executable, str(DOCTOR), '--version'],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertIn(__version__, result.stdout)
        with patch.dict(os.environ, {'CODEX_DOCTOR_DATA_DIR': str(self.base / 'override'),
                                     'XDG_STATE_HOME': str(self.base / 'state')}):
            self.assertEqual(client.default_data_dir(), self.base / 'override')
        with patch.dict(os.environ, {'XDG_STATE_HOME': str(self.base / 'state')}, clear=True):
            self.assertEqual(client.default_data_dir(), self.base / 'state/codex-session-doctor')
        health = server.api('/api/health', {}, str(self.data), [str(self.root)])
        self.assertEqual(health['service'], 'codex-session-doctor')
        self.assertEqual(health['data_dir'], str(self.data))
        self.assertFalse(self.data.exists())

    def test_start_status_stop_and_scan_with_empty_root(self):
        self.assertNotEqual(self.cli('status').returncode, 0)
        started = self.cli('start')
        self.assertEqual(started.returncode, 0, self.startup_failure_details(started))
        try:
            info = client.health(self.port)
            self.assertEqual(info['data_dir'], str(self.data))
            self.assertEqual(info['version'], __version__)
            self.assertEqual(self.cli('status').returncode, 0)
            self.assertEqual(self.cli('start').returncode, 0)
            scanned = self.cli('scan')
            self.assertEqual(scanned.returncode, 0, scanned.stderr)
            self.assertIn('overview', json.loads(scanned.stdout))
            self.assertTrue((self.data / 'server.log').exists())
        finally:
            stopped = self.cli('stop')
            self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertIsNone(client.health(self.port))
        self.assertNotEqual(self.cli('status').returncode, 0)

    def test_foreign_port_and_stale_pid_never_get_stopped(self):
        with socket.socket() as blocker:
            blocker.bind(('127.0.0.1', self.port))
            blocker.listen()
            self.data.mkdir()
            (self.data / 'doctor.pid').write_text(json.dumps({'pid': os.getpid(), 'port': self.port}))
            self.assertNotEqual(self.cli('status').returncode, 0)
            self.assertNotEqual(self.cli('stop').returncode, 0)
            self.assertNotEqual(self.cli('start').returncode, 0)
            self.assertNotEqual(self.cli('open').returncode, 0)
            self.assertEqual(blocker.getsockname()[1], self.port)

    def test_frozen_bootloader_child_pid_and_foreign_process_group(self):
        args = Mock(data_dir=self.data, port=self.port, roots=[str(self.root)],
                    open_browser=False)
        service = {'service': client.SERVICE, 'pid': 42001,
                   'data_dir': str(self.data)}
        bootloader = Mock(pid=42000)
        bootloader.poll.return_value = None
        with patch.object(client.sys, 'frozen', True, create=True), \
                patch.object(client.subprocess, 'Popen', return_value=bootloader) as popen, \
                patch.object(client, 'health', side_effect=[None, service]), \
                patch.object(client.os, 'getpgid', return_value=bootloader.pid):
            self.assertEqual(client.start(args), 0)
        self.assertEqual(json.loads((self.data / 'doctor.pid').read_text()),
                         {'pid': service['pid'], 'port': self.port,
                          'group_id': bootloader.pid})
        self.assertEqual(popen.call_args.kwargs['env']['PYINSTALLER_RESET_ENVIRONMENT'], '1')
        with patch.object(client, 'health', return_value=service), \
                patch.object(client.os, 'getpgid', return_value=bootloader.pid):
            self.assertEqual(client._running(self.data)['pid'], service['pid'])
        with patch.object(client, 'health', return_value=service), \
                patch.object(client.os, 'getpgid', return_value=99999):
            self.assertIsNone(client._running(self.data))

        (self.data / 'doctor.pid').unlink()
        foreign = Mock(pid=43000)
        foreign.poll.return_value = None
        with patch.object(client.subprocess, 'Popen', return_value=foreign), \
                patch.object(client, 'health', side_effect=[None, service]), \
                patch.object(client.os, 'getpgid', return_value=99999):
            self.assertEqual(client.start(args), 1)
        self.assertFalse((self.data / 'doctor.pid').exists())
        foreign.terminate.assert_called_once()

    def test_start_accepts_service_ready_after_six_seconds(self):
        args = Mock(data_dir=self.data, port=self.port, roots=[str(self.root)],
                    open_browser=False)
        service = {'service': client.SERVICE, 'pid': 42001,
                   'data_dir': str(self.data)}
        child = Mock(pid=42000)
        child.poll.return_value = None
        now = [0]
        def sleep(_):
            now[0] += 1
        with patch.object(client.subprocess, 'Popen', return_value=child), \
                patch.object(client, 'health', side_effect=[None] * 9 + [service]), \
                patch.object(client.os, 'getpgid', return_value=child.pid), \
                patch.object(client.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(client.time, 'sleep', side_effect=sleep):
            self.assertEqual(client.start(args), 0)
        self.assertGreater(now[0], 6)
        self.assertLess(now[0], client.START_TIMEOUT_SECONDS)
        self.assertEqual(json.loads((self.data / 'doctor.pid').read_text())['pid'], service['pid'])

    def test_start_failure_identifies_exit_timeout_and_wrong_identity(self):
        args = Mock(data_dir=self.data, port=self.port, roots=[str(self.root)],
                    open_browser=False)
        cases = [
            (5, None, '子进程提前退出'),
            (None, None, '30 秒内健康接口未就绪'),
            (None, {'service': client.SERVICE, 'pid': 42001,
                    'data_dir': '/other-data'}, '健康接口身份不匹配'),
        ]
        for exit_code, response, expected in cases:
            with self.subTest(expected=expected):
                child = Mock(pid=42000)
                child.poll.return_value = exit_code
                now = [0]
                def sleep(_):
                    now[0] += 10
                output = io.StringIO()
                with patch.object(client.subprocess, 'Popen', return_value=child), \
                        patch.object(client, 'health', side_effect=[None] + [response] * 10), \
                        patch.object(client.time, 'monotonic', side_effect=lambda: now[0]), \
                        patch.object(client.time, 'sleep', side_effect=sleep), \
                        contextlib.redirect_stderr(output):
                    self.assertEqual(client.start(args), 1)
                self.assertIn(expected, output.getvalue())
                self.assertFalse((self.data / 'doctor.pid').exists())

    def test_codex_child_restores_original_linux_library_path(self):
        with patch.dict(os.environ, {'LD_LIBRARY_PATH': '/packaged',
                                     'LD_LIBRARY_PATH_ORIG': '/system'}):
            with patch.object(quota.subprocess, 'Popen', side_effect=RuntimeError('captured')) as popen:
                with self.assertRaisesRegex(RuntimeError, 'captured'):
                    quota.read_weekly(self.root)
        env = popen.call_args.kwargs['env']
        self.assertEqual(env['LD_LIBRARY_PATH'], '/system')
        self.assertNotIn('LD_LIBRARY_PATH_ORIG', env)


if __name__ == '__main__':
    unittest.main()
