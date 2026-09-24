import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from session_doctor import config_audit


class ConfigAuditTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.home = self.base / 'home'
        self.home.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative, value, root=None):
        path = (root or self.home) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding='utf-8')
        return path

    def scan(self, roots=None):
        return config_audit.ConfigAudit(roots or [self.home]).snapshot()

    @unittest.skipIf(config_audit.tomllib is None, 'standard TOML parser unavailable')
    def test_missing_paths_and_line_numbers(self):
        self.write('config.toml', '# synthetic\na=1\nmodel_instructions_file="missing.md"\n'
                   '[[skills.config]]\npath="gone"\n'
                   '[mcp_servers.active]\ncommand="/unavailable/doctor-synthetic"\n'
                   '[mcp_servers.off]\nenabled=false\ncommand="/unavailable/disabled"\n')
        result = self.scan()
        self.assertEqual({(f['kind'], f['target_line']) for f in result['findings']},
                         {('missing_instructions', 3), ('missing_skill', 5), ('missing_mcp_command', 7)})
        self.assertTrue(all(f['target_path'] == str(self.home / 'config.toml') for f in result['findings']))

    def test_bad_toml_and_missing_parser_gap(self):
        self.write('config.toml', 'model = [\n')
        self.write('skills/sample/SKILL.md', '---\nname: sample\ndescription: Example\n---\n')
        if config_audit.tomllib:
            self.assertIn('toml_invalid', [f['kind'] for f in self.scan()['findings']])
        old = config_audit.tomllib
        try:
            config_audit.tomllib = None
            result = self.scan()
            self.assertEqual(result['scope']['coverage'], 'partial')
            self.assertEqual(result['files_checked'], 2)
            self.assertTrue(any('TOML' in e['message'] for e in result['errors']))
        finally:
            config_audit.tomllib = old

    def test_same_source_symlink_and_distinct_conflict(self):
        one = self.write('skills/alpha/SKILL.md', '---\nname: shared\ndescription: >\n  folded description\n---\n')
        second = self.base / 'second'
        (second / 'skills').mkdir(parents=True)
        (second / 'skills' / 'alias').symlink_to(one.parent)
        self.write('skills/beta/SKILL.md', '---\nname: shared\ndescription: "another value"\n---\n', second)
        result = self.scan([self.home, second])
        self.assertEqual([f['kind'] for f in result['findings']], ['skill_name_conflict'])
        self.assertEqual(result['files_checked'], 2)

    @unittest.skipIf(config_audit.tomllib is None, 'standard TOML parser unavailable')
    def test_disabled_skill_not_a_finding(self):
        self.write('config.toml', '[[skills.config]]\npath="skills/disabled"\nenabled=false\n')
        self.write('skills/disabled/SKILL.md', '---\nname: disabled\n')
        result = self.scan()
        self.assertEqual(result['findings'], [])

    @unittest.skipIf(config_audit.tomllib is None, 'standard TOML parser unavailable')
    def test_catalogue_max_not_default_and_secret_whitelist(self):
        self.write('config.toml', 'model="synthetic-model"\nmodel_context_window=900\n'
                   'model_reasoning_effort="high"\ntool_output_token_limit=200000\n'
                   '[mcp_servers.demo.env]\nTOKEN="SECRET_SENTINEL"\n')
        self.write('models_cache.json', json.dumps({'fetched_at': datetime.now(timezone.utc).isoformat(), 'models': [
            {'slug': 'synthetic-model', 'context_window': 100, 'max_context_window': 800,
             'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'medium'}]}]}))
        result = self.scan()
        self.assertEqual({f['kind'] for f in result['findings']},
                         {'context_exceeds_catalogue', 'effort_not_in_catalogue'})
        self.assertIn('large_output_limit', [o['kind'] for o in result['observations']])
        self.assertNotIn('SECRET_SENTINEL', json.dumps(result))
        self.assertEqual(result['findings'][0]['details']['catalogue_max'], 800)

    @unittest.skipIf(config_audit.tomllib is None, 'standard TOML parser unavailable')
    def test_stale_and_unknown_catalogue_times_are_observations(self):
        self.write('config.toml', 'model="sample"\nmodel_context_window=900\nmodel_reasoning_effort="high"\n')
        entry = {'slug': 'sample', 'max_context_window': 800, 'context_window': 100,
                 'supported_reasoning_levels': ['low']}
        cache = self.write('models_cache.json', json.dumps({'fetched_at': 1234567890, 'models': [entry]}))
        stale = self.scan()
        self.assertEqual(stale['findings'], [])
        self.assertEqual(stale['scope']['coverage'], 'partial')
        self.assertIn('检查覆盖不完整', stale['scope']['conclusion'])
        self.assertIn('context_exceeds_stale_catalogue', [o['kind'] for o in stale['observations']])
        cache.write_text(json.dumps({'fetched_at': datetime.now(timezone.utc).isoformat(),
                                     'expires_at': '2000-01-01T00:00:00Z', 'models': [entry]}))
        self.assertEqual(self.scan()['findings'], [])
        cache.write_text(json.dumps({'fetched_at': 'invalid', 'models': [entry]}))
        self.assertEqual(self.scan()['findings'], [])
        cache.write_text(json.dumps({'models': [entry]}))
        self.assertEqual(self.scan()['findings'], [])
        cache.write_text(json.dumps({'fetched_at': datetime.now(timezone.utc).isoformat(), 'models': [entry]}))
        old = time.time() - 3 * 86400
        os.utime(cache, (old, old))
        self.assertEqual(self.scan()['findings'], [])

    def test_concurrent_snapshot_runs_one_scan(self):
        self.write('skills/sample/SKILL.md', '---\nname: sample\ndescription: valid\n---\n')
        audit = config_audit.ConfigAudit([self.home])
        original = audit._scan
        started = threading.Barrier(8)
        count = [0]

        def counted(paths):
            count[0] += 1
            time.sleep(0.02)
            return original(paths)

        audit._scan = counted

        def snapshot(_):
            started.wait()
            return audit.snapshot()

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(snapshot, range(8)))
        self.assertEqual(count[0], 1)
        self.assertEqual(len({result['checked_at'] for result in results}), 1)
        results[0]['findings'].append('mutated')
        self.assertEqual(audit.snapshot()['findings'], [])

    @unittest.skipIf(config_audit.tomllib is None, 'standard TOML parser unavailable')
    def test_no_problem_and_mtime_cache(self):
        config = self.write('config.toml', 'model="synthetic-model"\n')
        audit = config_audit.ConfigAudit([self.home])
        initial = audit.snapshot()
        self.assertEqual(initial['findings'], [])
        self.assertIn('未发现', initial['scope']['conclusion'])
        self.assertEqual(initial['checked_at'], audit.snapshot()['checked_at'])
        config.write_text('model_instructions_file="absent"\n', encoding='utf-8')
        self.assertIn('missing_instructions', [f['kind'] for f in audit.snapshot()['findings']])


if __name__ == '__main__':
    unittest.main()
