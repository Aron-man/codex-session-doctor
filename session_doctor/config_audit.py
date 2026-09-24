"""Bounded, read-only audit of local Codex configuration and skill definitions."""

import copy
import hashlib
import json
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ImportError:  # Source checkout still supports Python 3.9.
    tomllib = None


MAX_FILES = 512
MAX_BYTES = 1024 * 1024
TTL = 60
CATALOGUE_MAX_AGE = 24 * 60 * 60


def _line(source, key):
    match = re.search(r'^\s*' + re.escape(key) + r'\s*=', source, re.M)
    return source.count('\n', 0, match.start()) + 1 if match else 1


def _line_value(source, key, value):
    for number, line in enumerate(source.splitlines(), 1):
        if re.match(r'^\s*' + re.escape(key) + r'\s*=', line) and value in line:
            return number
    return _line(source, key)


def _identity(path):
    actual = path.resolve()
    stat = actual.stat()
    return (stat.st_dev, stat.st_ino)


def _time(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    return str(value) if value is not None else None


def _timestamp(value):
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, timezone.utc)
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError, OSError):
        pass
    return None


def _frontmatter(source):
    """Read only simple top-level name/description fields; leave unknown YAML alone."""
    lines = source.splitlines()
    if not lines or lines[0].strip() != '---':
        return None, 1, 'missing'
    end = next((i for i, value in enumerate(lines[1:], 1) if value.strip() == '---'), None)
    if end is None:
        return None, 1, 'unclosed'
    fields = {}
    for i in range(1, end):
        match = re.match(r'^([A-Za-z_][\w-]*):\s*(.*)$', lines[i])
        if not match:
            continue
        key, value = match.groups()
        if key not in ('name', 'description'):
            continue
        if value in ('|', '>', '|-', '>-', '|+', '>+'):
            folded = []
            for next_line in lines[i + 1:end]:
                if next_line and not next_line[0].isspace():
                    break
                folded.append(next_line.strip())
            value = ' '.join(folded).strip()
        elif value.startswith(('"', "'")):
            quote = value[0]
            if not value.endswith(quote) or len(value) == 1:
                continuation = []
                for next_line in lines[i + 1:end]:
                    continuation.append(next_line.strip())
                    if next_line.rstrip().endswith(quote):
                        break
                value += ' '.join(continuation)
            value = value.strip(quote).strip()
        fields[key] = (value.strip(), i + 1)
    return fields, end + 1, None


