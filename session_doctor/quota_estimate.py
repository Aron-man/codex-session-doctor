"""Estimate a session's share of the signed-in account's weekly limit.

Credit prices are relative weights only; they are not the subscription debit.
"""

import datetime as dt
import math
from pathlib import Path


RATES = {
    'gpt-6-astra': (250, 25, 1250),
    'gpt-6-sol': (50, 5, 250),
    'gpt-6-luna': (2.5, 0.25, 12.5),
    'gpt-5.6-sol': (100, 10, 500),
    'gpt-5.6-terra': (50, 5, 300),
    'gpt-5.6-luna': (5, 0.5, 30),
    'gpt-5.5': (125, 12.5, 750),
    'gpt-5.4': (62.5, 6.25, 375),
    'gpt-5.4-mini': (18.75, 1.875, 113),
}
METHOD = 'credit_weighted_share_v1'
RATE_SOURCE = 'https://learn.chatgpt.com/docs/pricing'
ASSUMPTIONS = [
    '官方 Standard credit 费率仅作模型相对权重，不能确定订阅额度实际扣额',
    '仅分摊第一个 Codex 根目录中本机已采集的当前周期已知模型用量',
    '缓存输入属于输入，推理输出属于输出；不重复加算',
    '不包含 Fast 倍率、工具、图片、语音、远端或其他共享额度消耗',
]


def _instant(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone(dt.timezone.utc) if parsed.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        return None


def estimate(con, session_id, codex_home, snapshot, now=None):
    """Return the estimate contract, including a reason for every unavailable result."""
    weekly = snapshot.get('weekly') if snapshot else None
    result = {
        'available': False, 'estimated': True, 'percent': None, 'reason': None,
        'window_start': None, 'window_end': None, 'as_of': None,
        'stale': bool(snapshot and snapshot.get('stale')),
        'account_used_percent': None, 'session_weight': None, 'total_weight': None,
        'known_token_coverage_percent': None, 'unknown_models': [],
        'method': METHOD, 'rates_version': '2026-09-23',
        'rate_source': RATE_SOURCE, 'assumptions': ASSUMPTIONS,
    }
    if not weekly:
        result['reason'] = (snapshot or {}).get('error') or '本周额度尚未取得'
        return result
    reset = weekly.get('resets_at')
    minutes = weekly.get('window_minutes')
    used = weekly.get('used_percent')
    if not isinstance(reset, (int, float)) or isinstance(reset, bool) or not math.isfinite(reset) or not isinstance(minutes, (int, float)) or isinstance(minutes, bool) or minutes <= 0:
        result['reason'] = '官方周窗口或重置时间缺失'
        return result
    start = reset - minutes * 60
    result['window_start'], result['window_end'] = start, reset
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.timestamp() >= reset:
        result['reason'] = '周额度已到重置时间，等待官方新快照'
        return result
    if not isinstance(used, (int, float)) or isinstance(used, bool) or not math.isfinite(used):
        result['reason'] = '官方已用百分比缺失'
        return result
    result['account_used_percent'] = used
    updated = _instant(snapshot.get('updated_at'))
    if updated is None:
        result['reason'] = '额度快照时间缺失'
        return result
    as_of = min(updated, dt.datetime.fromtimestamp(reset, dt.timezone.utc))
    result['as_of'] = as_of.isoformat()
    if as_of.timestamp() < start:
        result['reason'] = '额度快照早于当前周期'
        return result

    # SQL narrows by calendar day; Python compares instants exactly, including
    # subsecond boundaries and mixed ISO-8601 timestamp precision.
    start_day = dt.datetime.fromtimestamp(start, dt.timezone.utc).date().isoformat()
    end_day = as_of.date().isoformat()
    root = str(Path(codex_home).expanduser().absolute()).rstrip('/') + '/'
    session_weight = total_weight = 0.0
    known_tokens = all_tokens = 0
    unknown = set()
    target_unknown = False
    target_records = 0
    for row in con.execute('''SELECT session_id,timestamp,model,input,cached,output,total,path
                              FROM usage WHERE timestamp>=? AND timestamp<?''',
                           (start_day, end_day + 'T~')):
        if not (row['path'] or '').startswith(root):
            continue
        instant = _instant(row['timestamp'])
        if instant is None or not start <= instant.timestamp() <= as_of.timestamp():
            continue
        target = row['session_id'] == session_id
        if target:
            target_records += 1
        tokens = max(0, row['total'] or 0)
        all_tokens += tokens
        rates = RATES.get(row['model'])
        if rates is None:
            unknown.add(row['model'] or 'unknown')
            target_unknown |= target
            continue
        known_tokens += tokens
        input_tokens = max(0, row['input'] or 0)
        cached = min(input_tokens, max(0, row['cached'] or 0))
        output = max(0, row['output'] or 0)
        weight = ((input_tokens - cached) * rates[0] + cached * rates[1] + output * rates[2]) / 1000000
        total_weight += weight
        if target:
            session_weight += weight
    result.update(session_weight=session_weight, total_weight=total_weight,
                  known_token_coverage_percent=100 * known_tokens / all_tokens if all_tokens else None,
                  unknown_models=sorted(unknown))
    if total_weight == 0:
        result['reason'] = '本机本周期已知模型加权用量为 0，无法分摊'
    elif target_unknown:
        result['reason'] = '该会话本周期包含未知模型用量，无法完整估算'
    elif not target_records:
        result.update(available=True, percent=0, reason='该会话本周期无已记录用量')
    else:
        result.update(available=True, percent=used * session_weight / total_weight)
    return result
