import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, quota, quota_estimate, repair_prompt, scanner, store


WEB=Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent))/'web'


def _config_audit(roots):
    from .config_audit import ConfigAudit
    return ConfigAudit(roots)


def _analysis_service(data_dir, roots):
    from .analysis_engine import AnalysisService
    return AnalysisService(data_dir, roots)


def api(path, query, data_dir, roots, quota_cache=None, config_cache=None, analysis_service=None):
    if path=='/api/health':
        return {'service':'codex-session-doctor','version':__version__,
                'pid':os.getpid(),'data_dir':str(Path(data_dir).expanduser().resolve())}
    if path=='/api/quota':
        return quota_cache.snapshot() if quota_cache else quota.QuotaCache(roots[0]).snapshot()
    if path=='/api/config-audit':
        return config_cache.snapshot() if config_cache else _config_audit(roots).snapshot()
    if path.startswith('/api/deep-analysis'):
        if analysis_service is None:
            return {'error':'深度诊断服务未初始化'}
        service=analysis_service
        value=lambda name: query.get(name,[''])[0]
        try:
            if path=='/api/deep-analysis/preview':
                if not value('session_id'):
                    raise ValueError('缺少会话 ID')
                return service.preview(value('session_id'),value('focus')[:2000],value('include_children')!='0')
            if path=='/api/deep-analysis/latest':
                if not value('session_id'):
                    raise ValueError('缺少会话 ID')
                return {'job':service.latest(value('session_id'))}
            if path=='/api/deep-analysis/prompt':
                return service.repair_prompt(value('id'),int(value('index')))
            if path=='/api/deep-analysis':
                return service.get(value('id'))
        except (ValueError, TypeError) as exc:
            return {'error':str(exc)}
        return {'error':'未知接口'}
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
            grouped=query.get('grouped',['0'])[0]=='1'
            listing=(store.session_hierarchy if grouped else store.sessions)(con,days,query.get('q',[''])[0][:200],integer('limit',100,500),integer('offset',0,10000000))
            snapshot=quota_cache.snapshot() if quota_cache else {'weekly':None,'error':'额度快照尚未取得'}
            allocation=quota_estimate.allocate(con,roots[0] if roots else '',snapshot)
            if grouped:
                stack=[(row,False) for row in listing['items']]
                while stack:
                    row,done=stack.pop()
                    if not done:
                        stack.append((row,True))
                        stack.extend((child,False) for child in row['children'])
                        continue
                    row['weekly_quota_estimate']=quota_estimate.for_sessions(allocation,[row['id']])
                    ids=[row['id']]
                    for child in row['children']:
                        ids.extend(child.pop('_group_ids'))
                    row['group_weekly_quota_estimate']=quota_estimate.for_sessions(allocation,ids)
                    row['_group_ids']=ids
                for row in listing['items']:
                    row.pop('_group_ids')
            else:
                for row in listing['items']:
                    row['weekly_quota_estimate']=quota_estimate.for_sessions(allocation,[row['id']])
            return listing
        if path=='/api/issues':
            view=query.get('view',['actionable'])[0]
            if view not in ('actionable','observations','all'):
                return {'error':'未知问题视图'}
            return store.issues(con,days,integer('limit',100,500),integer('offset',0,10000000),view=view)
        if path=='/api/issue-prompt':
            issue_id=query.get('id',[''])[0]
            if issue_id.startswith('config:'):
                snapshot=(config_cache.snapshot() if config_cache else _config_audit(roots).snapshot())
                finding=next((item for item in snapshot.get('findings',[]) if item.get('id')==issue_id),None)
                return repair_prompt.generate_config(finding) if finding else {'error':'配置问题不存在'}
            return repair_prompt.generate(con,issue_id)
        if path=='/api/session':
            sid=query.get('id',[''])[0]
            if not sid:
                return {'error':'缺少会话 ID'}
            result=store.detail(con,sid)
            if result:
                snapshot=quota_cache.snapshot() if quota_cache else {'weekly':None,'error':'额度快照尚未取得'}
                allocation=quota_estimate.allocate(con,roots[0] if roots else '',snapshot)
                forest=store.session_hierarchy(con,0,limit=con.execute('SELECT COUNT(*) FROM sessions').fetchone()[0])['items']
                pending=forest[:]
                tree=None
                while pending:
                    node=pending.pop()
                    if node['id']==sid:
                        tree=node
                        break
                    pending.extend(node['children'])
                pending=[tree] if tree else []
                ids=[]
                while pending:
                    node=pending.pop()
                    ids.append(node['id'])
                    pending.extend(node['children'])
                result['weekly_quota_estimate']=quota_estimate.for_sessions(allocation,[sid])
                result['group_weekly_quota_estimate']=quota_estimate.for_sessions(allocation,ids or [sid])
                result['descendant_count']=tree['descendant_count'] if tree else 0
                result['children']=tree['children'] if tree else []
                result['hierarchy_warning']=tree['hierarchy_warning'] if tree else None
            return result if result else {'error':'会话不存在'}
        return {'error':'未知接口'}
    finally:
        con.close()


