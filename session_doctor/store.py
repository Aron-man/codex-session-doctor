import datetime as dt
import json
import sqlite3
from pathlib import Path


SCHEMA = '''
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, dev INTEGER, ino INTEGER, size INTEGER, offset INTEGER DEFAULT 0, line INTEGER DEFAULT 0, session_id TEXT, state TEXT DEFAULT '{}', seen_scan INTEGER DEFAULT 0, status TEXT DEFAULT 'active');
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, title TEXT DEFAULT '', cwd TEXT DEFAULT '', source TEXT DEFAULT 'unknown', parent_id TEXT, created TEXT, last_active TEXT, status TEXT DEFAULT 'active', fork INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS usage(id TEXT PRIMARY KEY, session_id TEXT, timestamp TEXT, turn_id TEXT, model TEXT DEFAULT 'unknown', input INTEGER, cached INTEGER, output INTEGER, reasoning INTEGER, total INTEGER, basis TEXT, purpose TEXT DEFAULT 'model', path TEXT, line INTEGER, requests INTEGER DEFAULT 1, matched INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS usage_session_time ON usage(session_id,timestamp);
CREATE INDEX IF NOT EXISTS usage_time ON usage(timestamp);
CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY, session_id TEXT, timestamp TEXT, turn_id TEXT, name TEXT, command TEXT, output_chars INTEGER DEFAULT 0, output_hash TEXT, estimated_tokens INTEGER DEFAULT 0, status TEXT DEFAULT 'unknown', path TEXT, line INTEGER, truncated INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS calls_session_time ON calls(session_id,timestamp);
CREATE TABLE IF NOT EXISTS issues(id TEXT PRIMARY KEY, session_id TEXT, kind TEXT, severity TEXT, title TEXT, evidence TEXT, suggestion TEXT, timestamp TEXT, turn_id TEXT, path TEXT, line INTEGER, estimated_tokens INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS issues_session_time ON issues(session_id,timestamp);
CREATE TABLE IF NOT EXISTS warnings(id TEXT PRIMARY KEY, session_id TEXT, message TEXT, path TEXT, line INTEGER, timestamp TEXT);
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
'''


def connect(data_dir):
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(Path(data_dir) / 'doctor.sqlite3'), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA busy_timeout=30000')
    con.executescript(SCHEMA)
    return con


def rows(con, sql, args=()):
    return [dict(x) for x in con.execute(sql, args)]


