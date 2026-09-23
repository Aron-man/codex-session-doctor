import hashlib


READ_PREFIX = ('rg ', 'grep ', 'cat ', 'sed ', 'head ', 'tail ', 'find ', 'ls ', 'git show ', 'git diff ', 'git log ')
LARGE_OUTPUT = 20000
LARGE_CONTEXT = 150000
LOW_CACHE_INPUT = 100000


def put(con, sid, kind, key, severity, title, evidence, suggestion, row, estimate=0):
    iid=hashlib.sha256((sid+'|'+kind+'|'+key).encode()).hexdigest()[:32]
    con.execute('''INSERT INTO issues(id,session_id,kind,severity,title,evidence,suggestion,timestamp,turn_id,path,line,estimated_tokens)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET evidence=excluded.evidence,estimated_tokens=excluded.estimated_tokens''',
      (iid,sid,kind,severity,title,evidence,suggestion,row['timestamp'],row['turn_id'],row['path'],row['line'],estimate))


def analyze(con, sid):
    con.execute('DELETE FROM issues WHERE session_id=?',(sid,))
    calls=list(con.execute('SELECT * FROM calls WHERE session_id=? ORDER BY timestamp,line',(sid,)))
    by_turn={}
    for c in calls:
        by_turn.setdefault(c['turn_id'] or '',[]).append(c)
        if c['output_chars']>=LARGE_OUTPUT or c['truncated']:
            put(con,sid,'large_output',c['id'],'warning','工具输出过大或被截断',
                f"{c['name']} 输出 {c['output_chars']} 字符，约 {c['estimated_tokens']} token；日志行 {c['line']}",
                '限制输出字段或页数，先用 rg 定位，再读取所需范围。',c,c['estimated_tokens'])
    for turn, items in by_turn.items():
        groups={}
        for c in items:
            command=(c['command'] or '').strip()
            if command.startswith(READ_PREFIX) and c['output_chars']>=2000 and c['output_hash']:
                groups.setdefault((command,c['output_hash']),[]).append(c)
        for (cmd,_), matching in groups.items():
            if len(matching)>=3:
                first=matching[0]
                put(con,sid,'repeated_read',turn+'|'+cmd,'warning','重复读取相同内容',
                    f"同轮次相同命令返回相同内容 {len(matching)} 次，单次 {first['output_chars']} 字符；日志行 {', '.join(str(x['line']) for x in matching[:8])}",
                    '复用已读内容；后续检索缩小命令范围或行区间。',first,(len(matching)-1)*first['estimated_tokens'])
        streak=[]
        for c in items+[None]:
            if c is not None and c['status']=='failed' and (not streak or c['command']==streak[-1]['command']):
                streak.append(c)
            else:
                if len(streak)>=3:
                    first=streak[0]
                    put(con,sid,'repeated_failure',turn+'|'+first['id'],'warning','连续重复失败',
                        f"同轮次相同命令连续失败 {len(streak)} 次；首次日志行 {first['line']}",
                        '读取最早错误和命令帮助或 schema，改变输入后再重试。',first)
                streak=[c] if c is not None and c['status']=='failed' else []
    uses=list(con.execute("SELECT * FROM usage WHERE session_id=? AND requests=1 ORDER BY timestamp,line",(sid,)))
    by_turn={}
    for u in uses:
        by_turn.setdefault(u['turn_id'] or '',[]).append(u)
    for turn, items in by_turn.items():
        large=[x for x in items if x['input']>=LARGE_CONTEXT]
        if len(large)>=3:
            r=large[0]
            put(con,sid,'large_context',turn,'info','持续大上下文，建议检查',
                f"同轮次 {len(large)} 个请求输入至少 {LARGE_CONTEXT} token；输入 {sum(x['input'] for x in large)}，缓存 {sum(x['cached'] for x in large)}，非缓存 {sum(x['input']-x['cached'] for x in large)}。",
                '先判断这些内容是否与任务相关；精简重复说明和读取内容，在安全节点拆分会话。',r)
    if len(uses)>=4:
        tail=uses[1:]
        inp=sum(x['input'] for x in tail)
        cache=sum(x['cached'] for x in tail)
        if len(tail)>=3 and inp>=LOW_CACHE_INPUT and cache/inp<0.3:
            r=tail[0]
            put(con,sid,'low_cache','all','info','缓存复用偏低，建议检查',
                f"排除首请求后 {len(tail)} 个请求，输入 {inp}、缓存 {cache}，占比 {cache/inp:.1%}。",
                '检查模型切换、请求间隔和前缀变化；不能仅据此断定缓存故障。',r)
