import datetime as dt
import hashlib
import json
import os
from pathlib import Path

from . import diagnostics
from .parser import call_status, command_from, digest, difference, output_fingerprint, output_text, safe, title_from, usage
from .store import connect

MAX_LINE = 32 * 1024 * 1024


def key(*parts):
    return hashlib.sha256('|'.join(str(p) for p in parts).encode()).hexdigest()[:40]


def warn(con, sid, message, path, line, timestamp):
    con.execute('''INSERT INTO warnings VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET path=excluded.path''',
                (key(sid,message,line),sid,message,path,line,timestamp))


def put_usage(con, uid, sid, stamp, turn, model, u, basis, purpose, path, line, requests=1):
    con.execute('''INSERT OR IGNORE INTO usage(id,session_id,timestamp,turn_id,model,input,cached,output,reasoning,total,basis,purpose,path,line,requests)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
      (uid,sid,stamp,turn,model,*[u[k] for k in ('input','cached','output','reasoning','total')],basis,purpose,path,line,requests))


def session_id(path):
    stem=Path(path).stem
    return stem[-36:] if len(stem)>=36 else stem


def process(con, obj, path, line, state):
    kind=obj.get('type')
    payload=obj.get('payload') or {}
    stamp=obj.get('timestamp') or ''
    sid=state['sid']
    if kind=='session_meta':
        sid=payload.get('id') or sid
        state['sid']=sid
        state['created']=payload.get('timestamp') or stamp
        state['fork']=bool(payload.get('forked_from_id') or payload.get('parent_thread_id'))
        source_obj=payload.get('source')
        spawn=(source_obj.get('subagent') or {}).get('thread_spawn') if isinstance(source_obj,dict) else None
        parent=payload.get('parent_thread_id') or (spawn or {}).get('parent_thread_id') or payload.get('forked_from_id')
        source=payload.get('source')
        source=source if isinstance(source,str) else ('subagent' if spawn else 'unknown')
        thread_source=payload.get('thread_source')
        if thread_source=='automation':
            source='automation'
        elif thread_source=='subagent':
            source='subagent'
        con.execute('''INSERT INTO sessions(id,cwd,source,parent_id,created,last_active,fork) VALUES(?,?,?,?,?,?,?)
          ON CONFLICT(id) DO UPDATE SET cwd=excluded.cwd,source=excluded.source,parent_id=COALESCE(excluded.parent_id,sessions.parent_id),created=COALESCE(sessions.created,excluded.created),fork=excluded.fork''',
          (sid,safe(payload.get('cwd',''),500),safe(source,80),parent,state['created'],stamp,int(state['fork'])))
        return
    con.execute('INSERT OR IGNORE INTO sessions(id,created,last_active) VALUES(?,?,?)',(sid,state.get('created') or stamp,stamp))
    con.execute('UPDATE sessions SET last_active=CASE WHEN last_active<? THEN ? ELSE last_active END WHERE id=?',(stamp,stamp,sid))
    if kind=='turn_context':
        state['turn']=payload.get('turn_id') or state.get('turn')
        state['model']=payload.get('model') or 'unknown'
        return
    if kind=='event_msg' and payload.get('type')=='task_started':
        state['turn']=payload.get('turn_id') or state.get('turn')
        state['activity']='running'
        con.execute("UPDATE sessions SET status='running' WHERE id=?",(sid,))
        return
    if kind=='event_msg' and payload.get('type') in ('task_complete','turn_aborted','task_failed','task_error'):
        activity='completed' if payload.get('type')=='task_complete' else ('aborted' if payload.get('type')=='turn_aborted' else 'failed')
        state['activity']=activity
        con.execute('UPDATE sessions SET status=? WHERE id=?',(activity,sid))
        return
    if kind=='response_item':
        title=title_from(payload)
        if title:
            con.execute("UPDATE sessions SET title=? WHERE id=? AND (title='' OR title IS NULL)",(title,sid))
        typ=payload.get('type')
        if typ in ('function_call','custom_tool_call'):
            cid=payload.get('call_id') or payload.get('id') or key(sid,stamp,typ,line)
            name=payload.get('name') or 'unknown'
            con.execute('''INSERT OR IGNORE INTO calls(id,session_id,timestamp,turn_id,name,command,path,line) VALUES(?,?,?,?,?,?,?,?)''',
                (key(sid,cid),sid,stamp,state.get('turn'),safe(name,100),command_from(payload),path,line))
        elif typ in ('function_call_output','custom_tool_call_output'):
            cid=payload.get('call_id') or payload.get('id')
            prior=None
            if not payload.get('call_id') and payload.get('name'):
                prior=con.execute('''SELECT id FROM calls WHERE session_id=? AND name=? AND output_hash IS NULL ORDER BY timestamp DESC LIMIT 1''',
                    (sid,payload.get('name'))).fetchone()
                if prior:
                    cid=None
            if cid:
                output=payload.get('output','')
                raw=output_text(output)
                length=len(raw)
                truncated=any(x in raw[-2000:] for x in ('Warning: truncated output','truncated output','… tokens truncated…'))
                con.execute('''UPDATE calls SET output_chars=?,output_hash=?,estimated_tokens=?,status=?,truncated=? WHERE id=?''',
                    (length,output_fingerprint(raw),length//4,call_status(output),int(truncated),key(sid,cid)))
            elif prior:
                output=payload.get('output','')
                raw=output_text(output)
                length=len(raw)
                truncated=any(x in raw[-2000:] for x in ('Warning: truncated output','truncated output','… tokens truncated…'))
                con.execute('''UPDATE calls SET output_chars=?,output_hash=?,estimated_tokens=?,status=?,truncated=? WHERE id=?''',
                    (length,output_fingerprint(raw),length//4,call_status(output),int(truncated),prior['id']))
        return
    if kind=='compacted':
        rid=payload.get('compaction_response_id')
        latest=payload.get('latest_token_usage_record')
        if isinstance(latest,dict):
            rid=rid or latest.get('response_id')
            if latest.get('usage'):
                modern(con,latest,path,line,stamp,state,'compaction')
        if rid:
            con.execute("UPDATE usage SET purpose='compaction' WHERE id=?",(key('response',rid),))
        return
    if kind=='token_usage_record':
        modern(con,payload,path,line,stamp,state,'model')
        return
    if kind=='event_msg' and payload.get('type')=='token_count':
        legacy(con,payload.get('info'),path,line,stamp,state)


def modern(con,payload,path,line,stamp,state,purpose):
    sid=payload.get('thread_id') or payload.get('session_id') or state['sid']
    rid=payload.get('response_id')
    if not payload.get('usage'):
        warn(con,sid,'现代记录缺少 usage',path,line,stamp)
        return
    u=usage(payload['usage'])
    uid=key('response',rid) if rid else key('modern-fallback',sid,stamp,digest(payload['usage']))
    if not rid:
        warn(con,sid,'现代记录缺少 response_id，使用低可信度指纹去重',path,line,stamp)
    con.execute('INSERT OR IGNORE INTO sessions(id,created,last_active) VALUES(?,?,?)',(sid,stamp,stamp))
    existed=con.execute('SELECT 1 FROM usage WHERE id=?',(uid,)).fetchone() is not None
    turn=payload.get('turn_id') or state.get('turn')
    put_usage(con,uid,sid,stamp,turn,state.get('model') or 'unknown',u,'modern' if rid else 'modern_fallback',purpose,path,line)
    if purpose=='compaction':
        con.execute("UPDATE usage SET purpose='compaction' WHERE id=?",(uid,))
    state['modern']=True
    if purpose=='model':
        state.setdefault('recent_modern',[]).append({'usage':u,'stamp':stamp,'turn':turn})
        state['recent_modern']=state['recent_modern'][-8:]
    if sid==state['sid'] and not existed and purpose=='model':
        # A preceding legacy snapshot may be a mirror of this response.
        pending=state.pop('pending_legacy',None)
        if pending and pending['usage']==u and pending['turn']==turn and 0 < line-pending['line'] <= 10:
            con.execute('DELETE FROM usage WHERE id=?',(pending['id'],))


def near(a,b):
    try:
        return abs((dt.datetime.fromisoformat(a.replace('Z','+00:00'))-dt.datetime.fromisoformat(b.replace('Z','+00:00'))).total_seconds())<=10
    except (ValueError,TypeError):
        return False


def legacy(con,info,path,line,stamp,state):
    sid=state['sid']
    if not isinstance(info,dict):
        warn(con,sid,'token_count 缺少用量 info',path,line,stamp)
        return
    total=usage(info.get('total_token_usage'))
    last=usage(info.get('last_token_usage')) if info.get('last_token_usage') else None
    prev=state.get('legacy_total')
    if state.get('created') and stamp<state['created']:
        state['legacy_total']=total
        return
    if prev is not None and total==prev:
        return
    reset=prev is not None and total['total']<prev['total']
    if reset:
        warn(con,sid,'累计计数重置；仅按可见 last 用量恢复',path,line,stamp)
        state['epoch']=state.get('epoch',0)+1
    delta=difference(total,prev) if prev is not None and not reset else total
    state['legacy_total']=total
    if state.get('modern'):
        # Modern thread totals include compaction, while these snapshots do not.
        mirrored=False
        if last:
            recent=state.get('recent_modern',[])
            for idx in range(len(recent)-1,-1,-1):
                if recent[idx]['usage']==last and recent[idx]['turn']==state.get('turn'):
                    recent.pop(idx)
                    mirrored=True
                    break
            if not mirrored:
                mirrored=con.execute('''SELECT 1 FROM usage WHERE session_id=? AND basis LIKE 'modern%' AND purpose='model' AND turn_id IS ? AND path=? AND line BETWEEN ? AND ? AND input=? AND cached=? AND output=? AND reasoning=? AND total=? LIMIT 1''',
                    (sid,state.get('turn'),path,max(1,line-300),line,last['input'],last['cached'],last['output'],last['reasoning'],last['total'])).fetchone() is not None
        if not mirrored:
            warn(con,sid,'兼容快照新增用量无法对应现代普通请求，可能存在记录缺口',path,line,stamp)
        return
    if last and last['total']:
        uid=key('legacy',sid,state.get('epoch',0),total['total'],total['input'],total['output'])
        put_usage(con,uid,sid,stamp,state.get('turn'),state.get('model') or 'unknown',last,'legacy_last','model',path,line)
        state['pending_legacy']={'id':uid,'usage':last,'stamp':stamp,'turn':state.get('turn'),'line':line}
    if prev is None:
        gap=difference(total,last or {k:0 for k in total})
        if state.get('fork'):
            if gap['total']:
                warn(con,sid,'fork 首个累计快照含继承基线，未计入分支',path,line,stamp)
        elif gap['total']:
            put_usage(con,key('gap',sid,'first',state.get('epoch',0)),sid,stamp,None,'unknown',gap,'cumulative_gap','model',path,line,0)
    elif not reset and delta['total']>(last or {}).get('total',0):
        gap=difference(delta,last or {k:0 for k in delta})
        put_usage(con,key('gap',sid,state.get('epoch',0),total['total']),sid,stamp,None,'unknown',gap,'cumulative_gap','model',path,line,0)


def scan(roots,data_dir,progress=None):
    con=connect(data_dir)
    now=dt.datetime.now(dt.timezone.utc).isoformat()
    con.execute("INSERT OR REPLACE INTO state VALUES('scanning','1')")
    con.commit()
    touched=set()
    seen=[]
    try:
        for root in roots:
            for folder in ('sessions','archived_sessions'):
                base=Path(root)/folder
                if not base.exists():
                    continue
                try:
                    paths=(p for p in base.rglob('*.jsonl') if p.is_file())
                    seen.extend(paths)
                except OSError as exc:
                    warn(con,None,'目录无法读取：'+safe(str(exc),200),str(base),0,now)
        seen.sort(key=lambda p:p.stat().st_mtime if p.exists() else 0,reverse=True)
        for path in seen:
            try:
                st=path.stat()
                con.execute('''INSERT OR IGNORE INTO files(path,dev,ino,size,session_id,state,status) VALUES(?,?,?,?,?,?,?)''',
                    (str(path),st.st_dev,st.st_ino,st.st_size,session_id(path),json.dumps({'sid':session_id(path)}),'archived' if 'archived_sessions' in path.parts else 'active'))
            except OSError:
                pass
        con.commit()
        for idx,path in enumerate(seen):
            p=str(path)
            try:
                st=path.stat()
                row=con.execute('SELECT * FROM files WHERE path=?',(p,)).fetchone()
                offset=row['offset'] if row else 0
                line=row['line'] if row else 0
                state=json.loads(row['state']) if row else {'sid':session_id(p)}
                if row and (row['dev']!=st.st_dev or row['ino']!=st.st_ino or st.st_size<offset):
                    warn(con,state['sid'],'文件截断或替换，重新扫描并保留历史',p,line,now)
                    offset,line,state=0,0,{'sid':session_id(p)}
                if st.st_size==offset:
                    con.execute('UPDATE files SET size=?,status=? WHERE path=?',(st.st_size,folder if False else ('archived' if 'archived_sessions' in path.parts else 'active'),p))
                    continue
                with path.open('rb') as f:
                    f.seek(offset)
                    while True:
                        start=f.tell()
                        b=f.readline(MAX_LINE+1)
                        if not b:
                            break
                        if len(b)>MAX_LINE:
                            skipped=len(b)
                            while b and not b.endswith(b'\n'):
                                b=f.readline(MAX_LINE+1)
                                skipped+=len(b)
                            if not b.endswith(b'\n'):
                                f.seek(start)
                                break
                            line+=1
                            warn(con,state['sid'],f'超大行跳过 {skipped} 字节',p,line,now)
                            continue
                        if not b.endswith(b'\n'):
                            f.seek(start)
                            break
                        line+=1
                        try:
                            obj=json.loads(b)
                        except (ValueError,UnicodeDecodeError):
                            warn(con,state['sid'],'JSON 解析失败，已跳过完整坏行',p,line,now)
                            continue
                        if isinstance(obj,dict):
                            process(con,obj,p,line,state)
                    offset=f.tell()
                con.execute('''INSERT INTO files(path,dev,ino,size,offset,line,session_id,state,status) VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(path) DO UPDATE SET dev=excluded.dev,ino=excluded.ino,size=excluded.size,offset=excluded.offset,line=excluded.line,session_id=excluded.session_id,state=excluded.state,status=excluded.status''',
                    (p,st.st_dev,st.st_ino,st.st_size,offset,line,state['sid'],json.dumps(state), 'archived' if 'archived_sessions' in path.parts else 'active'))
                touched.add(state['sid'])
                con.commit()
                if progress:
                    progress(idx+1,len(seen))
            except (OSError,ValueError) as exc:
                warn(con,session_id(p),'文件读取失败：'+safe(str(exc),200),p,0,now)
                con.commit()
        live={str(p) for p in seen}
        for row in con.execute('SELECT path,session_id FROM files'):
            if row['path'] not in live:
                con.execute("UPDATE files SET status='missing' WHERE path=?",(row['path'],))
        con.execute('''UPDATE sessions SET status=CASE
          WHEN EXISTS(SELECT 1 FROM files f WHERE f.session_id=sessions.id AND f.status='active') THEN
            CASE WHEN status IN ('running','completed','aborted','failed') THEN status ELSE 'active' END
          WHEN EXISTS(SELECT 1 FROM files f WHERE f.session_id=sessions.id AND f.status='archived') THEN
            CASE WHEN status LIKE 'archived · %' THEN status ELSE 'archived' || CASE WHEN status IN ('running','completed','aborted','failed') THEN ' · ' || status ELSE '' END END
          ELSE 'source missing' END''')
        for sid in touched:
            diagnostics.analyze(con,sid)
        con.execute("INSERT OR REPLACE INTO state VALUES('last_scan',?)",(dt.datetime.now(dt.timezone.utc).isoformat(),))
        con.execute("INSERT OR REPLACE INTO state VALUES('error','')")
    except Exception as exc:
        con.execute("INSERT OR REPLACE INTO state VALUES('error',?)",(safe(str(exc),500),))
        raise
    finally:
        con.execute("INSERT OR REPLACE INTO state VALUES('scanning','0')")
        con.commit()
        con.close()
    if touched:
        reconcile_mirrors(data_dir)
    return {'files':len(seen),'changed_sessions':len(touched)}


def reconcile_mirrors(data_dir):
    """Remove false gap warnings after replay or delayed compatibility snapshots."""
    con=connect(data_dir)
    grouped={}
    for row in con.execute("SELECT id,session_id,path,line FROM warnings WHERE message='兼容快照新增用量无法对应现代普通请求，可能存在记录缺口'"):
        grouped.setdefault(row['path'],{})[row['line']]=(row['id'],row['session_id'])
    removed=0
    for path,targets in grouped.items():
        if not Path(path).is_file():
            continue
        try:
            with open(path,'rb') as f:
                for line,b in enumerate(f,1):
                    if line not in targets:
                        continue
                    wid,sid=targets[line]
                    try:
                        obj=json.loads(b)
                        raw=((obj.get('payload') or {}).get('info') or {}).get('last_token_usage')
                        if not raw:
                            continue
                        u=usage(raw)
                    except (ValueError,TypeError):
                        continue
                    mirror=con.execute('''SELECT 1 FROM usage WHERE session_id=? AND basis LIKE 'modern%' AND purpose='model' AND path=? AND line BETWEEN ? AND ? AND input=? AND cached=? AND output=? AND reasoning=? AND total=? LIMIT 1''',
                        (sid,path,max(1,line-300),line,u['input'],u['cached'],u['output'],u['reasoning'],u['total'])).fetchone()
                    if mirror:
                        con.execute('DELETE FROM warnings WHERE id=?',(wid,))
                        removed+=1
        except OSError:
            continue
    con.commit()
    con.close()
    return removed