class ConfigAudit:
    def __init__(self, roots):
        self.roots = [Path(root).expanduser() for root in roots]
        self._cached = None
        self._signature = None
        self._expires = 0
        self._lock = threading.Lock()

    def snapshot(self):
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self):
        paths = self._candidate_paths()
        signature = tuple((str(p), self._stat_signature(p)) for p in paths)
        if self._cached is not None:
            signature += tuple((str(p), self._stat_signature(p)) for p in self._watched_files)
        if self._cached is not None and signature == self._signature and time.monotonic() < self._expires:
            return copy.deepcopy(self._cached)
        result = self._scan(paths)
        self._cached, self._signature = result, signature
        self._signature = tuple((str(p), self._stat_signature(p)) for p in paths + self._watched_files)
        self._expires = time.monotonic() + TTL
        return copy.deepcopy(result)

    @staticmethod
    def _stat_signature(path):
        try:
            stat = path.stat()
            return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        except OSError:
            return None

    def _candidate_paths(self):
        paths = []
        default = Path('~/.codex').expanduser().resolve()
        for root in self.roots:
            paths += [root / 'config.toml', root / 'models_cache.json', root / 'skills']
            if root.resolve() == default:
                paths.append(Path('~/.agents/skills').expanduser())
        return paths

    def _scan(self, candidates):
        now = datetime.now(timezone.utc).isoformat()
        result = {'checked_at': now, 'scope': {'roots': [str(r) for r in self.roots],
                  'skill_directories': [], 'max_files': MAX_FILES, 'max_file_bytes': MAX_BYTES,
                  'coverage': 'complete'}, 'files_checked': 0,
                  'findings': [], 'observations': [], 'errors': []}
        self._result = result
        self._seen = set()
        self._skills = {}
        self._count = 0
        self._limit_reported = False
        self._configs = []
        self._disabled_skills = set()
        self._watched_files = []
        configs = []
        catalogues = []
        directories = []
        for root in self.roots:
            configs.append((root / 'config.toml', root))
            catalogues.append((root / 'models_cache.json', root))
            directories.append(root / 'skills')
            if root.resolve() == Path('~/.codex').expanduser().resolve():
                directories.append(Path('~/.agents/skills').expanduser())
        for config, root in configs:
            _, source = self._read(config, 'config')
            if source is None:
                continue
            if tomllib is None:
                self._gap('TOML 解析不可用：当前 Python 无 tomllib；配置规则未检查', str(config))
                continue
            try:
                data = tomllib.loads(source)
            except tomllib.TOMLDecodeError as exc:
                line = getattr(exc, 'lineno', None) or self._toml_error_line(source, exc)
                self._finding('toml_invalid', config, line, '配置 TOML 无法解析',
                              '标准 TOML 解析器在该行附近报错。', '修正该文件的 TOML 语法后重新检查。')
                continue
            self._check_config(config, root, source, data, directories)
        for path, root in catalogues:
            _, source = self._read(path, 'catalogue', missing_ok=True)
            if source is None:
                self._observation('catalogue_unavailable', path, 1,
                                  '模型目录不可用，无法核对模型配置上限。')
                continue
            try:
                data = json.loads(source)
            except (ValueError, TypeError):
                self._gap('模型目录 JSON 无法解析；模型上限规则未检查', str(path))
                continue
            config = root / 'config.toml'
            config_data = next((item for item in self._configs if item[0] == config), None)
            if config_data:
                self._check_catalogue(config, config_data[1], config_data[2], path, data)
        unique_dirs = []
        for directory in directories:
            try:
                identity = _identity(directory)
            except OSError:
                continue
            if identity not in [entry[0] for entry in unique_dirs]:
                unique_dirs.append((identity, directory))
        result['scope']['skill_directories'] = [str(directory) for _, directory in unique_dirs]
        for _, directory in unique_dirs:
            self._scan_skills(directory)
        if not result['findings']:
            result['scope']['conclusion'] = '未发现有证据的配置问题'
        if result['scope']['coverage'] != 'complete':
            result['scope']['conclusion'] = result['scope'].get('conclusion', '') + '；检查覆盖不完整，见 errors/observations'
        return result

    @staticmethod
    def _toml_error_line(source, exc):
        # Python 3.11/3.12 may omit lineno on TOMLDecodeError.
        match = re.search(r'line (\d+)', str(exc))
        return int(match.group(1)) if match else 1

    def _gap(self, message, path):
        self._result['errors'].append({'path': path, 'message': message})
        self._result['scope']['coverage'] = 'partial'

    def _read(self, path, purpose, missing_ok=False):
        try:
            identity = _identity(path)
            if identity in self._seen:
                return None, None
            self._seen.add(identity)
            stat = path.stat()
            if stat.st_size > MAX_BYTES:
                self._gap('文件超过单文件读取上限，未检查 ' + purpose, str(path))
                return None, None
            if self._count >= MAX_FILES:
                if not self._limit_reported:
                    self._gap('文件数量达到检查上限，剩余文件未检查', str(path))
                    self._limit_reported = True
                return None, None
            source = path.read_text(encoding='utf-8')
            self._count += 1
            self._result['files_checked'] += 1
            self._watched_files.append(path)
            return path.resolve(), source
        except FileNotFoundError:
            if not missing_ok and purpose == 'config':
                self._observation('config_missing', path, 1, '该 root 下未发现 config.toml。')
        except (OSError, UnicodeError):
            self._gap('文件无法读取，未检查 ' + purpose, str(path))
        return None, None

    def _finding(self, kind, path, line, title, evidence, suggestion, details=None,
                 severity='warning', confidence='high'):
        line = line or 1
        path = str(path)
        ident = hashlib.sha256((kind + '|' + path + '|' + str(line) + '|' + evidence).encode()).hexdigest()[:24]
        self._result['findings'].append({'id': 'config:' + ident, 'kind': kind,
            'category': 'configuration', 'severity': severity, 'confidence': confidence,
            'title': title, 'evidence': evidence, 'suggestion': suggestion,
            'target_path': path, 'target_line': line, 'details': details or {}})

    def _observation(self, kind, path, line, evidence):
        path = str(path)
        ident = hashlib.sha256((kind + '|' + path + '|' + evidence).encode()).hexdigest()[:24]
        self._result['observations'].append({'id': 'config:' + ident, 'kind': kind,
            'category': 'configuration', 'severity': 'info', 'confidence': 'info',
            'title': '覆盖与配置观察', 'evidence': evidence, 'suggestion': '结合实际调用确认是否需要调整。',
            'target_path': path, 'target_line': line or 1, 'details': {}})

    def _check_config(self, path, root, source, data, directories):
        self._configs.append((path, data, source))
        instructions = data.get('model_instructions_file')
        if isinstance(instructions, str):
            target = Path(instructions).expanduser()
            if not target.is_absolute():
                target = root / target
            if not target.is_file():
                self._finding('missing_instructions', path, _line(source, 'model_instructions_file'),
                    '模型说明文件不存在', 'model_instructions_file 指向的文件不存在：' + str(target),
                    '核对配置路径或恢复该文件。', {'referenced_path': str(target)})
        skills = data.get('skills', {})
        entries = skills.get('config', []) if isinstance(skills, dict) else []
        if isinstance(entries, dict):
            entries = [entries]
        for item in entries if isinstance(entries, list) else []:
            if not isinstance(item, dict):
                continue
            value = item.get('path')
            if not isinstance(value, str):
                continue
            target = Path(value).expanduser()
            if not target.is_absolute():
                target = root / target
            if item.get('enabled') is False:
                self._disabled_skills.add(str(target.resolve()))
                continue
            if not target.exists():
                self._finding('missing_skill', path, _line_value(source, 'path', value), '启用的 skill 路径不存在',
                    'skills.config.path 指向不存在的路径：' + str(target), '核对路径或禁用该 skill。',
                    {'referenced_path': str(target)})
            else:
                directories.append(target if target.is_dir() else target.parent)
        servers = data.get('mcp_servers', {})
        if isinstance(servers, dict):
            for name, settings in servers.items():
                if not isinstance(settings, dict) or settings.get('enabled') is False or settings.get('disabled') is True:
                    continue
                command = settings.get('command')
                if not isinstance(command, str):
                    continue
                if Path(command).is_absolute() and not Path(command).is_file():
                    self._finding('missing_mcp_command', path, _line_value(source, 'command', command),
                        '启用的 MCP 命令路径不存在', 'MCP ' + str(name) + ' 的绝对 command 路径不存在：' + command,
                        '核对可执行文件路径或禁用该 MCP。', {'server': str(name), 'command_path': command})
                elif not Path(command).is_absolute() and shutil.which(command) is None:
                    self._observation('mcp_command_not_in_path', path, _line_value(source, 'command', command),
                        'MCP ' + str(name) + ' 的裸命令在当前进程 PATH 中未找到；服务运行时 PATH 可能不同。')
        limit = data.get('tool_output_token_limit')
        if isinstance(limit, int) and limit >= 100000:
            self._observation('large_output_limit', path, _line(source, 'tool_output_token_limit'),
                'tool_output_token_limit=' + str(limit) + '；是否合适需结合实际工具调用。')

    @staticmethod
    def _catalogue_entries(data):
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ('models', 'data', 'items'):
                value = data.get(key)
                if isinstance(value, list):
                    return value
        return []

    def _check_catalogue(self, config, data, source, path, catalogue):
        provider = data.get('model_provider', 'openai')
        if provider not in ('openai', None):
            return
        model = data.get('model')
        if not isinstance(model, str):
            return
        entry = next((item for item in self._catalogue_entries(catalogue)
                      if isinstance(item, dict) and model in (item.get('slug'), item.get('id'), item.get('name'))), None)
        if entry is None:
            self._observation('model_not_in_catalogue', path, 1, '所选模型未在本机模型目录中找到；无法核对上限。')
            return
        fetched = _timestamp(catalogue.get('fetched_at')) if isinstance(catalogue, dict) else None
        expires = _timestamp(catalogue.get('expires_at')) if isinstance(catalogue, dict) and 'expires_at' in catalogue else None
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        except OSError:
            modified = None
        snapshot_time = fetched.isoformat() if fetched else (modified.isoformat() if modified else None)
        now = datetime.now(timezone.utc)
        reason = None
        if not fetched:
            reason = '模型目录缺少可解析的 fetched_at，文件 mtime 不能证明目录抓取时间'
        elif isinstance(catalogue, dict) and 'expires_at' in catalogue and not expires:
            reason = '模型目录 expires_at 无法解析'
        elif expires and expires <= now:
            reason = '模型目录 expires_at 已过'
        elif (now - fetched).total_seconds() > CATALOGUE_MAX_AGE or (modified and (now - modified).total_seconds() > CATALOGUE_MAX_AGE):
            reason = '模型目录 fetched_at 或文件 mtime 超过 24 小时审计新鲜度窗口'
        elif fetched > now or (expires and expires <= fetched):
            reason = '模型目录时间戳不一致，无法确认新鲜度'
        if reason:
            self._result['scope']['coverage'] = 'partial'
            self._observation('catalogue_stale', path, 1, reason + '；目录快照 ' + str(snapshot_time) + '。')
        maximum = entry.get('max_context_window')
        chosen = data.get('model_context_window')
        if isinstance(chosen, int) and isinstance(maximum, int) and chosen > maximum:
            evidence = ('model_context_window=' + str(chosen) + ' 超过本机模型目录中 ' + model +
                        ' 的 max_context_window=' + str(maximum) + '；目录快照 ' + str(snapshot_time) + '。')
            if reason:
                self._observation('context_exceeds_stale_catalogue', config, _line(source, 'model_context_window'),
                    evidence + '目录新鲜度不足，仅供核对。')
            else:
                self._finding('context_exceeds_catalogue', config, _line(source, 'model_context_window'),
                    '配置与本机模型目录不一致', evidence, '核对本机模型目录和配置；此设置可能不生效。',
                    {'catalogue_path': str(path), 'catalogue_time': snapshot_time,
                     'configured': chosen, 'catalogue_max': maximum})
        levels = entry.get('supported_reasoning_levels') or entry.get('supported_reasoning_efforts')
        effort = data.get('model_reasoning_effort')
        if isinstance(levels, list) and isinstance(effort, str):
            allowed = [item.get('effort') if isinstance(item, dict) else item for item in levels]
            allowed = [item for item in allowed if isinstance(item, str)]
            if allowed and effort not in allowed:
                evidence = ('model_reasoning_effort=' + effort + '；本机目录中 ' + model + ' 明确列出：' +
                            ', '.join(allowed) + '；目录快照 ' + str(snapshot_time) + '。')
                if reason:
                    self._observation('effort_not_in_stale_catalogue', config, _line(source, 'model_reasoning_effort'),
                        evidence + '目录新鲜度不足，仅供核对。')
                else:
                    self._finding('effort_not_in_catalogue', config, _line(source, 'model_reasoning_effort'),
                        '推理档位与本机模型目录不一致', evidence,
                        '核对模型目录和配置；此设置可能不生效。',
                        {'catalogue_path': str(path), 'catalogue_time': snapshot_time, 'configured': effort,
                         'catalogue_supported': allowed})

    def _scan_skills(self, directory):
        # One level of named skills only. Plugin caches and nested repositories are out of scope.
        try:
            children = sorted(directory.iterdir())
        except OSError:
            self._gap('skill 目录无法列举', str(directory))
            return
        if len(children) > MAX_FILES:
            self._gap('skill 目录条目超过检查上限', str(directory))
            children = children[:MAX_FILES]
        for child in children:
            path = child if child.name == 'SKILL.md' else child / 'SKILL.md'
            if str(path.resolve()) in self._disabled_skills or str(child.resolve()) in self._disabled_skills:
                continue
            if not path.is_file():
                continue
            _, source = self._read(path, 'skill')
            if source is None:
                continue
            fields, end, problem = _frontmatter(source)
            if problem == 'unclosed':
                self._finding('skill_frontmatter_unclosed', path, 1, 'skill frontmatter 未闭合',
                    '文件开头有 ---，但没有结束标记。', '补全 frontmatter 结束标记。')
                continue
            if problem == 'missing':
                self._finding('skill_frontmatter_missing', path, 1, 'skill 缺少 frontmatter',
                    '文件开头没有 --- frontmatter。', '添加 name 与 description 元数据。')
                continue
            for key in ('name', 'description'):
                if not fields.get(key) or not fields[key][0]:
                    self._finding('skill_' + key + '_missing', path, 1,
                        'skill 缺少 ' + key, 'frontmatter 没有非空的 ' + key + ' 字段。',
                        '补全 skill 的 ' + key + ' 字段。')
            name = fields.get('name', (child.name, 1))[0] or child.name
            digest = hashlib.sha256(source.encode()).hexdigest()
            existing = self._skills.get(name)
            if existing and existing[1] != digest:
                self._finding('skill_name_conflict', path, fields.get('name', ('', 1))[1],
                    '同名 skill 定义内容不同', 'skill ' + name + ' 在 ' + str(existing[0]) + ' 与 ' + str(path) +
                    ' 指向不同真实文件，内容哈希不同。', '核对两个定义并统一或改名。',
                    {'other_path': str(existing[0]), 'other_line': existing[2]})
            else:
                self._skills[name] = (path, digest, fields.get('name', ('', 1))[1])
