"""Evidence based session diagnostics from indexed native tool calls."""
import hashlib
import json
import re

from .parser import safe

LARGE_OUTPUT, LARGE_CONTEXT, LOW_CACHE_INPUT = 20000, 150000, 100000
VERSION = 'diagnostics-v2.2'
READ = re.compile(r'^\s*(?:cat|sed|head|tail|rg|grep)\b')


def _skill_evidence(row, skill):
    try: return json.loads(row['skill_evidence'] or '{}').get(skill) or {}
    except (ValueError, TypeError): return {}


def put(con, sid, kind, key, severity, title, evidence, suggestion, row, estimate=0, *, category='observation', confidence='info', actionable=False, target_path=None, target_line=None, tool=None, command=None, details=None):
    iid = hashlib.sha256((sid+'|'+kind+'|'+key).encode()).hexdigest()[:32]
    con.execute('''INSERT INTO issues(id,session_id,kind,severity,title,evidence,suggestion,timestamp,turn_id,path,line,estimated_tokens,category,confidence,actionable,target_path,target_line,tool,command,details_json)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET severity=excluded.severity,title=excluded.title,evidence=excluded.evidence,suggestion=excluded.suggestion,estimated_tokens=excluded.estimated_tokens,category=excluded.category,confidence=excluded.confidence,actionable=excluded.actionable,target_path=excluded.target_path,target_line=excluded.target_line,tool=excluded.tool,command=excluded.command,details_json=excluded.details_json''',
      (iid,sid,kind,severity,title,safe(evidence,1000),safe(suggestion,1000),row['timestamp'],row['turn_id'],row['path'],row['line'],estimate,category,confidence,int(actionable),target_path,target_line,tool,safe(command,240) if command else None,json.dumps(details or {},ensure_ascii=False)))


def _paths(row, field):
    try: return json.loads(row[field] or '[]')
    except (ValueError, TypeError): return []


def _failed(row):
    code = row['exit_code']
    if row['tool']=='exec_command' and code == 1 and re.match(r'^\s*(?:rg|grep)\b', row['command'] or ''): return False
    return (code is not None and code != 0) or row['status'] in ('failed','error') or bool(row['error_excerpt'] and row['status'] not in ('completed','success'))


