import hashlib
import json
import re


SECRET = re.compile(r'''(?i)((?:bearer\s+)|(?:["']?(?:api[_-]?key|token|password|secret)["']?\s*[:=]\s*["']?))([^\s,;"']+)''')
BARE_SECRET = re.compile(r'(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,}')


def safe(value, limit=500):
    value = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    value=SECRET.sub(r'\1[REDACTED]', value.replace('\x00', ''))
    return BARE_SECRET.sub('[REDACTED_SECRET]',value)[:limit]


def digest(value):
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(value.encode('utf-8', 'replace')).hexdigest()


def usage(raw):
    raw = raw if isinstance(raw, dict) else {}
    def n(key):
        try:
            return max(0, int(raw.get(key) or 0))
        except (ValueError, TypeError):
            return 0
    i, c, o, r = n('input_tokens'), n('cached_input_tokens'), n('output_tokens'), n('reasoning_output_tokens')
    return {'input': i, 'cached': c, 'output': o, 'reasoning': r,
            'total': n('total_tokens') if raw.get('total_tokens') is not None else i + o}


def difference(new, old):
    return {key: max(0, new[key] - old[key]) for key in ('input', 'cached', 'output', 'reasoning', 'total')}


def title_from(item):
    if item.get('type') != 'message' or item.get('role') != 'user':
        return None
    for part in item.get('content') or []:
        if isinstance(part, dict) and part.get('type') in ('input_text', 'text') and part.get('text'):
            if part['text'].lstrip().startswith(('<environment_context>','<recommended_plugins>','# AGENTS.md','<INSTRUCTIONS>')):
                continue
            for line in part['text'].strip().splitlines():
                line=line.strip()
                if line and not line.startswith(('<environment_context>','<recommended_plugins>','# AGENTS.md','<INSTRUCTIONS>','</','<')):
                    return safe(line, 160)
    return None


def call_status(output):
    if isinstance(output, dict):
        if output.get('isError') is True or output.get('error'):
            return 'failed'
        if isinstance(output.get('exit_code'), int):
            return 'success' if output['exit_code'] == 0 else 'failed'
        if output.get('status') in ('failed','error'):
            return 'failed'
        nested=[output[k] for k in ('result','value','content','output','text') if k in output]
        found=[call_status(x) for x in nested]
        return 'failed' if 'failed' in found else ('success' if 'success' in found else 'unknown')
    if isinstance(output, list):
        found=[call_status(x) for x in output]
        return 'failed' if 'failed' in found else ('success' if 'success' in found else 'unknown')
    if not isinstance(output, str): return 'unknown'
    match = re.search(r'(?im)^(?:Process )?exited with code\s+(\d+)|^exit_code\s*[=:]\s*(\d+)', output[:500])
    if match:
        return 'success' if int(match.group(1) or match.group(2)) == 0 else 'failed'
    try:
        parsed=json.loads(output)
    except (ValueError,TypeError):
        parsed=None
    if isinstance(parsed,(dict,list)):
        return call_status(parsed)
    return 'unknown'


def output_text(output):
    if isinstance(output,str):
        try:
            obj=json.loads(output)
        except (ValueError,TypeError):
            obj=None
        if isinstance(obj,(dict,list)):
            return output_text(obj)
        return re.sub(r'data:[^\s]+;base64,[A-Za-z0-9+/=_-]+','[binary omitted]',output)
    if isinstance(output,list):
        return '\n'.join(filter(None,(output_text(x) for x in output)))
    if isinstance(output,dict):
        if output.get('type') in ('image','audio') or 'data' in output and 'mimeType' in output:
            return ''
        if output.get('type')=='text':
            return output_text(output.get('text',''))
        fields=[output_text(output[field]) for field in ('result','value','content','output','text') if field in output]
        if fields:return '\n'.join(filter(None,fields))
    return ''


def output_fingerprint(text):
    lines=text.splitlines(keepends=True)
    prefix=[]
    for line in lines[:12]:
        if line.startswith(('Wall time:','Chunk ID:','Process exited with code','Process running with session ID')):
            continue
        prefix.append(line)
    return digest(''.join(prefix)+''.join(lines[12:]))


def command_from(item):
    raw = item.get('arguments', item.get('input', ''))
    try:
        obj = json.loads(raw) if isinstance(raw, str) and raw.startswith('{') else raw
    except ValueError:
        obj = raw
    if isinstance(obj, dict):
        command=obj.get('cmd', obj.get('command', obj.get('code', obj)))
    else:
        command=obj
    if item.get('name') in ('functions.exec','exec') and isinstance(command,str):
        matches=re.findall(r'exec_command\s*\(\s*\{\s*cmd\s*:\s*("(?:\\.|[^"\\])*")',command)
        if len(matches)==1:
            try:
                command=json.loads(matches[0])
            except ValueError:
                pass
    return safe(command,500)