def cutoff(days):
    if not days:
        return '0000'
    local=dt.datetime.now().astimezone()
    start=local.replace(hour=0,minute=0,second=0,microsecond=0)-dt.timedelta(days=days-1)
    return start.astimezone(dt.timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')


def session_row(con, sid):
    row = con.execute('''SELECT s.*,COALESCE(u.total,0) total,COALESCE(u.input,0) input,COALESCE(u.cached,0) cached,COALESCE(u.output,0) output,COALESCE(u.reasoning,0) reasoning,COALESCE(u.requests,0) requests,COALESCE(c.tool_calls,0) tool_calls,COALESCE(i.issue_count,0) issue_count
    FROM sessions s LEFT JOIN (SELECT session_id,SUM(total) total,SUM(input) input,SUM(cached) cached,SUM(output) output,SUM(reasoning) reasoning,SUM(requests) requests FROM usage GROUP BY session_id) u ON u.session_id=s.id
    LEFT JOIN (SELECT session_id,COUNT(*) tool_calls FROM calls GROUP BY session_id) c ON c.session_id=s.id
    LEFT JOIN (SELECT session_id,COUNT(*) issue_count FROM issues GROUP BY session_id) i ON i.session_id=s.id WHERE s.id=?''', (sid,)).fetchone()
    return dict(row) if row else None


def sessions(con, days=7, q='', limit=100, offset=0):
    since = cutoff(days)
    where = "WHERE (u.session_id IS NOT NULL OR (s.created>=? AND NOT EXISTS(SELECT 1 FROM usage ux WHERE ux.session_id=s.id)))"
    args = [since]
    if q:
        where += ' AND (s.title LIKE ? OR s.cwd LIKE ? OR s.id LIKE ?)'
        args += ['%' + q + '%'] * 3
    joins='''FROM sessions s
      LEFT JOIN (SELECT session_id,SUM(total) total,SUM(input) input,SUM(cached) cached,SUM(output) output,SUM(reasoning) reasoning,SUM(requests) requests FROM usage WHERE timestamp>=? GROUP BY session_id) u ON u.session_id=s.id
      LEFT JOIN (SELECT session_id,COUNT(*) tool_calls FROM calls WHERE timestamp>=? GROUP BY session_id) c ON c.session_id=s.id
      LEFT JOIN (SELECT session_id,COUNT(*) issue_count FROM issues WHERE timestamp>=? GROUP BY session_id) i ON i.session_id=s.id'''
    base=[since,since,since]+args
    items=rows(con,f'''SELECT s.id,s.title,s.cwd,s.source,s.parent_id,s.created,s.last_active,s.status,
      COALESCE(u.total,0) total,COALESCE(u.input,0) input,COALESCE(u.cached,0) cached,COALESCE(u.output,0) output,COALESCE(u.reasoning,0) reasoning,
      COALESCE(u.requests,0) requests,COALESCE(c.tool_calls,0) tool_calls,COALESCE(i.issue_count,0) issue_count
      {joins} {where} ORDER BY s.last_active DESC LIMIT ? OFFSET ?''',base+[limit,offset])
    total=con.execute(f'SELECT COUNT(*) {joins} {where}',base).fetchone()[0]
    return {'items':items,'total':total,'limit':limit,'offset':offset}


def issues(con, days=7, limit=100, offset=0, sid=None):
    where = 'WHERE i.session_id=?' if sid else 'WHERE i.timestamp>=?'
    args = [sid] if sid else [cutoff(days)]
    items = rows(con, f'''SELECT i.*,s.title session_title FROM issues i LEFT JOIN sessions s ON s.id=i.session_id {where} ORDER BY i.timestamp DESC LIMIT ? OFFSET ?''', args + [limit,offset])
    total = con.execute(f'SELECT COUNT(*) FROM issues i {where}', args).fetchone()[0]
    return {'items': items, 'total': total, 'limit': limit, 'offset': offset}


def detail(con, sid):
    session = session_row(con, sid)
    if not session:
        return None
    ucount = con.execute('SELECT COUNT(*) FROM usage WHERE session_id=?', (sid,)).fetchone()[0]
    ccount = con.execute('SELECT COUNT(*) FROM calls WHERE session_id=?', (sid,)).fetchone()[0]
    return {'session': session,
      'turns': rows(con, '''SELECT COALESCE(turn_id,'unknown') id,SUM(total) total,SUM(input) input,SUM(cached) cached,SUM(output) output,SUM(requests) requests FROM usage WHERE session_id=? GROUP BY turn_id ORDER BY MIN(timestamp)''',(sid,)),
      'usage': rows(con, 'SELECT timestamp,turn_id,model,input,cached,output,reasoning,total,basis,purpose,path,line FROM usage WHERE session_id=? ORDER BY timestamp DESC LIMIT 500',(sid,)),
      'calls': rows(con, 'SELECT timestamp,turn_id,name,command,output_chars,estimated_tokens,status,path,line FROM calls WHERE session_id=? ORDER BY timestamp DESC LIMIT 500',(sid,)),
      'issues': issues(con, 0, 1000, 0, sid)['items'],
      'children': rows(con, '''SELECT s.id,s.title,COALESCE(SUM(u.total),0) total FROM sessions s LEFT JOIN usage u ON u.session_id=s.id WHERE s.parent_id=? GROUP BY s.id''',(sid,)),
      'warnings': rows(con,'SELECT message,path,line FROM warnings WHERE session_id=? ORDER BY timestamp DESC LIMIT 200',(sid,)),
      'truncated': {'usage': ucount>500,'calls':ccount>500}}


def overview(con, days=7, roots=()):
    since=cutoff(days)
    t=con.execute('''SELECT COALESCE(SUM(input),0) input,COALESCE(SUM(cached),0) cached,COALESCE(SUM(output),0) output,COALESCE(SUM(reasoning),0) reasoning,COALESCE(SUM(total),0) total,COALESCE(SUM(requests),0) requests,COUNT(DISTINCT session_id) sessions FROM usage WHERE timestamp>=?''',(since,)).fetchone()
    data={'totals':dict(t),
      'daily': rows(con,'''SELECT substr(datetime(timestamp,'localtime'),1,10) day,SUM(input) input,SUM(cached) cached,SUM(output) output,SUM(total) total,SUM(requests) requests FROM usage WHERE timestamp>=? GROUP BY day ORDER BY day''',(since,)),
      'models':rows(con,'''SELECT model name,SUM(total) total,SUM(input) input,SUM(cached) cached,SUM(output) output,SUM(requests) requests FROM usage WHERE timestamp>=? GROUP BY model ORDER BY total DESC''',(since,)),
      'projects':rows(con,'''SELECT COALESCE(NULLIF(s.cwd,''),'unknown') name,SUM(u.total) total,SUM(u.requests) requests FROM usage u LEFT JOIN sessions s ON s.id=u.session_id WHERE u.timestamp>=? GROUP BY name ORDER BY total DESC''',(since,)),
      'sources':rows(con,'''SELECT COALESCE(NULLIF(s.source,''),'unknown') name,SUM(u.total) total,SUM(u.requests) requests FROM usage u LEFT JOIN sessions s ON s.id=u.session_id WHERE u.timestamp>=? GROUP BY name ORDER BY total DESC''',(since,))}
    fs=con.execute('SELECT COUNT(*) files,SUM(CASE WHEN offset>0 THEN 1 ELSE 0 END) scanned_files,COALESCE(SUM(size),0) bytes,COALESCE(SUM(offset),0) scanned_bytes FROM files').fetchone()
    state={r['key']:r['value'] for r in con.execute('SELECT * FROM state')}
    data['coverage']={**dict(fs),'parse_errors':con.execute("SELECT COUNT(*) FROM warnings WHERE message LIKE 'JSON%'").fetchone()[0],
      'missing_usage_sessions':con.execute('SELECT COUNT(*) FROM sessions s WHERE NOT EXISTS(SELECT 1 FROM usage u WHERE u.session_id=s.id)').fetchone()[0],
      'warning_count':con.execute('SELECT COUNT(*) FROM warnings').fetchone()[0], 'last_scan':state.get('last_scan'), 'scanning':state.get('scanning')=='1','error':state.get('error'), 'roots':list(roots),
      'limitations':['仅覆盖可读取的本机日志；云端、其他机器和已删除的历史不在内','旧版兼容累计可能缺少上下文压缩消耗','工具输出 token 为字符数/4 估算，不能归为精确模型用量']}
    return data
