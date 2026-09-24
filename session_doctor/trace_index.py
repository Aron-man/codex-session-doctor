"""Incremental native tool-event index; never stores full output."""
import json
import re
import shlex
from pathlib import Path
from urllib.parse import unquote, urlparse

from .parser import digest, output_text, safe

MAX_LINE = 32 * 1024 * 1024
TRUNCATION = re.compile(r'(?m)^(?:Warning: truncated output(?: \(|$)|…\s*\d+ tokens truncated\s*…$|\[?output truncated\]?$)')
FILE_ERROR = re.compile(r'(?i)(?:no such file|permission denied|cannot open|can.t (?:open|read)|failed to (?:open|read)|not found|is a directory)')


def ensure_schema(con):
    con.executescript("""CREATE TABLE IF NOT EXISTS trace_files(path TEXT PRIMARY KEY,dev INTEGER,ino INTEGER,size INTEGER,offset INTEGER DEFAULT 0,line INTEGER DEFAULT 0,session_id TEXT,state TEXT DEFAULT '{}',status TEXT DEFAULT 'active',bad_lines INTEGER DEFAULT 0,oversize_lines INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS tool_events(id TEXT PRIMARY KEY,session_id TEXT,turn_id TEXT,timestamp TEXT,tool TEXT,command TEXT,command_hash TEXT,cwd TEXT,target_paths TEXT,skill_paths TEXT,skill_evidence TEXT,status TEXT,exit_code INTEGER,error_excerpt TEXT,output_chars INTEGER,output_hash TEXT,truncated INTEGER,context_epoch INTEGER,path TEXT,line INTEGER);
    CREATE INDEX IF NOT EXISTS tool_events_session_time ON tool_events(session_id,timestamp);
    """)
    columns={r['name'] for r in con.execute('PRAGMA table_info(trace_files)')}
    for name in ('bad_lines','oversize_lines'):
        if name not in columns: con.execute(f'ALTER TABLE trace_files ADD COLUMN {name} INTEGER DEFAULT 0')
    if 'skill_evidence' not in {r['name'] for r in con.execute('PRAGMA table_info(tool_events)')}:
        con.execute('ALTER TABLE tool_events ADD COLUMN skill_evidence TEXT')


def _command(raw):
    if isinstance(raw, list):
        if len(raw)==1: return str(raw[0])
        if len(raw)>=3 and Path(str(raw[0])).name in ('bash','zsh','sh') and str(raw[1]) in ('-c','-lc','-ic'):
            return str(raw[2])
        return ' '.join(shlex.quote(str(x)) for x in raw)
    return raw if isinstance(raw, str) else ''


def _shell_segments(command):
    try:
        lexer=shlex.shlex(command,posix=True,punctuation_chars=';&|')
        lexer.whitespace_split=True
        lexer.commenters=''
        tokens=list(lexer)
    except ValueError:
        return []
    segments=[]; current=[]
    for token in tokens:
        if token in (';', '&&', '||', '|', '&'):
            if current: segments.append(current); current=[]
        else:
            current.append(token)
    if current: segments.append(current)
    return segments


def _read_segments(command,cwd):
    """Recognize only simple file reads; unknown shell constructs yield no paths."""
    operations=[]
    for tokens in _shell_segments(command):
        if not tokens or tokens[0] not in ('cat','sed','head','tail') or any('$(' in x or '`' in x for x in tokens): continue
        words=[]; discarded=False; i=1
        while i<len(tokens):
            token=tokens[i]
            if token in ('>/dev/null','1>/dev/null','>'):
                if token!='>' or i+1<len(tokens) and tokens[i+1]=='/dev/null': discarded=True
                i+=2 if token=='>' else 1
                continue
            if token.startswith(('>', '1>', '2>', '<', '&>')):
                i+=1
                continue
            words.append(token); i+=1
        tool=tokens[0]; operands=[]; script_seen=False; skip_next=False
        for word in words:
            if skip_next: skip_next=False; continue
            if tool in ('head','tail') and word in ('-n','-c'):
                skip_next=True; continue
            if tool=='sed' and word in ('-e','-f'):
                skip_next=True; script_seen=True; continue
            if word.startswith('-'): continue
            if tool=='sed' and not script_seen:
                script_seen=True; continue
            if word!='-': operands.append(word)
        paths=[]
        for raw in operands:
            if raw.startswith(('-', '>', '<')) or raw in ('&&','||',';','|') or raw=='/dev/null': continue
            p=Path(raw)
            paths.append(safe(str(p if p.is_absolute() else Path(cwd)/p),500))
        operations.append({'paths':list(dict.fromkeys(paths)),'discarded':discarded})
    return operations


