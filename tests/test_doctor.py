import json
import tempfile
import unittest
import datetime as dt
from pathlib import Path

from session_doctor import parser, scanner, server, store


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)/'codex'
        self.logs=self.root/'sessions'
        self.logs.mkdir(parents=True)
        self.data=Path(self.tmp.name)/'data'
        self.sid='11111111-1111-1111-1111-111111111111'
        self.path=self.logs/('rollout-'+self.sid+'.jsonl')
        self.events=[]
        self.meta={'type':'session_meta','timestamp':'2026-09-23T00:00:00Z','payload':{'id':self.sid,'timestamp':'2026-09-23T00:00:00Z','cwd':'/tmp/project','source':'vscode'}}
        self.events.append(self.meta)

    def tearDown(self):
        self.tmp.cleanup()

    def event(self,typ,payload,second=1):
        self.events.append({'type':typ,'timestamp':f'2026-09-23T00:00:{second:02d}Z','payload':payload})

    def write(self,complete=True):
        blob='\n'.join(json.dumps(e) for e in self.events)
        self.path.write_text(blob+('\n' if complete else ''),encoding='utf-8')

    def scan(self):
        return scanner.scan([str(self.root)],str(self.data))

    def con(self):
        return store.connect(self.data)

    @staticmethod
    def u(input=100,cache=20,output=10,total=None):
        return {'input_tokens':input,'cached_input_tokens':cache,'output_tokens':output,'reasoning_output_tokens':3,'total_tokens':total if total is not None else input+output}

    def modern(self,rid,u=None,second=1,turn='turn1'):
        self.event('token_usage_record',{'thread_id':self.sid,'turn_id':turn,'response_id':rid,'usage':u or self.u()},second)

    def legacy(self,total,last,second=2):
        self.event('event_msg',{'type':'token_count','info':{'total_token_usage':total,'last_token_usage':last}},second)

    def test_modern_mirror_and_distinct_equal_responses(self):
        self.event('turn_context',{'turn_id':'turn1','model':'gpt-test'})
        self.modern('r1')
        self.legacy(self.u(),self.u(),2)
        self.modern('r2',second=3)
        self.legacy(self.u(200,40,20),self.u(),4)
        self.write();self.scan();self.scan()
        with self.con() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM usage').fetchone()[0],2)
            self.assertEqual(store.overview(con,0)['totals']['total'],220)
            self.assertEqual(store.overview(con,0)['totals']['cached'],40)
            self.assertEqual(store.overview(con,0)['totals']['requests'],2)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM warnings').fetchone()[0],0)

    def test_delayed_mirror_and_archive_copy(self):
        self.event('turn_context',{'turn_id':'turn1','model':'m'})
        self.modern('r1',second=1)
        self.legacy(self.u(),self.u(),20)
        self.write();self.scan()
        archived=self.root/'archived_sessions';archived.mkdir()
        target=archived/self.path.name
        target.write_bytes(self.path.read_bytes())
        self.scan()
        with self.con() as con:
            self.assertEqual(store.overview(con,0)['totals']['total'],110)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM warnings WHERE message LIKE '兼容%'").fetchone()[0],0)

    def test_snapshot_first_replaced_by_modern_and_compaction_pointer(self):
        self.event('turn_context',{'turn_id':'turn1','model':'m'})
        self.legacy(self.u(),self.u(),1)
        self.modern('r1',second=2)
        comp={'thread_id':self.sid,'turn_id':'turn1','response_id':'c1','usage':self.u(50,5,5)}
        self.event('token_usage_record',comp,3)
        self.event('compacted',{'compaction_response_id':'c1','latest_token_usage_record':comp,'replacement_history':[{'type':'token_usage_record','response_id':'should-ignore','usage':self.u(999)}]},4)
        self.write();self.scan()
        with self.con() as con:
            self.assertEqual(store.overview(con,0)['totals']['total'],165)
            self.assertEqual(con.execute("SELECT purpose FROM usage WHERE id=?",(scanner.key('response','c1'),)).fetchone()[0],'compaction')
            self.assertEqual(con.execute('SELECT COUNT(*) FROM usage').fetchone()[0],2)

    def test_legacy_gap_repeat_reset_and_fork(self):
        self.legacy(self.u(500,100,50),self.u(),1)
        self.legacy(self.u(500,100,50),self.u(),2)
        self.legacy(self.u(600,120,60),self.u(),3)
        self.legacy(self.u(10,0,1),self.u(10,0,1),4)
        self.write();self.scan()
        with self.con() as con:
            self.assertEqual(store.overview(con,0)['totals']['total'],671)
            self.assertEqual(store.overview(con,0)['totals']['requests'],3)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM warnings WHERE message LIKE '%重置%'").fetchone()[0],1)
        self.meta['payload']['forked_from_id']='parent'
        self.events=self.events[:1]
        self.legacy(self.u(128458250,1000,0,128458250),self.u(450076,20352,440,450516),1)
        self.write();self.scan()
        with self.con() as con:
            self.assertEqual(store.overview(con,0)['totals']['total'],671+450516)

    def test_incremental_half_bad_line_archive_restart(self):
        self.modern('r1')
        self.write(False)
        self.scan()
        with self.con() as con: self.assertEqual(store.overview(con,0)['totals']['total'],0)
        with self.path.open('a') as f: f.write('\n{bad json}\n')
        self.scan()
        archived=self.root/'archived_sessions';archived.mkdir()
        target=archived/self.path.name;self.path.rename(target)
        self.scan()
        with self.con() as con:
            self.assertEqual(store.overview(con,0)['totals']['total'],110)
            self.assertEqual(store.overview(con,0)['coverage']['parse_errors'],1)

    def test_source_output_status_and_safe_title(self):
        self.meta['payload']['source']={'subagent':{'thread_spawn':{'parent_thread_id':'parent'}}}
        self.meta['payload']['session_id']='parent'
        self.meta['payload']['thread_source']='subagent'
        self.event('response_item',{'type':'message','role':'user','content':[{'type':'input_text','text':'<environment_context>\ninternal context\n</environment_context>'},{'type':'input_text','text':'hello world'}]})
        self.event('response_item',{'type':'custom_tool_call','call_id':'call1','name':'exec','input':'{"cmd":"rg needle"}'},2)
        self.event('response_item',{'type':'custom_tool_call_output','call_id':'call1','output':{'content':[{'type':'image','data':'AAAA','mimeType':'image/png'},{'type':'text','text':'Process exited with code 1\nhello'}]}},3)
        self.write();self.scan()
        with self.con() as con:
            s=store.session_row(con,self.sid)
            self.assertEqual((s['source'],s['parent_id'],s['title']),('subagent','parent','hello world'))
            c=con.execute('SELECT * FROM calls').fetchone()
            self.assertEqual(c['status'],'failed')
            self.assertLess(c['output_chars'],100)

    def test_http_filters_and_empty_usage(self):
        self.event('event_msg',{'type':'token_count','info':None})
        self.write();self.scan()
        overview=server.api('/api/overview',{'days':['7']},str(self.data),[str(self.root)])
        self.assertEqual(overview['coverage']['missing_usage_sessions'],1)
        self.assertEqual(server.api('/api/sessions',{'q':['project']},str(self.data),[str(self.root)])['total'],1)
        self.assertIsNotNone(server.api('/api/session',{'id':[self.sid]},str(self.data),[])['session'])

    def test_local_day_cutoff_and_windowed_session_totals(self):
        local=dt.datetime.now().astimezone()
        midnight=local.replace(hour=0,minute=0,second=0,microsecond=0)
        expected=midnight.astimezone(dt.timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')
        self.assertEqual(store.cutoff(1),expected)
        self.assertEqual(store.cutoff(7),(midnight-dt.timedelta(days=6)).astimezone(dt.timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z'))
        con=self.con();con.execute('INSERT INTO sessions(id,title,created,last_active) VALUES(?,?,?,?)',(self.sid,'跨窗口','2026-01-01T00:00:00Z',local.astimezone(dt.timezone.utc).isoformat().replace('+00:00','Z')))
        old=(local-dt.timedelta(days=20)).astimezone(dt.timezone.utc).isoformat().replace('+00:00','Z')
        new=local.astimezone(dt.timezone.utc).isoformat().replace('+00:00','Z')
        for uid,stamp in (('old',old),('new',new)):
            scanner.put_usage(con,uid,self.sid,stamp,'turn','m',{'input':100,'cached':10,'output':10,'reasoning':0,'total':110},'modern','model','x',1)
        con.commit()
        self.assertEqual(store.sessions(con,7)['items'][0]['total'],110)
        self.assertEqual(store.detail(con,self.sid)['session']['total'],220)
        self.assertEqual(store.overview(con,7)['totals']['total'],110)
        con.close()

    def test_terminal_status_and_archive(self):
        self.event('event_msg',{'type':'task_started','turn_id':'t'},1)
        self.event('event_msg',{'type':'task_complete','turn_id':'t'},2)
        self.write();self.scan()
        with self.con() as con: self.assertEqual(store.session_row(con,self.sid)['status'],'completed')
        archived=self.root/'archived_sessions';archived.mkdir();self.path.rename(archived/self.path.name)
        self.scan()
        with self.con() as con: self.assertEqual(store.session_row(con,self.sid)['status'],'archived · completed')

    def test_model_switch_and_too_large_line(self):
        self.event('turn_context',{'turn_id':'one','model':'model-a'},1)
        self.modern('r1',second=2,turn='one')
        self.event('turn_context',{'turn_id':'two','model':'model-b'},3)
        self.modern('r2',second=4,turn='two')
        self.write()
        with self.path.open('a') as f:
            f.write('x'*(scanner.MAX_LINE+1)+'\n')
        self.scan()
        with self.con() as con:
            models={r['model'] for r in con.execute('SELECT model FROM usage')}
            self.assertEqual(models,{'model-a','model-b'})
            self.assertEqual(store.overview(con,0)['totals']['total'],220)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM warnings WHERE message LIKE '超大行%'").fetchone()[0],1)

    def test_parser_and_rules(self):
        self.assertEqual(parser.call_status('ordinary error text'),'unknown')
        self.assertEqual(parser.call_status({'output':'{"content":[{"exit_code":1}]}' }),'failed')
        self.assertEqual(parser.call_status({'name':'functions.exec','result':{'content':[{'status':'success','value':{'exit_code':0}},{'status':'failed','value':{'exit_code':1}}]}}),'failed')
        self.assertEqual(parser.command_from({'name':'functions.exec','input':'const r=await tools.exec_command({cmd:"rg needle",yield_time_ms:1000});'}),'rg needle')
        a='Script completed\nWall time: 1.0 seconds\nChunk ID: abc\nOutput:\nSAME BODY'
        b='Script completed\nWall time: 2.0 seconds\nChunk ID: xyz\nOutput:\nSAME BODY'
        self.assertEqual(parser.output_fingerprint(a),parser.output_fingerprint(b))
        self.assertEqual(parser.output_text({'result':{'content':[{'type':'image','data':'AAAA','mimeType':'image/png'},{'type':'text','text':'visible'}]}}),'visible')
        self.assertEqual(parser.safe('"api_key":"secret123" token="another" sk-abcdefghijklmnop'), '"api_key":"[REDACTED]" token="[REDACTED]" [REDACTED_SECRET]')
        self.assertIsNone(parser.title_from({'type':'message','role':'user','content':[{'type':'input_text','text':'<recommended_plugins>\nsecret instructions\n</recommended_plugins>'}]}))
        con=self.con();sid=self.sid
        con.execute('INSERT INTO sessions(id) VALUES(?)',(sid,))
        for i in range(4):
            scanner.put_usage(con,'u'+str(i),sid,f'2026-09-23T00:00:0{i}Z','t','m',{'input':160000,'cached':0,'output':2,'reasoning':0,'total':160002},'modern','model','x',i)
        for i in range(3):
            con.execute('INSERT INTO calls(id,session_id,timestamp,turn_id,name,command,output_chars,output_hash,estimated_tokens,status,path,line) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                        ('c'+str(i),sid,f'2026-09-23T00:00:1{i}Z','t','exec','rg foo',20000,'hash',5000,'failed','x',i))
        from session_doctor import diagnostics
        diagnostics.analyze(con,sid)
        kinds={r[0] for r in con.execute('SELECT kind FROM issues')}
        self.assertEqual(kinds,{'large_output','large_context','low_cache'})
        self.assertEqual(store.issues(con,0)['total'],0)
        con.execute("UPDATE calls SET status='unknown',output_hash='different'||id")
        con.execute("UPDATE usage SET cached=100000")
        diagnostics.analyze(con,sid)
        kinds={r[0] for r in con.execute('SELECT kind FROM issues')}
        self.assertNotIn('repeated_read',kinds);self.assertNotIn('repeated_failure',kinds);self.assertNotIn('low_cache',kinds)
        con.close()


if __name__=='__main__':
    unittest.main()
