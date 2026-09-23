import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from session_doctor import quota, server


class QuotaTest(unittest.TestCase):
    def test_weekly_window_and_mapping_priority(self):
        week={'windowDurationMins':10080,'usedPercent':22,'resetsAt':1790733726}
        five={'windowDurationMins':300,'usedPercent':99,'resetsAt':1}
        self.assertEqual(quota.weekly_limit({'rateLimitsByLimitId':{'codex':{'primary':five,'secondary':week}}})['remaining_percent'],78)
        self.assertEqual(quota.weekly_limit({'rateLimitsByLimitId':{'codex':{'primary':week}},'rateLimits':{'primary':five}})['used_percent'],22)
        self.assertIsNone(quota.weekly_limit({'rateLimitsByLimitId':{},'rateLimits':{'secondary':week}}))
        self.assertIsNone(quota.weekly_limit({'rateLimitsByLimitId':{'other':{'primary':week}},'rateLimits':{'primary':week}}))
        self.assertEqual(quota.weekly_limit({'rateLimitsByLimitId':None,'rateLimits':{'secondary':week}})['resets_at'],1790733726)
        self.assertIsNone(quota.weekly_limit({'rateLimits':{'primary':five}}))

    def test_unknown_and_limits(self):
        for used, remaining in ((None,None),(0,100),(100,0),(120,0)):
            item=quota.weekly_limit({'rateLimits':{'primary':{'windowDurationMins':10080,'usedPercent':used}}})
            self.assertEqual(item['remaining_percent'],remaining)
            self.assertIsNone(item['resets_at'])
        item=quota.weekly_limit({'rateLimits':{'primary':{'windowDurationMins':10080,'usedPercent':float('nan'),'resetsAt':float('inf')}}})
        self.assertIsNone(item['remaining_percent'])
        self.assertIsNone(item['resets_at'])

    def fake_cli(self, directory, body):
        path=Path(directory)/'codex'
        path.write_text('#!/usr/bin/env python3\n'+body,encoding='utf-8')
        path.chmod(0o755)
        return path

    def test_rpc_handshake_and_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.fake_cli(tmp, '''import json,os,sys
first=json.loads(sys.stdin.readline())
assert first['method']=='initialize'
assert first['params']['clientInfo']=={'name':'codex_session_doctor','title':'Codex Session Doctor','version':'0.1.0'}
print(json.dumps({'jsonrpc':'2.0','id':1,'result':{}}),flush=True)
second=json.loads(sys.stdin.readline())
third=json.loads(sys.stdin.readline())
assert second['method']=='initialized' and third['method']=='account/rateLimits/read'
assert os.environ['CODEX_HOME'].endswith('/chosen')
print(json.dumps({'jsonrpc':'2.0','method':'notice','params':{}}),flush=True)
print(json.dumps({'jsonrpc':'2.0','id':2,'result':{'rateLimitsByLimitId':{'codex':{'primary':{'windowDurationMins':10080,'usedPercent':25,'resetsAt':1234}}}}}),flush=True)
''')
            with patch.dict(os.environ,{'PATH':tmp+os.pathsep+os.environ['PATH']}):
                self.assertEqual(quota.read_weekly(Path(tmp)/'chosen')['remaining_percent'],75)

    def test_rpc_timeout_cleans_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid=Path(tmp)/'pid'
            self.fake_cli(tmp, f'''import os,time
open({str(pid)!r},'w').write(str(os.getpid()))
time.sleep(60)
''')
            with patch.dict(os.environ,{'PATH':tmp+os.pathsep+os.environ['PATH']}):
                with self.assertRaises(TimeoutError):
                    quota.read_weekly(tmp,timeout=1)
            process=int(pid.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(process,0)

    def test_cache_singleflight_and_stale(self):
        entered=threading.Event(); release=threading.Event(); calls=[]
        week={'used_percent':40,'remaining_percent':60,'resets_at':1,'window_minutes':10080}
        def reader(root):
            calls.append(root);entered.set();release.wait(2)
            return week
        cache=quota.QuotaCache('/first',reader)
        self.assertEqual(cache.snapshot()['error'],'读取中')
        thread=threading.Thread(target=cache.refresh);thread.start();self.assertTrue(entered.wait(2))
        self.assertFalse(cache.refresh())
        release.set();thread.join(2)
        self.assertEqual(calls,['/first'])
        self.assertEqual(server.api('/api/quota',{},'/nonexistent',[],cache)['weekly'],week)
        self.assertIsNotNone(cache.snapshot()['updated_at'])
        cache.reader=lambda _: (_ for _ in ()).throw(RuntimeError('额度读取失败'))
        cache.refresh()
        state=cache.snapshot()
        self.assertTrue(state['available']);self.assertTrue(state['stale'])
        self.assertEqual(state['weekly'],week)
        self.assertEqual(state['error'],'额度读取失败')


if __name__=='__main__':
    unittest.main()
