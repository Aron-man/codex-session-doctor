import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from session_doctor import scanner, store


class TraceDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)/'root'
        (self.root/'sessions').mkdir(parents=True)
        self.data = Path(self.tmp.name)/'data'
        self.sid = '22222222-2222-2222-2222-222222222222'
        self.path = self.root/'sessions'/('rollout-'+self.sid+'.jsonl')
        self.rows = [self._row('session_meta',{'id':self.sid,'cwd':'/tmp/project'}) , self._row('turn_context',{'turn_id':'t'})]

    def tearDown(self): self.tmp.cleanup()

    def _row(self, typ, payload):
        return {'type':typ,'timestamp':'2026-09-23T00:00:00Z','payload':payload}

    def item(self, ident, command=None, output='x'*2100, **kwargs):
        if command is None:
            value={'type':'McpToolCall','id':ident,'server':'example','tool':'read','arguments':{'path':'/tmp/a'},'result':output,'status':'success'}
        else:
            value={'type':'CommandExecution','id':ident,'command':[command],'cwd':'file:///tmp/project','stdout':output,'aggregated_output':output,'status':'completed','exit_code':0,'parsed_cmd':[{'type':'read','path':'doc.md'}]}
        value.update(kwargs)
        self.rows.append(self._row('event_msg',{'type':'item_completed','item':value}))

    def scan(self):
        self.path.write_text(''.join(json.dumps(x)+'\n' for x in self.rows),encoding='utf-8')
        return scanner.scan([str(self.root)],str(self.data))

    def test_native_subcalls_dedupe_and_usage_unchanged(self):
        self.rows.append(self._row('token_usage_record',{'thread_id':self.sid,'response_id':'r1','usage':{'input_tokens':10,'output_tokens':2,'total_tokens':12}}))
        for i in range(3): self.item('e'+str(i),'cat doc.md')
        self.item('m1')
        self.item('e0','cat doc.md')
        self.scan(); self.scan()
        with store.connect(self.data) as con:
            events=store.detail(con,self.sid)['tool_events']
            self.assertEqual(len(events),4)
            self.assertEqual({e['tool'] for e in events},{'exec_command','example.read'})
            self.assertEqual(events[-1]['target_paths'],['/tmp/project/doc.md'])
            self.assertEqual(store.issues(con,0)['items'][0]['kind'],'repeated_read')
            self.assertEqual(store.detail(con,self.sid)['session']['total'],12)
            self.assertEqual(store.overview(con,0)['coverage']['scanned_trace_files'],1)

    def test_compaction_and_discard_and_failure_recovery(self):
        for i in range(2): self.item('a'+str(i),'cat doc.md')
        self.rows.append(self._row('event_msg',{'type':'item_completed','item':{'type':'ContextCompaction','id':'c1'}}))
        self.item('a3','cat doc.md')
        self.item('s1','cat /tmp/SKILL.md > /dev/null',parsed_cmd=[{'type':'read','path':'/tmp/SKILL.md'}])
        self.item('s2','cat /tmp/SKILL.md 2>/dev/null',parsed_cmd=[{'type':'read','path':'/tmp/SKILL.md'}])
        self.item('s3','cat /tmp/SKILL.md',status='failed',exit_code=2,stderr='missing',parsed_cmd=[{'type':'read','path':'/tmp/SKILL.md'}])
        self.item('s4','cat /tmp/SKILL.md',parsed_cmd=[{'type':'read','path':'/tmp/SKILL.md'}])
        self.scan()
        with store.connect(self.data) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM issues WHERE kind='repeated_read'").fetchone()[0],0)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM issues WHERE kind='skill_read_discarded'").fetchone()[0],1)
            self.assertEqual(con.execute("SELECT actionable FROM issues WHERE kind='skill_read_failed'").fetchone()[0],0)

    def test_truncation_and_failed_streak(self):
        self.item('long','cat doc.md','z'*21000)
        self.item('trunc','cat doc.md','Warning: truncated output\n')
        self.item('trunc2','cat doc.md','Warning: truncated output\n')
        for i in range(3): self.item('rg'+str(i),'rg missing',status='failed',exit_code=1,stderr='')
        for i in range(3): self.item('bad'+str(i),'cat missing',status='failed',exit_code=2,stderr='No file')
        self.scan()
        with store.connect(self.data) as con:
            kinds={x['kind'] for x in store.issues(con,0)['items']}
            self.assertIn('tool_output_truncated',kinds)
            self.assertIn('repeated_failure',kinds)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM issues WHERE kind='large_output' AND actionable=0").fetchone()[0],1)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM issues WHERE kind='tool_output_truncated'").fetchone()[0],1)

    def test_old_schema_migrates_without_usage_change(self):
        self.data.mkdir()
        raw=sqlite3.connect(self.data/'doctor.sqlite3')
        raw.execute('CREATE TABLE issues(id TEXT PRIMARY KEY,session_id TEXT,kind TEXT,severity TEXT,title TEXT,evidence TEXT,suggestion TEXT,timestamp TEXT,turn_id TEXT,path TEXT,line INTEGER,estimated_tokens INTEGER DEFAULT 0)')
        raw.execute("INSERT INTO issues(id,session_id,kind,timestamp) VALUES('old','session','large_output','2026-09-23T00:00:00Z')")
        raw.commit();raw.close()
        with store.connect(self.data) as con:
            self.assertIn('actionable',{r['name'] for r in con.execute('PRAGMA table_info(issues)')})
            self.assertEqual(con.execute('SELECT COUNT(*) FROM usage').fetchone()[0],0)
            self.assertEqual(store.issues(con,0)['total'],0)
            self.assertEqual(store.issues(con,0,view='observations')['total'],1)

    def test_partial_line_then_archive_preserves_unique_events(self):
        self.item('e1','cat doc.md')
        full=''.join(json.dumps(x)+'\n' for x in self.rows)
        self.path.write_text(full[:-5],encoding='utf-8')
        scanner.scan([str(self.root)],str(self.data))
        with store.connect(self.data) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM tool_events').fetchone()[0],0)
        self.path.write_text(full,encoding='utf-8')
        scanner.scan([str(self.root)],str(self.data))
        archive=self.root/'archived_sessions'
        archive.mkdir()
        self.path.rename(archive/self.path.name)
        scanner.scan([str(self.root)],str(self.data))
        with store.connect(self.data) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM tool_events').fetchone()[0],1)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM trace_files WHERE status='archived'").fetchone()[0],1)

    def test_thread_ownership_creation_and_top_level_compaction(self):
        self.rows[0]['payload']['timestamp']='2026-09-23T00:00:00Z'
        inherited=self._row('event_msg',{'type':'item_completed','thread_id':'parent','item':{'type':'CommandExecution','id':'old','command':['cat','doc.md'],'stdout':'x','status':'completed'}})
        self.rows.append(inherited)
        before=self._row('event_msg',{'type':'item_completed','item':{'type':'CommandExecution','id':'before','command':['cat','doc.md'],'stdout':'x','status':'completed'}})
        before['timestamp']='2026-09-22T00:00:00Z'
        self.rows.append(before)
        self.rows.append(self._row('compacted',{}))
        self.item('own','cat doc.md')
        self.scan()
        with store.connect(self.data) as con:
            events=store.detail(con,self.sid)['tool_events']
            self.assertEqual(len(events),1)
            self.assertEqual(events[0]['context_epoch'],1)

    def test_same_id_final_status_and_visible_output(self):
        self.item('same','cat doc.md',output='',status='running')
        self.rows[-1]['timestamp']='2026-09-23T00:00:01Z'
        self.item('same','cat doc.md',output='visible',status='completed',formatted_output='visible',aggregated_output='Warning: truncated output\n',stdout='hidden full output')
        self.rows[-1]['timestamp']='2026-09-23T00:00:02Z'
        self.item('mcp',output={'content':[{'type':'image','data':'SECRETBASE64','mimeType':'image/png'},{'type':'text','text':'MCP visible'}]},status='success')
        self.scan()
        with store.connect(self.data) as con:
            events={x['id']:x for x in store.detail(con,self.sid)['tool_events']}
            self.assertEqual(len(events),2)
            shell=next(x for x in events.values() if x['tool']=='exec_command')
            mcp=next(x for x in events.values() if x['tool']=='example.read')
            self.assertEqual((shell['status'],shell['output_chars'],shell['truncated']),('completed',7,0))
            self.assertEqual(mcp['output_chars'],len('MCP visible'))

    def test_trace_bad_and_oversize_lines_are_counted(self):
        self.path.write_bytes((json.dumps(self.rows[0])+'\n').encode()+b'{bad}\n'+b'x'*(32*1024*1024+1)+b'\n')
        scanner.scan([str(self.root)],str(self.data))
        with store.connect(self.data) as con:
            coverage=store.overview(con,0)['coverage']
            self.assertEqual(coverage['trace_bad_lines'],1)
            self.assertEqual(coverage['trace_oversize_lines'],1)

    def test_two_files_one_session_analyzed_once(self):
        self.item('one','cat doc.md')
        self.path.write_text(''.join(json.dumps(x)+'\n' for x in self.rows),encoding='utf-8')
        second=self.path.with_name('other-'+self.sid+'.jsonl')
        second.write_text(''.join(json.dumps(x)+'\n' for x in self.rows),encoding='utf-8')
        from session_doctor import diagnostics
        original=diagnostics.analyze
        seen=[]
        def counting(con,sid):
            seen.append(sid)
            return original(con,sid)
        with patch.object(diagnostics,'analyze',counting):
            scanner.scan([str(self.root)],str(self.data))
        self.assertEqual(seen,[self.sid])

    def test_unrelated_compound_failure_does_not_blame_skill(self):
        self.item('mixed','cat /tmp/SKILL.md; false',output='skill content\nother command failed',
                  status='failed',exit_code=1,parsed_cmd=[{'type':'read','cmd':'cat /tmp/SKILL.md','path':'/tmp/SKILL.md'},{'type':'unknown','cmd':'false'}])
        self.scan()
        with store.connect(self.data) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM issues WHERE kind='skill_read_failed'").fetchone()[0],0)

    def test_merged_output_proves_missing_skill_without_stderr(self):
        self.item('missing','cat /tmp/SKILL.md; echo done',output='cat: /tmp/SKILL.md: No such file or directory\ndone',
                  parsed_cmd=[{'type':'read','cmd':'cat /tmp/SKILL.md','path':'/tmp/SKILL.md'},{'type':'unknown','cmd':'echo done'}])
        self.scan()
        with store.connect(self.data) as con:
            event=con.execute("SELECT error_excerpt,skill_evidence FROM tool_events WHERE tool='exec_command'").fetchone()
            self.assertIn('No such file',event['error_excerpt'])
            self.assertIn('/tmp/SKILL.md',event['error_excerpt'])
            issue=con.execute("SELECT actionable,evidence FROM issues WHERE kind='skill_read_failed'").fetchone()
            self.assertEqual(issue['actionable'],1)
            self.assertIn('/tmp/SKILL.md',issue['evidence'])

    def test_long_compound_discard_uses_full_command(self):
        prefix='echo '+('a'*300)
        self.item('discard',prefix+'; cat /tmp/SKILL.md > /dev/null',
                  parsed_cmd=[{'type':'unknown','cmd':prefix},{'type':'read','cmd':'cat /tmp/SKILL.md','path':'/tmp/SKILL.md'}])
        self.item('other','cat /tmp/SKILL.md; echo x > /dev/null',
                  parsed_cmd=[{'type':'read','cmd':'cat /tmp/SKILL.md','path':'/tmp/SKILL.md'},{'type':'unknown','cmd':'echo x > /dev/null'}])
        self.scan()
        with store.connect(self.data) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM issues WHERE kind='skill_read_discarded'").fetchone()[0],1)
            row=con.execute("SELECT command,skill_evidence FROM tool_events WHERE status='completed' ORDER BY line LIMIT 1").fetchone()
            self.assertNotIn('SKILL.md',row['command'])
            self.assertTrue(json.loads(row['skill_evidence'])['/tmp/SKILL.md']['discarded'])

    def test_stderr_redirect_is_not_stdout_discard(self):
        self.item('stderr','cat /tmp/SKILL.md 2>/dev/null',
                  parsed_cmd=[{'type':'read','cmd':'cat /tmp/SKILL.md','path':'/tmp/SKILL.md'}])
        self.scan()
        with store.connect(self.data) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM issues WHERE kind='skill_read_discarded'").fetchone()[0],0)

    def test_unknown_parsed_compound_reads_only_real_paths(self):
        command=("sed -n '1,420p' '/tmp/skills/dws/SKILL.md' >/dev/null && "
                 "sed -n '1,360p' '/tmp/skills/dws/references/mail.md' >/dev/null && "
                 "sed -n '1,160p' '/tmp/skills/dws/references/09-mail.md' >/dev/null; echo ok")
        self.item('compound',command,parsed_cmd=[{'type':'unknown','cmd':command}])
        self.scan()
        with store.connect(self.data) as con:
            event=store.detail(con,self.sid)['tool_events'][0]
            self.assertEqual(event['skill_paths'],['/tmp/skills/dws/SKILL.md'])
            self.assertEqual(event['target_paths'],[
                '/tmp/skills/dws/SKILL.md',
                '/tmp/skills/dws/references/mail.md',
                '/tmp/skills/dws/references/09-mail.md'])
            self.assertEqual(con.execute("SELECT COUNT(*) FROM issues WHERE kind='skill_read_discarded'").fetchone()[0],1)

    def test_echo_and_test_only_skill_mention_are_not_reads(self):
        command='echo /tmp/SKILL.md >/dev/null && test -r /tmp/SKILL.md; echo ok'
        self.item('mention',command,parsed_cmd=[{'type':'unknown','cmd':command}])
        self.scan()
        with store.connect(self.data) as con:
            event=store.detail(con,self.sid)['tool_events'][0]
            self.assertEqual(event['skill_paths'],[])
            self.assertEqual(event['target_paths'],[])
            self.assertEqual(con.execute("SELECT COUNT(*) FROM issues WHERE kind='skill_read_discarded'").fetchone()[0],0)

    def test_existing_tool_events_schema_adds_skill_evidence(self):
        self.data.mkdir()
        raw=sqlite3.connect(self.data/'doctor.sqlite3')
        raw.execute('CREATE TABLE tool_events(id TEXT PRIMARY KEY,session_id TEXT,timestamp TEXT)')
        raw.commit();raw.close()
        with store.connect(self.data) as con:
            self.assertIn('skill_evidence',{r['name'] for r in con.execute('PRAGMA table_info(tool_events)')})


if __name__=='__main__': unittest.main()