def post_api(path, payload, analysis_service):
    try:
        if not isinstance(payload,dict):
            raise ValueError('请求必须是 JSON 对象')
        if path=='/api/deep-analysis':
            if not isinstance(payload.get('preview_id'),str) or not payload['preview_id']:
                raise ValueError('缺少 preview_id')
            return analysis_service.start(payload['preview_id'])
        if path=='/api/deep-analysis/cancel':
            if not isinstance(payload.get('id'),str) or not payload['id']:
                raise ValueError('缺少任务 ID')
            return analysis_service.cancel(payload['id'])
    except (ValueError, TypeError) as exc:
        return {'error':str(exc)}
    return {'error':'未知接口'}


def _error_envelope(result):
    return isinstance(result,dict) and set(result)=={'error'}


def _valid_origin(origin, host):
    if not origin:
        return True
    parsed=urlparse(origin)
    return (parsed.scheme=='http' and parsed.hostname in ('127.0.0.1','localhost')
            and not parsed.path.strip('/') and not parsed.query and not parsed.fragment
            and parsed.netloc==host)


def _post_request(path, headers, body, analysis_service):
    if path not in ('/api/deep-analysis','/api/deep-analysis/cancel'):
        return 404,{'error':'未知接口'}
    if not _valid_origin(headers.get('Origin'),headers.get('Host','')):
        return 403,{'error':'请求来源不匹配'}
    if headers.get('Content-Type','').split(';',1)[0].strip().lower()!='application/json':
        return 415,{'error':'需要 application/json'}
    if len(body)>8192:
        return 413,{'error':'请求内容超过 8 KiB'}
    try:
        result=post_api(path,json.loads(body),analysis_service)
        return (400 if _error_envelope(result) else 200),result
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        return 400,{'error':str(exc)}


def serve(data_dir, roots, port=8768):
    quota_cache=quota.QuotaCache(roots[0])
    config_cache=_config_audit(roots)
    analysis_service=_analysis_service(data_dir, roots)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed=urlparse(self.path)
            path=parsed.path
            if path.startswith('/api/'):
                try:
                    result=api(path,parse_qs(parsed.query),data_dir,roots,quota_cache,config_cache,analysis_service)
                    body=json.dumps(result,ensure_ascii=False).encode()
                    status=400 if path!='/api/quota' and _error_envelope(result) else 200
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

        def do_POST(self):
            path=urlparse(self.path).path
            try:
                length=int(self.headers.get('Content-Length',''))
                if length<0 or length>8192:
                    raise ValueError('请求内容超过 8 KiB 或长度无效')
                status,result=_post_request(path,self.headers,self.rfile.read(length),analysis_service)
            except (ValueError, TypeError) as exc:
                status,result=400,{'error':str(exc)}
            except Exception:
                status,result=500,{'error':'诊断请求失败'}
            body=json.dumps(result,ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8')
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
    old_sigterm=None
    def stop_on_sigterm(_signum, _frame):
        raise SystemExit(0)
    try:
        if threading.current_thread() is threading.main_thread():
            old_sigterm=signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM,stop_on_sigterm)
        threading.Thread(target=watcher,daemon=True).start()
        threading.Thread(target=quota_cache.run,daemon=True).start()
        httpd.serve_forever()
    finally:
        try:
            analysis_service.shutdown()
        finally:
            httpd.server_close()
            if old_sigterm is not None:
                signal.signal(signal.SIGTERM,old_sigterm)