def analyze(con, sid):
    con.execute('DELETE FROM issues WHERE session_id=?',(sid,))
    events=list(con.execute('SELECT * FROM tool_events WHERE session_id=? ORDER BY timestamp,line',(sid,)))
    if events:
        groups={}; truncations={}; skill_failures={}
        for e in events:
            targets=_paths(e,'target_paths'); skills=_paths(e,'skill_paths')
            target=targets[0] if targets else None
            common=dict(target_path=target,tool=e['tool'],command=e['command'])
            if e['output_chars']>=LARGE_OUTPUT and not e['truncated']:
                put(con,sid,'large_output',e['id'],'info','工具输出较长',f"{e['tool']} 输出 {e['output_chars']} 字符；命令：{e['command']}",'按任务需要检查输出范围。',e,details={'event_ids':[e['id']]},**common)
            if e['truncated']:
                truncations.setdefault((e['turn_id'],e['context_epoch'],e['tool'],e['command_hash'],tuple(targets)),[]).append(e)
            if skills:
              for skill in skills:
                proof=_skill_evidence(e,skill)
                if proof.get('discarded'):
                    put(con,sid,'skill_read_discarded',e['id'],'warning','Skill 读取结果未返回模型',f"读取 {skill} 时将 stdout 重定向到 /dev/null；命令：{e['command']}",'如需加载该 Skill，读取正文并保留输出供模型查看。',e,category='skill',confidence='high',actionable=True,target_path=skill,tool=e['tool'],command=e['command'],details={'event_ids':[e['id']],'exit_code':e['exit_code']})
                if proof.get('file_error') or proof.get('sole_read') and _failed(e):
                    skill_failures.setdefault((e['turn_id'],e['context_epoch'],skill,e['command_hash']),[]).append(e)
            if e['tool']=='exec_command' and READ.match(e['command'] or '') and e['output_hash'] and not _failed(e):
                groups.setdefault((e['turn_id'],e['context_epoch'],e['command_hash'],json.dumps(targets),e['output_hash']),[]).append(e)
        for _,matches in truncations.items():
            first=matches[0]; targets=_paths(first,'target_paths')
            put(con,sid,'tool_output_truncated',first['id'],'warning','工具输出确实被截断',f"{first['tool']} 同一操作有 {len(matches)} 次出现执行包装的截断标记；命令：{first['command']}",'缩小读取范围或分批获取所需结果。',first,category='tool',confidence='high',actionable=True,target_path=targets[0] if targets else None,tool=first['tool'],command=first['command'],details={'event_ids':[x['id'] for x in matches],'count':len(matches),'exit_code':first['exit_code']})
        for (_,_,skill,_),matches in skill_failures.items():
            first=matches[0]; last=matches[-1]
            recovered=any(skill in _paths(n,'skill_paths') and not _skill_evidence(n,skill).get('file_error') and not _failed(n) for n in events if (n['timestamp'],n['line'])>(last['timestamp'],last['line']))
            proof=_skill_evidence(first,skill)
            put(con,sid,'skill_read_failed',first['id'],'info' if recovered else 'warning','Skill 读取失败'+('，后续已恢复' if recovered else ''),f"读取 {skill} 失败 {len(matches)} 次；退出码 {first['exit_code']}；{proof.get('file_error') or first['error_excerpt'] or ''}",'核对路径与最早失败原因；若已恢复无需再次修复。',first,category='skill' if not recovered else 'observation',confidence='high' if not recovered else 'info',actionable=not recovered,target_path=skill,tool=first['tool'],command=first['command'],details={'event_ids':[x['id'] for x in matches],'count':len(matches),'recovered':recovered,'exit_code':first['exit_code'],'file_error':proof.get('file_error')})
        for _,matches in groups.items():
            if len(matches)<3: continue
            first=matches[0]; skills=_paths(first,'skill_paths'); target=_paths(first,'target_paths')
            kind='skill_repeated_read' if skills else 'repeated_read'
            put(con,sid,kind,first['id'],'warning','重复读取相同内容',f"同轮次、同压缩区间相同命令及输出出现 {len(matches)} 次；目标 {target[0] if target else '未解析'}。",'复用已读内容，或缩小后续读取范围。',first,category='skill' if skills else 'tool',confidence='high',actionable=True,target_path=target[0] if target else None,tool=first['tool'],command=first['command'],details={'event_ids':[x['id'] for x in matches],'count':len(matches),'context_epoch':first['context_epoch']})
        streak=[]
        for e in events+[None]:
            same=e is not None and streak and e['tool']==streak[-1]['tool'] and e['command_hash']==streak[-1]['command_hash'] and e['turn_id']==streak[-1]['turn_id'] and e['context_epoch']==streak[-1]['context_epoch']
            if e is not None and _failed(e) and (not streak or same): streak.append(e); continue
            if len(streak)>=3:
                first=streak[0]; targets=_paths(first,'target_paths')
                last=streak[-1]
                recovered=any(n['tool']==first['tool'] and n['command_hash']==first['command_hash'] and not _failed(n) for n in events if (n['timestamp'],n['line'])>(last['timestamp'],last['line']))
                put(con,sid,'repeated_failure',first['id'],'info' if recovered else 'warning','同一操作连续失败'+('，后续已恢复' if recovered else ''),f"{first['tool']} 相同操作连续失败 {len(streak)} 次；退出码 {first['exit_code']}；{first['error_excerpt'] or ''}",'先读取最早错误及命令帮助或 schema；若已恢复无需再次修复。',first,category='observation' if recovered else 'tool',confidence='info' if recovered else 'high',actionable=not recovered,target_path=targets[0] if targets else None,tool=first['tool'],command=first['command'],details={'event_ids':[x['id'] for x in streak],'count':len(streak),'exit_code':first['exit_code'],'recovered':recovered})
            streak=[e] if e is not None and _failed(e) else []
    else:
        for c in con.execute('SELECT * FROM calls WHERE session_id=? ORDER BY timestamp,line',(sid,)):
            if c['output_chars']>=LARGE_OUTPUT or c['truncated']:
                put(con,sid,'large_output',c['id'],'info','外层工具输出较长',f"{c['name']} 输出 {c['output_chars']} 字符；只有旧版外层调用证据。",'需要原生调用记录才能判断具体工具问题。',c,c['estimated_tokens'],details={'source':'legacy_calls','limited':True})
    uses=list(con.execute('SELECT * FROM usage WHERE session_id=? AND requests=1 ORDER BY timestamp,line',(sid,)))
    by_turn={}
    for u in uses: by_turn.setdefault(u['turn_id'] or '',[]).append(u)
    for turn, items in by_turn.items():
        large=[x for x in items if x['input']>=LARGE_CONTEXT]
        if len(large)>=3:
            put(con,sid,'large_context',turn,'info','持续大上下文',f"同轮次 {len(large)} 个请求输入至少 {LARGE_CONTEXT} token。",'按任务需要检查上下文范围。',large[0])
    if len(uses)>=4:
        tail=uses[1:]; inp=sum(x['input'] for x in tail); cache=sum(x['cached'] for x in tail)
        if inp>=LOW_CACHE_INPUT and cache/inp<0.3:
            put(con,sid,'low_cache','all','info','缓存复用偏低',f"排除首请求后输入 {inp}、缓存 {cache}，占比 {cache/inp:.1%}。",'结合模型、请求间隔与前缀变化检查。',tail[0])
