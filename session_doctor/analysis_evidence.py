"""Build a bounded, read-only evidence packet from indexed Codex rollouts."""

import datetime as dt
import hashlib
import json
import re
from pathlib import Path

from .parser import safe


MAX_LINE = 32 * 1024 * 1024
MAX_SCAN = 512 * 1024 * 1024
MAX_EVENTS = 160
MAX_CHARS = 64000
MAX_TEXT = 2600
ANCHOR = re.compile(r'纠正|更正|不对|不是|过度|偏离|反证|恢复|已解决|改成|不要|应该|actually|instead|wrong|correct|resolved|superseded|overengineer', re.I)
BINARY = re.compile(r'data:[^\s]+;base64,[A-Za-z0-9+/=_-]+|(?:[A-Za-z0-9+/]{120,}={0,2})')
CLI_SECRET = re.compile(r'(?i)(--(?:api[-_]?key|token|password|secret)(?:=|\s+)|authorization\s*[:=]\s*(?:bearer\s+)?)([^\s,;"\']+)')
MYSQL_PASSWORD = re.compile(r'(?i)(?<!\S)-p(?:=|\s+)?([^\s=][^\s]*)')
REQUEST = re.compile(r'(?im)^## My request(?: for Codex)?:\s*')


def _clean(value, limit=MAX_TEXT):
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    value = BINARY.sub('[binary omitted]', value)
    value = CLI_SECRET.sub(lambda m: m.group(1) + '[REDACTED]', value)
    if re.search(r'(?i)(?:^|[;&|]\s*|\s)(?:mysql|mariadb)(?:\s|$)', value):
        value = MYSQL_PASSWORD.sub('-p[REDACTED]', value)
    return safe(value, limit)