def _paths(item, command, cwd=''):
    parsed = item.get('parsed_cmd') or []
    if isinstance(parsed, dict): parsed = [parsed]
    paths = []
    for part in parsed if isinstance(parsed, list) else []:
        if not isinstance(part, dict): continue
        kind = str(part.get('type') or part.get('kind') or '').lower()
        if kind in ('read', 'search'):
            value = part.get('path') or part.get('paths')
            values = value if isinstance(value, list) else [value] if isinstance(value, str) else []
            paths.extend(x for x in values if kind != 'search' or isinstance(x, str) and ('/' in x or x.startswith('.')))
    if not paths:
        return list(dict.fromkeys(p for op in _read_segments(command,cwd) for p in op['paths']))[:30]
    resolved=[]
    for x in paths:
        if not isinstance(x,str): continue
        p=Path(x)
        if not p.is_absolute() and cwd: p=Path(cwd)/p
        resolved.append(safe(str(p),500))
    return list(dict.fromkeys(resolved))[:30]


def _output(item):
    if item.get('type')=='McpToolCall':
        return output_text(item.get('result'))
    for key in ('formatted_output', 'aggregated_output', 'stdout'):
        value = item.get(key)
        if isinstance(value, str) and value: return value
    return ''


def _skill_paths(item,command,cwd,target):
    parsed=item.get('parsed_cmd') or []
    if isinstance(parsed,dict): parsed=[parsed]
    if isinstance(parsed,list) and any(isinstance(p,dict) and str(p.get('type') or p.get('kind') or '').lower() in ('read','search') for p in parsed):
        reads=[]
        for part in parsed:
            if not isinstance(part,dict) or str(part.get('type') or part.get('kind') or '').lower()!='read': continue
            raw=part.get('path')
            if isinstance(raw,str) and Path(raw).name=='SKILL.md':
                reads.append(safe(str(Path(raw) if Path(raw).is_absolute() else Path(cwd)/raw),500))
        return list(dict.fromkeys(p for p in reads if p in target))
    return [p for op in _read_segments(command,cwd) for p in op['paths'] if p in target and Path(p).name=='SKILL.md']


def _skill_evidence(item, command, skills, cwd, visible):
    """Summarize evidence per skill without persisting a full command or output."""
    parsed=item.get('parsed_cmd') or []
    if isinstance(parsed,dict): parsed=[parsed]
    parts=[p for p in parsed if isinstance(p,dict)] if isinstance(parsed,list) else []
    segments=_shell_segments(command)
    reads=_read_segments(command,cwd)
    evidence={}
    text=visible+'\n'+str(item.get('stderr') or '')
    for skill in skills:
        raw_paths=[]
        for part in parts:
            if str(part.get('type') or part.get('kind') or '').lower()!='read': continue
            raw=part.get('path')
            if isinstance(raw,str):
                resolved=str(Path(raw) if Path(raw).is_absolute() else Path(cwd)/raw)
                if resolved==skill: raw_paths.append(raw)
        if not raw_paths: raw_paths=[skill]
        matching=[op for op in reads if skill in op['paths']]
        discarded=any(op['discarded'] for op in matching)
        sole_read=len(segments)==1 and len(reads)==1 and len(matching)==1 and (not parts or len(parts)==1 and str(parts[0].get('type') or parts[0].get('kind') or '').lower() in ('read','unknown'))
        error=''
        for line in text.splitlines():
            if FILE_ERROR.search(line) and any(raw in line for raw in raw_paths):
                error=safe(line.strip(),300)
                break
        evidence[skill]={'discarded':discarded,'sole_read':sole_read,'file_error':error}
    return evidence


