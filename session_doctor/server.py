import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, quota, quota_estimate, repair_prompt, scanner, store


WEB=Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent))/'web'


def api(path, query, data_dir, roots, quota_cache=None):
    if path=='/api/health':
        return {'service':'codex-session-doctor','version':__version__,
                'pid':os.getpid(),'data_dir':str(Path(data_dir).expanduser().resolve())}
    if path=='/api/quota':
        return quota_cache.snapshot() if quota_cache else quota.QuotaCache(roots[0]).snapshot()
    def integer(name, default, maximum=1000):
        try:
            return max(0,min(maximum,int(query.get(name,[default])[0])))
        except (TypeError,ValueError):
            return default
    con=store.connect(data_dir)
    try:
        days=integer('days',7,36500)
        if path=='/api/overview':
            return store.overview(con,days,roots)
        if path=='/api/sessions':
            return store.sessions(con,days,query.get('q',[''])[0][:200],integer('limit',100,500),integer('offset',0,10000000))
        if path=='/api/issues':
            return store.issues(con,days,integer('limit',100,500),integer('offset',0,10000000))
        if path=='/api/issue-prompt':
            return repair_prompt.generate(con,query.get('id',[''])[0])
        if path=='/api/session':
            sid=query.get('id',[''])[0]
            if not sid:
                return {'error':'缺少会话 ID'}
            result=store.detail(con,sid)
            if result:
                snapshot=quota_cache.snapshot() if quota_cache else {'weekly':None,'error':'额度快照尚未取得'}
                result['weekly_quota_estimate']=quota_estimate.estimate(con,sid,roots[0] if roots else '',snapshot)
            return result if result else {'error':'会话不存在'}
        return {'error':'未知接口'}
    finally:
        con.close()


def serve(data_dir, roots, port=8768):
    quota_cache=quota.QuotaCache(roots[0])
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed=urlparse(self.path)
            path=parsed.path
            if path.startswith('/api/'):
                try:
                    result=api(path,parse_qs(parsed.query),data_dir,roots,quota_cache)
                    body=json.dumps(result,ensure_ascii=False).encode()
                    status=400 if path!='/api/quota' and 'error' in result else 200
                except Exception as exc:
                    body=json.dumps({'error':str(exc)},ensure_ascii=False).encode()
                    status=500
                self.send_response(status)
                self.send_header('Content-Type','application/json; charset=utf-8')
            else:
                target={'/':'index.html','/index.html':'index.html','/app.js':'app.js','/style.css':'style.css'}.get(path)
                if not target:
                    self.send_error(404)
                    return
                body=(WEB/target).read_bytes()
                self.send_response(200)
                self.send_header('Content-Type',{'index.html':'text/html; charset=utf-8','app.js':'application/javascript; charset=utf-8','style.css':'text/css; charset=utf-8'}[target])
                self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'")
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    def watcher():
        while True:
            try:
                scanner.scan(roots,data_dir)
            except Exception:
                pass
            time.sleep(5)
    httpd=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    threading.Thread(target=watcher,daemon=True).start()
    threading.Thread(target=quota_cache.run,daemon=True).start()
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