def _visible(value):
    """Keep short MCP JSON fields even when they have unfamiliar names."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return value
        return _visible(parsed) if isinstance(parsed, (dict, list)) else value
    if isinstance(value, list):
        return '\n'.join(filter(None, (_visible(part) for part in value[:12])))
    if isinstance(value, dict):
        if value.get('type') in ('image', 'audio') or 'data' in value and 'mimeType' in value:
            return ''
        if value.get('type') == 'text':
            return _visible(value.get('text', ''))
        filtered = {}
        for key, part in value.items():
            if key in ('data', 'raw_content', 'encrypted_content', 'reasoning', 'stderr', 'stdout'):
                continue
            if isinstance(part, dict) and (part.get('type') in ('image', 'audio') or 'data' in part and 'mimeType' in part):
                continue
            if isinstance(part, list):
                filtered[key] = [v for v in (_visible(x) if isinstance(x, (dict, list)) else x for x in part) if v]
            else:
                filtered[key] = _visible(part) if isinstance(part, dict) else part
        return json.dumps(filtered, ensure_ascii=False, sort_keys=True, default=str)
    return ''


def _message_text(item):
    parts = item.get('content') or []
    if isinstance(parts, str):
        return parts
    return '\n'.join(part.get('text', '') for part in parts if isinstance(part, dict) and part.get('type') in ('input_text', 'output_text', 'text') and isinstance(part.get('text'), str))


def _user_text(text):
    matches = list(REQUEST.finditer(text))
    if matches:
        return text[matches[-1].end():].strip(), False
    return text, _context(text)


def _context(text):
    stripped = text.lstrip()
    return bool(stripped.startswith(('# AGENTS.md', '<INSTRUCTIONS>', '<environment_context>', '<recommended_plugins>', '<app-context>', '<skills_instructions>', '<permissions instructions>', '<codex_delegation>', '<codex_internal_context>', '<heartbeat>', '# Developer Instructions', 'Message Type: ', '<system_reminder>', 'Task name:', 'Sender:')) or '<environment_context>' in stripped[:300])


def _event(sid, turn, stamp, kind, raw, path, line, details=None, identity=None):
    identity = identity or f'{sid}|{path}|{line}|{kind}'
    ident = 'E' + hashlib.sha256(identity.encode()).hexdigest()[:16]
    return {'id': ident, 'session_id': sid, 'turn_id': turn, 'timestamp': stamp,
            'kind': kind, 'text': _clean(raw), 'source': {'path': path, 'line': line},
            'details': details or {}}


def _scope(con, sid, include_children):
    if not con.execute('SELECT 1 FROM sessions WHERE id=?', (sid,)).fetchone():
        raise ValueError('Unknown session')
    result = [sid]
    seen = {sid}
    if include_children:
        for current in result:
            rows = con.execute("SELECT id FROM sessions WHERE parent_id=? AND source='subagent' ORDER BY created,id", (current,))
            for row in rows:
                child = row[0]
                if child not in seen:
                    seen.add(child)
                    result.append(child)
    return result


def _records(obj, sid, turn, stamp, path, line):
    payload = obj.get('payload') or {}
    typ = obj.get('type')
    events = []
    if typ == 'response_item':
        kind = payload.get('type')
        if kind == 'message':
            role = payload.get('role')
            if role in ('user', 'assistant'):
                content = _message_text(payload)
                if content:
                    content, context = _user_text(content) if role == 'user' else (content, False)
                    if not content: return events
                    label = 'context' if context else role
                    events.append(_event(sid, turn, stamp, label, content, path, line,
                                         {'role': role, 'source_type': 'response_item'},
                                         f'message|{sid}|{turn}|{role}|{hashlib.sha256(content.encode()).hexdigest()}'))
        elif kind in ('function_call', 'custom_tool_call'):
            command = payload.get('arguments', payload.get('input', ''))
            try:
                parsed = json.loads(command) if isinstance(command, str) else command
            except ValueError:
                parsed = command
            events.append(_event(sid, turn, stamp, 'tool', f"{payload.get('name', 'tool')}: {_visible(parsed)}", path, line,
                                 {'status': 'called', 'source_type': 'response_item'}))
        elif kind in ('function_call_output', 'custom_tool_call_output'):
            output = _visible(payload.get('output', ''))
            if output:
                events.append(_event(sid, turn, stamp, 'tool', output, path, line,
                                     {'status': 'result', 'source_type': 'response_item'}))
    elif typ == 'event_msg':
        sub = payload.get('type')
        if sub == 'item_completed':
            item = payload.get('item') or {}
            kind = item.get('type')
            if kind in ('UserMessage', 'AgentMessage'):
                role = 'user' if kind == 'UserMessage' else 'assistant'
                content = item.get('text') or _message_text(item)
                if content:
                    content, context = _user_text(content) if role == 'user' else (content, False)
                    if not content: return events
                    label = 'context' if context else role
                    events.append(_event(sid, turn, stamp, label, content, path, line,
                                         {'role': role, 'source_type': 'native'},
                                         f'message|{sid}|{turn}|{role}|{hashlib.sha256(content.encode()).hexdigest()}'))
            elif kind in ('CommandExecution', 'McpToolCall'):
                if kind == 'CommandExecution':
                    command = item.get('command') or ''
                    if isinstance(command, list): command = ' '.join(map(str, command))
                    output = next((item.get(k) for k in ('formatted_output', 'aggregated_output') if item.get(k)), '')
                    name = 'exec_command'
                else:
                    command = item.get('arguments') or {}
                    output = item.get('result')
                    name = '.'.join(filter(None, (str(item.get('server') or ''), str(item.get('tool') or '')))) or 'mcp'
                status = item.get('status') or 'unknown'
                if isinstance(output, dict) and output.get('isError'): status = 'failed'
                summary = _visible(output)
                error = item.get('error') or item.get('stderr') or ''
                shown = f'{name}: {_visible(command)}\nstatus: {status}'
                if item.get('exit_code') is not None: shown += f"; exit_code: {item['exit_code']}"
                if error: shown += '\nerror: ' + _clean(error, 240)
                if summary: shown += '\nresult: ' + _clean(summary, 700)
                details = {'status': str(status), 'tool': name, 'source_type': 'native'}
                if item.get('exit_code') is not None: details['exit_code'] = item['exit_code']
                paths = item.get('parsed_cmd') or []
                if isinstance(paths, dict): paths = [paths]
                targets = [p.get('path') for p in paths if isinstance(p, dict) and isinstance(p.get('path'), str)]
                if targets: details['paths'] = [_clean(p, 300) for p in targets[:10]]
                events.append(_event(sid, turn, stamp, 'tool', shown, path, line, details,
                                     f'tool|{sid}|{item.get("id")}' if item.get('id') else None))
            elif kind == 'FileChange':
                changes = item.get('changes') or {}
                paths = list(changes) if isinstance(changes, dict) else [c.get('path') for c in changes if isinstance(c, dict)] if isinstance(changes, list) else []
                paths = [_clean(p, 300) for p in paths if isinstance(p, str)][:30]
                status = str(item.get('status') or 'unknown')
                events.append(_event(sid, turn, stamp, 'file_change',
                                     'File changes: ' + ', '.join(paths) + '; status: ' + status,
                                     path, line, {'paths': paths, 'status': status, 'source_type': 'native'},
                                     f'file|{sid}|{item.get("id")}' if item.get('id') else None))
            elif kind == 'CollabAgentToolCall':
                status = str(item.get('status') or 'unknown')
                target = item.get('agent_thread_id') or item.get('agent_id') or item.get('target') or ''
                action = item.get('tool') or item.get('name') or item.get('kind') or 'collab agent call'
                details = {'status': status, 'source_type': 'native'}
                if target: details['target'] = _clean(str(target), 120)
                events.append(_event(sid, turn, stamp, 'task', f'{action}: {target}; status: {status}',
                                     path, line, details,
                                     f'collab|{sid}|{item.get("id")}' if item.get('id') else None))
            elif kind == 'ContextCompaction':
                events.append(_event(sid, turn, stamp, 'compaction', 'Context compacted', path, line,
                                     {'source_type': 'native'},
                                     f'compaction|{sid}|{item.get("id")}' if item.get('id') else None))
        elif sub in ('task_started', 'task_complete', 'turn_aborted', 'task_failed', 'task_error'):
            details = {'status': sub}
            for key in ('started_at', 'completed_at', 'duration_ms', 'time_to_first_token_ms'):
                if isinstance(payload.get(key), (str, int, float)):
                    details[key] = payload[key]
            summary = sub + ''.join(f'; {key}: {details[key]}' for key in details if key != 'status')
            events.append(_event(sid, turn, stamp, 'task', summary, path, line, details))
    elif typ == 'compacted':
        events.append(_event(sid, turn, stamp, 'compaction', 'Context compacted', path, line))
    return events


def _select(events, focus, char_budget=56000):
    """Reserve coherent goal/correction/result groups before filling spare space."""
    if not events: return []
    words = [word.casefold() for word in re.findall(r'[\u4e00-\u9fff]+|[A-Za-z0-9_]+', focus) if len(word) > 1]
    messages = [i for i, e in enumerate(events) if e['kind'] in ('user', 'assistant')]
    users = [i for i in messages if events[i]['kind'] == 'user']
    assistants = [i for i in messages if events[i]['kind'] == 'assistant']

    def near(index, role, direction):
        candidates = users if role == 'user' else assistants
        return next((i for i in (reversed(candidates) if direction < 0 else candidates)
                     if (i < index if direction < 0 else i > index)), None)

    def focused(event):
        return bool(words and any(word in event['text'].casefold() for word in words))

    groups = []
    if users:
        first = users[0]
        groups.append([first, near(first, 'assistant', 1)])
    if users:
        last = users[-1]
        groups.append([near(last, 'assistant', -1), last, near(last, 'assistant', 1)])
    if assistants:
        groups.append(assistants[-3:])
    for kind in ('file_change', 'tool', 'task', 'compaction'):
        candidates = [i for i, e in enumerate(events) if e['kind'] == kind]
        if kind == 'tool':
            candidates = [i for i in candidates if events[i]['details'].get('status') in ('failed', 'error') or focused(events[i])]
        groups.extend([i] for i in reversed(candidates[-3:]))
    corrections = [i for i in users if ANCHOR.search(events[i]['text']) or focused(events[i])]
    for i in reversed(corrections):
        prior_request = near(i, 'user', -1)
        groups.append([prior_request, near(i, 'assistant', -1), i, near(i, 'assistant', 1)])
    for i in reversed(users):
        groups.append([i, near(i, 'assistant', 1)])
    for i in reversed(assistants):
        if focused(events[i]) or ANCHOR.search(events[i]['text']): groups.append([i])
    for i in reversed(range(len(events))):
        if events[i]['kind'] in ('file_change', 'tool', 'task', 'compaction') and (focused(events[i]) or events[i]['kind'] == 'file_change'):
            groups.append([i])
    for i in reversed(messages): groups.append([i])
    for i in reversed(range(len(events))): groups.append([i])

    chosen = set(); size = 0
    for group in groups:
        pending = [i for i in group if i is not None and i not in chosen]
        if not pending: continue
        cost = sum(len(json.dumps(events[i], ensure_ascii=False)) for i in pending)
        if len(chosen) + len(pending) > MAX_EVENTS or size + cost > char_budget: continue
        chosen.update(pending); size += cost
    return [events[i] for i in sorted(chosen)]


def build_evidence(con, session_id, roots, focus='', include_children=True):
    """Read registered rollouts only; never changes the caller's connection or source files."""
    all_ids = _scope(con, session_id, include_children)
    ids = all_ids[:12]
    notes = []
    if len(ids) < len(all_ids): notes.append(f'Session limit: included {len(ids)} of {len(all_ids)} sessions')
    placeholders = ','.join('?' for _ in ids)
    uncertain = con.execute(f'SELECT COUNT(*) FROM sessions WHERE id IN ({placeholders}) AND (fork=1 OR parent_id IS NOT NULL)', ids).fetchone()[0]
    if uncertain:
        notes.append(f'{uncertain} fork or parent-linked sessions may contain inherited history with rewritten timestamps; message origin is uncertain')
    session = con.execute('SELECT title FROM sessions WHERE id=?', (session_id,)).fetchone()
    allowed = [Path(root).expanduser().resolve() for root in roots]
    records = []
    scanned_files = scanned_bytes = skipped_lines = unreadable_files = 0
    files = con.execute('SELECT path,session_id FROM files ORDER BY path').fetchall()
    registered_files = sum(row['session_id'] in ids for row in files)
    for row in files:
        if row['session_id'] not in ids: continue
        path = Path(row['path'])
        try:
            resolved = path.resolve(strict=True)
            if not any(resolved.is_relative_to(root) for root in allowed):
                unreadable_files += 1
                continue
            if scanned_bytes >= MAX_SCAN:
                notes.append('Scan byte limit reached'); break
            with resolved.open('rb') as stream:
                scanned_files += 1
                sid = row['session_id']; created = ''; turn = None
                line = 0
                while True:
                    raw = stream.readline(MAX_LINE + 1)
                    if not raw: break
                    line += 1
                    scanned_bytes += len(raw)
                    if scanned_bytes > MAX_SCAN:
                        notes.append('Scan byte limit reached'); break
                    if len(raw) > MAX_LINE:
                        while not raw.endswith(b'\n'):
                            raw = stream.readline(MAX_LINE + 1)
                            if not raw: break
                            scanned_bytes += len(raw)
                            if scanned_bytes > MAX_SCAN:
                                notes.append('Scan byte limit reached'); break
                        skipped_lines += 1
                        if scanned_bytes > MAX_SCAN: break
                        continue
                    if not raw.endswith(b'\n'):
                        skipped_lines += 1; continue
                    try: obj = json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        skipped_lines += 1; continue
                    if not isinstance(obj, dict): continue
                    payload = obj.get('payload') or {}
                    stamp = obj.get('timestamp') or ''
                    if obj.get('type') == 'session_meta':
                        sid = payload.get('id') or sid
                        created = payload.get('timestamp') or stamp
                        if sid != row['session_id']:
                            notes.append('Registered file session identity mismatch')
                        continue
                    if sid not in ids or payload.get('thread_id') and payload['thread_id'] != sid or payload.get('session_id') and payload['session_id'] != sid:
                        skipped_lines += 1; continue
                    if created and stamp and stamp < created:
                        skipped_lines += 1; continue
                    if obj.get('type') == 'turn_context': turn = payload.get('turn_id') or turn
                    if obj.get('type') == 'event_msg' and payload.get('type') == 'task_started': turn = payload.get('turn_id') or turn
                    records.extend(_records(obj, sid, payload.get('turn_id') or turn, stamp, str(resolved), line))
                if scanned_bytes > MAX_SCAN: break
        except OSError:
            unreadable_files += 1
    if unreadable_files:
        notes.append(f'{unreadable_files} registered source files unavailable or outside configured roots')
    if not scanned_files:
        notes.append('No readable registered source files')
    if skipped_lines: notes.append(f'{skipped_lines} invalid, oversized, incomplete, or inherited lines skipped')
    # Native messages have fuller provenance than mirrored response items.
    records.sort(key=lambda e: (e['timestamp'] or '', e['source']['path'], e['source']['line']))
    unique = {}
    for event in records:
        old = unique.get(event['id'])
        if old is None or event['details'].get('source_type') == 'native' and old['details'].get('source_type') != 'native':
            unique[event['id']] = event
    events = sorted(unique.values(), key=lambda e: (e['timestamp'] or '', e['source']['path'], e['source']['line']))
    selected = _select(events, focus)
    if len(selected) < len(events): notes.append(f'Event sample: selected {len(selected)} of {len(events)} events')
    metric = con.execute(f'''SELECT COALESCE(SUM(input),0),COALESCE(SUM(cached),0),COALESCE(SUM(output),0),
        COALESCE(SUM(reasoning),0),COALESCE(SUM(total),0),COALESCE(SUM(requests),0)
        FROM usage WHERE session_id IN ({placeholders})''', ids).fetchone()
    packet = {'schema_version': 'analysis-evidence-v1', 'session_id': session_id,
              'title': _clean(session['title'] or '', 160), 'focus': _clean(focus, 500),
              'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
              'scope': {'session_ids': ids, 'included_sessions': len(ids), 'total_sessions': len(all_ids)},
              'coverage': {'partial': False, 'notes': notes, 'scanned_files': scanned_files,
                           'registered_files': registered_files, 'unreadable_files': unreadable_files,
                           'scanned_bytes': scanned_bytes, 'skipped_lines': skipped_lines,
                           'omitted_sessions': len(all_ids) - len(ids),
                           'selected_events': len(selected), 'omitted_events': len(events) - len(selected)},
              'metrics': dict(zip(('input', 'cached', 'output', 'reasoning', 'total', 'requests'), metric)),
              'events': selected}
    packet['metrics']['scope'] = 'included_sessions_history'
    if len(json.dumps(packet, ensure_ascii=False)) > MAX_CHARS:
        selected[:] = _select(events, focus, 48000)
        packet['coverage']['omitted_events'] = len(events) - len(selected)
        packet['coverage']['selected_events'] = len(selected)
    if packet['coverage']['omitted_events'] and not any(note.startswith('Event sample') for note in notes):
        notes.append('Packet character limit reduced the event sample')
    packet['coverage']['partial'] = bool(notes)
    return packet