def _insert(con, obj, path, line, state):
    kind, payload = obj.get('type'), obj.get('payload') or {}
    if kind == 'session_meta':
        state['sid'] = payload.get('id') or state['sid']
        state['created'] = payload.get('timestamp') or obj.get('timestamp') or ''
    elif kind == 'turn_context': state['turn'] = payload.get('turn_id') or state.get('turn')
    elif kind == 'compacted': state['epoch'] = state.get('epoch', 0) + 1
    elif kind == 'event_msg' and payload.get('type') == 'task_started': state['turn'] = payload.get('turn_id') or state.get('turn')
    elif kind == 'event_msg' and payload.get('type') == 'item_completed':
        item = payload.get('item') or {}
        if not isinstance(item, dict): return
        typ = item.get('type')
        if typ == 'ContextCompaction':
            state['epoch'] = state.get('epoch', 0) + 1
            return
        if typ not in ('CommandExecution', 'McpToolCall'): return
        sid = state['sid']
        if payload.get('thread_id') and payload['thread_id'] != sid: return
        if state.get('created') and obj.get('timestamp') and obj['timestamp'] < state['created']: return
        native_id = item.get('id')
        eid = digest([sid, native_id])[:40] if native_id else digest([sid, path, line, typ])[:40]
        turn = payload.get('turn_id') or item.get('turn_id') or state.get('turn')
        if typ == 'CommandExecution':
            tool, command = 'exec_command', _command(item.get('command'))
            cwd = item.get('cwd') or ''
            if isinstance(cwd, str) and cwd.startswith('file://'): cwd = unquote(urlparse(cwd).path)
            target = _paths(item, command, cwd)
        else:
            tool = '.'.join(filter(None, (str(item.get('server') or ''), str(item.get('tool') or '')))) or 'mcp.unknown'
            args = item.get('arguments') or {}
            command = args if isinstance(args, str) else json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
            target, cwd = [], ''
        output = _output(item)
        error = item.get('error') or item.get('stderr') or ''
        result = item.get('result')
        if typ == 'McpToolCall' and isinstance(result, dict) and result.get('isError'):
            error = error or result.get('error') or output_text(result) or 'MCP result isError'
        status = str(item.get('status') or 'unknown').lower()
        if typ == 'McpToolCall' and isinstance(result, dict) and result.get('isError'):
            status = 'failed'
        skills = _skill_paths(item,command,cwd,target) if typ=='CommandExecution' else []
        skill_evidence = _skill_evidence(item,command,skills,cwd,output) if typ=='CommandExecution' and skills else {}
        relevant_error = next((v['file_error'] for v in skill_evidence.values() if v['file_error']), '')
        error = relevant_error or error
        code = item.get('exit_code') if isinstance(item.get('exit_code'), int) else None
        con.execute('''INSERT INTO tool_events(id,session_id,turn_id,timestamp,tool,command,command_hash,cwd,target_paths,skill_paths,skill_evidence,status,exit_code,error_excerpt,output_chars,output_hash,truncated,context_epoch,path,line)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
          turn_id=excluded.turn_id,timestamp=excluded.timestamp,tool=excluded.tool,command=excluded.command,command_hash=excluded.command_hash,cwd=excluded.cwd,target_paths=excluded.target_paths,skill_paths=excluded.skill_paths,skill_evidence=excluded.skill_evidence,status=excluded.status,exit_code=excluded.exit_code,error_excerpt=excluded.error_excerpt,output_chars=excluded.output_chars,output_hash=excluded.output_hash,truncated=excluded.truncated,context_epoch=excluded.context_epoch,path=excluded.path,line=excluded.line
          WHERE excluded.timestamp>tool_events.timestamp OR (excluded.timestamp=tool_events.timestamp AND excluded.line>=tool_events.line AND (tool_events.status NOT IN ('completed','success','failed','error') OR excluded.status IN ('completed','success','failed','error')))''',
          (eid, sid, turn, obj.get('timestamp') or '', tool, safe(command, 240), digest(command), safe(cwd, 500), json.dumps(target, ensure_ascii=False), json.dumps(skills, ensure_ascii=False), json.dumps(skill_evidence,ensure_ascii=False), status, code, safe(error, 300), len(output), digest(output) if output else None, int(bool(TRUNCATION.search(output[:500] + '\n' + output[-1000:]))), state.get('epoch', 0), path, line))


def update(con, path, sid):
    """Consume complete lines from one file after ensure_schema, preserving a cursor."""
    path = str(path)
    st = Path(path).stat()
    row = con.execute('SELECT * FROM trace_files WHERE path=?', (path,)).fetchone()
    if row and row['dev'] == st.st_dev and row['ino'] == st.st_ino and st.st_size >= row['offset']:
        offset, line = row['offset'], row['line']
        bad_lines, oversize_lines = row['bad_lines'], row['oversize_lines']
        try: state = json.loads(row['state'])
        except ValueError: state = {'sid': sid}
    else:
        offset, line, state = 0, 0, {'sid': sid, 'epoch': 0}
        bad_lines, oversize_lines = 0, 0
    previous_offset = offset
    if st.st_size == offset:
        con.execute('UPDATE trace_files SET size=?,status=? WHERE path=?', (st.st_size, 'archived' if 'archived_sessions' in Path(path).parts else 'active', path))
        return False
    with open(path, 'rb') as f:
        f.seek(offset)
        while True:
            start = f.tell(); b = f.readline(MAX_LINE + 1)
            if not b: break
            if len(b) > MAX_LINE:
                while b and not b.endswith(b'\n'): b = f.readline(MAX_LINE + 1)
                if not b.endswith(b'\n'): f.seek(start); break
                line += 1; oversize_lines += 1; continue
            if not b.endswith(b'\n'): f.seek(start); break
            line += 1
            try: obj = json.loads(b)
            except (ValueError, UnicodeDecodeError):
                bad_lines += 1
                continue
            if isinstance(obj, dict): _insert(con, obj, path, line, state)
        offset = f.tell()
    con.execute('''INSERT INTO trace_files(path,dev,ino,size,offset,line,session_id,state,status,bad_lines,oversize_lines) VALUES(?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(path) DO UPDATE SET dev=excluded.dev,ino=excluded.ino,size=excluded.size,offset=excluded.offset,line=excluded.line,session_id=excluded.session_id,state=excluded.state,status=excluded.status,bad_lines=excluded.bad_lines,oversize_lines=excluded.oversize_lines''',
      (path, st.st_dev, st.st_ino, st.st_size, offset, line, state['sid'], json.dumps(state), 'archived' if 'archived_sessions' in Path(path).parts else 'active',bad_lines,oversize_lines))
    return offset > previous_offset
