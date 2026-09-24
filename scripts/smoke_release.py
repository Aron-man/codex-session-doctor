#!/usr/bin/env python3
"""Exercise the packaged CLI and deep analysis with synthetic, local-only data."""

import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SESSION_ID = '11111111-1111-1111-1111-111111111111'
USAGE = {'input_tokens': 10, 'cached_input_tokens': 0, 'output_tokens': 2}


def request(root, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode('utf-8')
    headers = {'Origin': root, 'Content-Type': 'application/json'} if data else {}
    with urlopen(Request(root + path, data=data, headers=headers), timeout=3) as response:
        body = response.read()
        assert response.status == 200, (path, response.status)
        return json.loads(body) if path.startswith('/api/') else body


def rollout(codex_home):
    sessions = codex_home / 'sessions'
    sessions.mkdir(parents=True)
    stamp = '2026-09-24T00:00:00Z'
    records = [
        {'type': 'session_meta', 'timestamp': stamp, 'payload': {
            'id': SESSION_ID, 'timestamp': stamp, 'cwd': '/synthetic-project', 'source': 'vscode'}},
        {'type': 'turn_context', 'timestamp': stamp, 'payload': {'turn_id': 'turn-1', 'model': 'smoke-model'}},
        {'type': 'response_item', 'timestamp': stamp, 'payload': {'type': 'message', 'role': 'user',
            'content': [{'type': 'input_text', 'text': 'Inspect this synthetic session.'}]}},
        {'type': 'response_item', 'timestamp': stamp, 'payload': {'type': 'message', 'role': 'assistant',
            'content': [{'type': 'output_text', 'text': 'Synthetic answer.'}]}},
        {'type': 'token_usage_record', 'timestamp': stamp, 'payload': {
            'thread_id': SESSION_ID, 'turn_id': 'turn-1', 'response_id': 'response-1',
            'usage': {**USAGE, 'reasoning_output_tokens': 0, 'total_tokens': 12}}},
    ]
    (sessions / f'rollout-{SESSION_ID}.jsonl').write_text(
        ''.join(json.dumps(record) + '\n' for record in records), encoding='utf-8')


def fake_codex(folder, env):
    stub = folder / 'codex_stub.py'
    stub.write_text('''import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if 'app-server' in args:
    raise SystemExit(1)
if 'exec' not in args:
    raise SystemExit('unexpected codex command')
assert os.environ['CODEX_HOME'] == os.environ['SMOKE_CODEX_HOME']
assert sys.stdin.read()
output = Path(args[args.index('--output-last-message') + 1])
output.write_text(json.dumps({'summary': 'packaged smoke', 'findings': [], 'uncertainties': []}), encoding='utf-8')
with open(os.environ['SMOKE_CODEX_CALLS'], 'a', encoding='utf-8') as calls:
    calls.write('exec\\n')
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 10, 'cached_input_tokens': 0, 'output_tokens': 2}}), flush=True)
''', encoding='utf-8')
    launcher = folder / 'codex'
    launcher.write_text('#!/bin/sh\n'
        'if [ "${LD_LIBRARY_PATH_ORIG+x}" = x ]; then\n'
        '  export LD_LIBRARY_PATH="$LD_LIBRARY_PATH_ORIG"\n'
        'else\n'
        '  unset LD_LIBRARY_PATH\n'
        'fi\n'
        f'exec {shlex.quote(sys.executable)} {shlex.quote(str(stub))} "$@"\n', encoding='utf-8')
    launcher.chmod(0o755)
    env['PATH'] = str(folder) + os.pathsep + env.get('PATH', '')


def run(binary):
    binary = Path(binary).expanduser().resolve(strict=True)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError(f'not an executable file: {binary}')
    version = subprocess.run([str(binary), '--version'], check=True, capture_output=True, text=True)
    assert 'codex-doctor ' in version.stdout, version.stdout
    with tempfile.TemporaryDirectory(prefix='codex-doctor-release-smoke-') as temporary:
        folder = Path(temporary).resolve()
        codex_home = folder / 'empty-codex'
        data_dir = folder / 'state'
        fake_bin = folder / 'bin'
        fake_bin.mkdir()
        calls = folder / 'codex-calls.txt'
        rollout(codex_home)
        env = os.environ.copy()
        env.update(SMOKE_CODEX_HOME=str(codex_home), SMOKE_CODEX_CALLS=str(calls),
                   CODEX_HOME=str(codex_home))
        fake_codex(fake_bin, env)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        common = ['--data-dir', str(data_dir), '--codex-home', str(codex_home), '--port', str(port)]
        root = f'http://127.0.0.1:{port}'
        started = False
        try:
            subprocess.run([str(binary), 'scan', *common], env=env, check=True,
                           capture_output=True, text=True, timeout=30)
            subprocess.run([str(binary), 'start', *common], env=env, check=True,
                           capture_output=True, text=True, timeout=35)
            started = True
            deadline = time.monotonic() + 20
            while True:
                try:
                    health = request(root, '/api/health')
                    break
                except (URLError, TimeoutError):
                    if time.monotonic() >= deadline:
                        raise AssertionError('standalone service did not become ready')
                    time.sleep(0.2)
            assert health['service'] == 'codex-session-doctor', health
            assert health['data_dir'] == str(data_dir), health
            assert b'<html' in request(root, '/').lower()
            assert request(root, '/app.js')
            preview = request(root, '/api/deep-analysis/preview?' + urlencode({
                'session_id': SESSION_ID, 'include_children': '0'}))
            assert preview['preview_id'], preview
            evidence = preview['evidence']
            assert evidence['session_id'] == SESSION_ID, evidence
            assert any(event['kind'] == 'user' for event in evidence['events']), evidence
            assert any(event['kind'] == 'assistant' for event in evidence['events']), evidence
            assert preview['guidance'] and preview['prompt'], preview
            job = request(root, '/api/deep-analysis', {'preview_id': preview['preview_id']})
            job_id = job['id']
            deadline = time.monotonic() + 20
            while True:
                job = request(root, '/api/deep-analysis?' + urlencode({'id': job_id}))
                if job['status'] not in ('queued', 'running'):
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError(f'deep analysis did not finish: {job}')
                time.sleep(0.2)
            assert job['status'] == 'completed', job
            assert job['usage'] == USAGE, job
            assert job['result'] == {'summary': 'packaged smoke', 'findings': [], 'uncertainties': []}, job
            assert job['evidence']['events'] and job['guidance'], job
            reused = request(root, '/api/deep-analysis', {'preview_id': preview['preview_id']})
            assert reused['id'] == job_id and reused['reused'] is True, reused
            assert calls.read_text(encoding='utf-8').splitlines() == ['exec']
        finally:
            if started:
                subprocess.run([str(binary), 'stop', *common], env=env,
                               capture_output=True, text=True, timeout=10, check=True)
    print('standalone release smoke passed')


if __name__ == '__main__':
    if len(sys.argv) != 2 or not Path(sys.argv[1]).is_absolute():
        raise SystemExit('usage: smoke_release.py /absolute/path/to/codex-doctor')
    run(sys.argv[1])
