"""Command line and local service lifecycle for Codex Session Doctor."""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from . import __version__, scanner, server, store


SERVICE = 'codex-session-doctor'


def default_data_dir():
    configured = os.environ.get('CODEX_DOCTOR_DATA_DIR')
    if configured:
        return Path(configured).expanduser().resolve()
    state_home = os.environ.get('XDG_STATE_HOME')
    if state_home:
        return (Path(state_home).expanduser() / SERVICE).resolve()
    return (Path.home() / '.local/state' / SERVICE).resolve()


def health(port):
    try:
        with urlopen(f'http://127.0.0.1:{port}/api/health', timeout=0.35) as response:
            if response.status != 200:
                return None
            result = json.load(response)
            return result if isinstance(result, dict) else None
    except (HTTPError, URLError, OSError, ValueError):
        return None


def _record(data_dir):
    try:
        record = json.loads((data_dir / 'doctor.pid').read_text())
        if (isinstance(record.get('pid'), int) and record['pid'] > 0
                and isinstance(record.get('port'), int) and 0 < record['port'] < 65536):
            return record
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def _running(data_dir):
    record = _record(data_dir)
    if not record:
        return None
    info = health(record['port'])
    if (info and info.get('service') == SERVICE
            and info.get('pid') == record['pid']
            and info.get('data_dir') == str(data_dir)
            and (record.get('group_id') is None
                 or _in_process_group(record['pid'], record['group_id']))):
        return record
    return None


def _in_process_group(pid, group_id):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        return os.getpgid(pid) == group_id
    except (OSError, ValueError):
        return False


def _write_record(data_dir, pid, port, group_id):
    target = data_dir / 'doctor.pid'
    temporary = data_dir / f'.doctor.pid.{os.getpid()}'
    temporary.write_text(json.dumps({'pid': pid, 'port': port, 'group_id': group_id}))
    temporary.replace(target)


def _child_command(args):
    if getattr(sys, 'frozen', False):
        command = [sys.executable]
    else:
        command = [sys.executable, str(Path(__file__).resolve().parent.parent / 'doctor.py')]
    command.extend(['serve', '--data-dir', str(args.data_dir), '--port', str(args.port)])
    for root in args.roots:
        command.extend(['--codex-home', root])
    return command


def start(args):
    args.data_dir.mkdir(parents=True, exist_ok=True)
    existing = _running(args.data_dir)
    if existing:
        print(f"运行中 PID {existing['pid']}，http://127.0.0.1:{existing['port']}")
        if args.open_browser:
            webbrowser.open(f"http://127.0.0.1:{existing['port']}")
        return 0
    if health(args.port):
        print(f'端口 {args.port} 已被占用', file=sys.stderr)
        return 1
    env = os.environ.copy()
    if getattr(sys, 'frozen', False):
        env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    with (args.data_dir / 'server.log').open('a') as log:
        child = subprocess.Popen(_child_command(args), stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True, env=env)
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        info = health(args.port)
        if info:
            service_pid = info.get('pid')
            if (info.get('service') == SERVICE
                    and info.get('data_dir') == str(args.data_dir)
                    and _in_process_group(service_pid, child.pid)):
                _write_record(args.data_dir, service_pid, args.port, child.pid)
                print(f'已启动 PID {service_pid}，http://127.0.0.1:{args.port}')
                if args.open_browser:
                    webbrowser.open(f'http://127.0.0.1:{args.port}')
                return 0
            break
        if child.poll() is not None:
            break
        time.sleep(0.1)
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
    print(f'启动失败，查看 {args.data_dir / "server.log"}', file=sys.stderr)
    return 1


def stop(args):
    running = _running(args.data_dir)
    if not running:
        print('未运行或服务身份无法确认', file=sys.stderr)
        return 1
    os.kill(running['pid'], signal.SIGTERM)
    for _ in range(30):
        if not _running(args.data_dir):
            break
        time.sleep(0.1)
    else:
        print('停止超时', file=sys.stderr)
        return 1
    (args.data_dir / 'doctor.pid').unlink(missing_ok=True)
    print('已停止')
    return 0


def status(args):
    running = _running(args.data_dir)
    if running:
        print(f"运行中 PID {running['pid']}，http://127.0.0.1:{running['port']}")
        return 0
    print('未运行或状态未知')
    return 1


def scan(args):
    result = scanner.scan(args.roots, str(args.data_dir))
    con = store.connect(args.data_dir)
    try:
        overview = store.overview(con, 0, args.roots)
        result['overview'] = overview['totals']
        result['coverage'] = overview['coverage']
    finally:
        con.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='本地 Codex 会话诊断')
    parser.add_argument('--version', action='version', version=f'codex-doctor {__version__}')
    parser.add_argument('command', choices=['scan', 'serve', 'start', 'stop', 'status', 'open'])
    parser.add_argument('--codex-home', action='append', dest='roots')
    parser.add_argument('--data-dir')
    parser.add_argument('--port', type=int, default=8768)
    parser.add_argument('--open', action='store_true', dest='open_browser')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('--port 必须在 1..65535 之间')
    if args.open_browser and args.command != 'start':
        parser.error('--open 仅用于 start')
    args.data_dir = Path(args.data_dir).expanduser().resolve() if args.data_dir else default_data_dir()
    args.roots = [str(Path(root).expanduser().resolve()) for root in
                  (args.roots or [os.environ.get('CODEX_HOME', '~/.codex')])]
    if args.command == 'scan':
        return scan(args)
    if args.command == 'serve':
        try:
            server.serve(str(args.data_dir), args.roots, args.port)
        except OSError as exc:
            print(f'服务启动失败：{exc}', file=sys.stderr)
            return 1
        return 0
    if args.command == 'start':
        return start(args)
    if args.command == 'stop':
        return stop(args)
    if args.command == 'status':
        return status(args)
    running = _running(args.data_dir)
    if not running:
        print('未运行或服务身份无法确认', file=sys.stderr)
        return 1
    webbrowser.open(f"http://127.0.0.1:{running['port']}")
    return 0
