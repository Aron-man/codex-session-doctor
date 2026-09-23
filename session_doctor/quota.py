"""Read the signed-in Codex account's weekly limit from the local app server."""

import datetime as dt
import json
import math
import os
import select
import subprocess
import threading
import time


SOURCE = 'codex_app_server'
INTERVAL = 60
WEEK_MINUTES = 10080


def weekly_limit(result):
    limits = result.get('rateLimitsByLimitId')
    if isinstance(limits, dict):
        limits = limits.get('codex')
    elif limits is None:
        limits = result.get('rateLimits')
    else:
        return None
    if not isinstance(limits, dict):
        return None
    for name in ('primary', 'secondary'):
        window = limits.get(name)
        if not isinstance(window, dict) or window.get('windowDurationMins') != WEEK_MINUTES:
            continue
        used = window.get('usedPercent')
        if not isinstance(used, (int, float)) or isinstance(used, bool) or not math.isfinite(used):
            used = None
        reset = window.get('resetsAt')
        if not isinstance(reset, (int, float)) or isinstance(reset, bool) or not math.isfinite(reset):
            reset = None
        return {
            'used_percent': used,
            'remaining_percent': max(0, min(100, 100-used)) if used is not None else None,
            'resets_at': reset,
            'window_minutes': WEEK_MINUTES,
        }
    return None


def read_weekly(codex_home, timeout=8):
    """One short JSONL RPC session; no credentials or raw process output escape."""
    env = os.environ.copy()
    env['CODEX_HOME'] = str(codex_home)
    if 'LD_LIBRARY_PATH_ORIG' in env:
        env['LD_LIBRARY_PATH'] = env.pop('LD_LIBRARY_PATH_ORIG')
    elif getattr(__import__('sys'), 'frozen', False):
        env.pop('LD_LIBRARY_PATH', None)
    try:
        proc = subprocess.Popen(
            ['codex', 'app-server', '--stdio'], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, bufsize=0,
        )
    except FileNotFoundError as exc:
        raise RuntimeError('本机未找到 Codex CLI') from exc
    deadline = time.monotonic() + timeout
    buffer = b''

    def send(message):
        proc.stdin.write((json.dumps(message, separators=(',', ':'))+'\n').encode())
        proc.stdin.flush()

    def receive(request_id):
        nonlocal buffer
        while True:
            while b'\n' in buffer:
                raw, buffer = buffer.split(b'\n', 1)
                try:
                    message = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if message.get('id') == request_id:
                    if 'error' in message:
                        raise RuntimeError('Codex 额度读取失败，请检查登录状态')
                    return message.get('result')
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Codex 额度读取超时')
            ready, _, _ = select.select([proc.stdout], [], [], remaining)
            if not ready:
                raise TimeoutError('Codex 额度读取超时')
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError('Codex app-server 未返回额度，请检查登录状态')
            buffer += chunk
            if len(buffer) > 1024*1024:
                raise RuntimeError('Codex app-server 响应过大')

    try:
        send({'jsonrpc':'2.0','id':1,'method':'initialize','params':{
            'clientInfo':{'name':'codex_session_doctor','title':'Codex Session Doctor','version':'0.1.0'}}})
        receive(1)
        send({'jsonrpc':'2.0','method':'initialized','params':{}})
        send({'jsonrpc':'2.0','id':2,'method':'account/rateLimits/read','params':{}})
        result = receive(2)
        return weekly_limit(result if isinstance(result, dict) else {})
    except TimeoutError:
        raise
    except OSError as exc:
        raise RuntimeError('Codex app-server 通信失败') from exc
    finally:
        proc.terminate() if proc.poll() is None else None
        try:
            proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()


class QuotaCache:
    def __init__(self, codex_home, reader=read_weekly):
        self.codex_home = codex_home
        self.reader = reader
        self.lock = threading.Lock()
        self.refresh_lock = threading.Lock()
        self.state = self._state(error='读取中')

    @staticmethod
    def _state(weekly=None, updated_at=None, stale=False, error=None):
        return {'available': weekly is not None, 'weekly': weekly,
                'updated_at': updated_at, 'stale': stale, 'error': error,
                'source': SOURCE, 'refresh_interval_seconds': INTERVAL}

    def snapshot(self):
        with self.lock:
            return {**self.state, 'weekly': dict(self.state['weekly']) if self.state['weekly'] else None}

    def refresh(self):
        if not self.refresh_lock.acquire(blocking=False):
            return False
        try:
            try:
                weekly = self.reader(self.codex_home)
                if weekly is None:
                    raise RuntimeError('当前账号未提供本周额度')
                new = self._state(weekly, dt.datetime.now(dt.timezone.utc).isoformat())
            except Exception as exc:
                old = self.snapshot()
                error = str(exc) if isinstance(exc, (RuntimeError, TimeoutError)) else 'Codex 额度读取失败'
                new = self._state(old['weekly'], old['updated_at'], old['weekly'] is not None, error)
            with self.lock:
                self.state = new
            return True
        finally:
            self.refresh_lock.release()

    def run(self):
        while True:
            self.refresh()
            time.sleep(INTERVAL)
