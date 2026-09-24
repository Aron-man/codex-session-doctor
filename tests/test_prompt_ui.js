const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const nodes = new Map();
function node() {
  return {
    listeners: {}, classList: {toggle() {}}, style: {}, value: '', open: false, children: [],
    addEventListener(type, fn) { this.listeners[type] = fn; },
    setAttribute(key, value) { this[key] = value; },
    append(...items) { this.children.push(...items); }, replaceChildren() { this.children = []; }, focus() { this.focused = true; },
    select() { this.selected = true; },
    showModal() { this.open = true; }, close() { this.open = false; },
  };
}
const document = {
  listeners: {},
  getElementById(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); },
  createElement: node,
  createTextNode(value) { return {textContent: value}; },
  addEventListener(type, fn) { this.listeners[type] = fn; },
};
const context = vm.createContext({document, window: {}, navigator: {clipboard: {
  async writeText(value) { context.copied = value; },
}}, Intl, Date, Number, Math, String, Boolean, Promise, setTimeout() {}, clearTimeout() {}});
const source = fs.readFileSync('web/app.js', 'utf8').replace(/refresh\(\);setInterval\(refresh,10000\);\s*refreshQuota\(\);setInterval\(refreshQuota,60000\);setInterval\(renderQuota,1000\);\s*$/, '');
vm.runInContext(source, context);

(async () => {
  const card = node();
  context.card = card;
  vm.runInContext("renderQuotaEstimate(card, {state:'out_of_window', percent:0, reason:'本周期无已记录用量'})", context);
  assert.equal(card.children[0].children[1].textContent, '本周期无记录');
  const unknown = node();
  context.card = unknown;
  vm.runInContext("renderQuotaEstimate(card, {state:'unobserved', percent:0})", context);
  assert.equal(unknown.children[0].children[1].textContent, '未观察到用量');
  assert.equal(vm.runInContext("quotaEstimateValue({state:'estimated',percent:2})", context), '≈2.00%');
  nodes.get('search').value = 'child';
  context.fetch = async url => {
    assert.match(url, /grouped=1/);
    return {ok:true,json:async () => ({total:1,items:[{
      id:'parent',title:'parent',source:'cli',cwd:'/tmp',total:0,descendant_count:2,
      group_usage:{input:100,cached:0,output:0,total:100,requests:1,issue_count:0},
      weekly_quota_estimate:{state:'unobserved'},group_weekly_quota_estimate:{state:'estimated',percent:4},
      search_path:true,children:[{id:'child',title:'child',source:'subagent',cwd:'/tmp',total:100,descendant_count:1,
      group_usage:{input:100,cached:0,output:0,total:100,requests:1,issue_count:0},
      weekly_quota_estimate:{state:'estimated',percent:4},group_weekly_quota_estimate:{state:'estimated',percent:4},
      search_path:true,children:[{id:'grandchild',title:'grandchild',source:'subagent',cwd:'/tmp',total:100,
      group_usage:{input:100,cached:0,output:0,total:100,requests:1,issue_count:0},
      weekly_quota_estimate:{state:'estimated',percent:4},group_weekly_quota_estimate:{state:'estimated',percent:4},
      search_path:true,children:[]}]}]}]})};
  };
  await vm.runInContext('renderSessions()', context);
  const rows = nodes.get('sessions').children;
  assert.equal(rows.length, 3);
  assert.equal(rows[0].children[5].textContent, '100');
  assert.equal(rows[0].children[8].textContent, '≈4.00%');
  assert.equal(rows[1].hidden, false);
  assert.equal(rows[2].hidden, false);
  nodes.get('search').value = '';
  await vm.runInContext('renderSessions()', context);
  let tree = nodes.get('sessions').children;
  assert.equal(tree[1].hidden, true, 'automatic search expansion must not become manual state');
  tree[0]._toggle.listeners.click({stopPropagation() {}});
  assert.equal(tree[1].hidden, false);
  assert.equal(tree[2].hidden, true);
  tree[1]._toggle.listeners.click({stopPropagation() {}});
  assert.equal(tree[2].hidden, false);
  tree[0]._toggle.listeners.click({stopPropagation() {}});
  assert.equal(tree[1].hidden, true);
  assert.equal(tree[2].hidden, true);
  tree[0]._toggle.listeners.click({stopPropagation() {}});
  assert.equal(tree[1].hidden, false);
  assert.equal(tree[1]._toggle.textContent, '▾');
  assert.equal(tree[2].hidden, false);
  await vm.runInContext('renderSessions()', context);
  tree = nodes.get('sessions').children;
  assert.equal(tree[0]._toggle.textContent, '▾');
  assert.equal(tree[1]._toggle.textContent, '▾');
  assert.equal(tree[2].hidden, false, 'manual expansion survives refresh');
  const detail = {
    session:{id:'leaf',title:'leaf',source:'cli',total:100,input:100,cached:0,output:0,reasoning:0,requests:1,tool_calls:0},
    descendant_count:0,weekly_quota_estimate:{state:'estimated',percent:4},
    group_weekly_quota_estimate:{state:'estimated',percent:4},
    issues:[],observations:[],turns:[],usage:[],calls:[],tool_events:[],children:[],warnings:[],
    truncated:{usage:false,calls:false,tool_events:false},
  };
  context.fetch = async () => ({ok:true,json:async () => detail});
  await vm.runInContext("openSession('leaf')", context);
  let detailCard = nodes.get('detailBody').children[0];
  assert.equal(detailCard.children.filter(child => child.className === 'estimate').length, 1);
  detail.descendant_count = 2;
  await vm.runInContext("openSession('leaf')", context);
  detailCard = nodes.get('detailBody').children[0];
  const estimates = detailCard.children.filter(child => child.className === 'estimate');
  assert.equal(estimates.length, 2);
  assert.match(estimates[1].children[0].textContent, /含 2 个子任务，估算/);
  const issue = vm.runInContext("issueItem({id:'i',title:'具体问题',tool:'exec_command',command:'cat a',target_path:'/tmp/a',target_line:3,evidence:'重复读取',timestamp:'2026-09-20'})", context);
  assert.match(issue.children[1].textContent, /exec_command.*cat a.*\/tmp\/a:3/);
  const observation = vm.runInContext("issueItem({id:'o',title:'大上下文',evidence:'观察'}, true)", context);
  assert.equal(observation.children.some(x => x.className === 'prompt-button'), false);
  context.fetch = async () => ({ok: true, json: async () => ({checked_at:'2026-09-23T00:00:00Z',scope:['temporary'],files_checked:0,findings:[],observations:[],errors:['config unreadable']})});
  await vm.runInContext('renderConfigAudit()', context);
  assert.match(nodes.get('configFindings').children[0].textContent, /检查存在缺口/);
  assert.match(nodes.get('configScope').textContent, /config unreadable/);
  context.fetch = async () => ({ok: true, json: async () => ({prompt: '可编辑内容'})});
  const button = vm.runInContext("promptButton('issue-1')", context);
  assert.equal(button.textContent, '生成核对 Prompt');
  assert.equal(vm.runInContext("promptButton('config:one')", context).textContent, '生成修复 Prompt');
  let stopped = false;
  button.listeners.click({stopPropagation() { stopped = true; }});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(stopped, true);
  assert.equal(nodes.get('promptText').value, '可编辑内容');
  assert.equal(nodes.get('promptDialog').open, true);

  nodes.get('promptText').value = '编辑后的文本';
  await vm.runInContext('copyIssuePrompt()', context);
  assert.equal(context.copied, '编辑后的文本');
  assert.equal(nodes.get('promptStatus').textContent, '已复制');

  context.navigator.clipboard.writeText = async () => { throw new Error('denied'); };
  await vm.runInContext('copyIssuePrompt()', context);
  assert.equal(nodes.get('promptText').selected, true);
  assert.match(nodes.get('promptStatus').textContent, /手动复制/);

  const requests = [];
  let resolvePreview;
  context.fetch = async (url, options) => {
    requests.push([url, options]);
    if (url.startsWith('/api/session?')) return {ok:true,json:async()=>detail};
    if (url.startsWith('/api/deep-analysis/latest?')) return {ok:true,json:async()=>({job:null})};
    if (url.startsWith('/api/deep-analysis/preview?')) return new Promise(resolve=>{resolvePreview=resolve;});
    if (url === '/api/deep-analysis') return {ok:true,json:async()=>({id:'job-1',session_id:'leaf',status:'running',usage:null,error:null})};
    if (url.startsWith('/api/deep-analysis?id=')) return {ok:true,json:async()=>({id:'job-1',session_id:'leaf',status:'completed',created_at:'now',finished_at:'now',model:'gpt-6-sol',effort:'medium',error:null,reused:false,usage:{input_tokens:10,cached_input_tokens:2,output_tokens:3},evidence:{events:[{id:'E1',text:'证据文本',source:{path:'/tmp/log',line:4}}]},guidance:[{id:'G1',title:'公开依据',url:'https://example.org/principle'}],result:{summary:'存在约束丢失',findings:[{title:'问题',status:'open',severity:'medium',confidence:'high',category:'goal_drift',goal:'目标',problem:'问题',evidence_refs:['E1'],counterevidence_refs:[],recommendation:{action:'建议',reason:'原因',alternative:'备选',tradeoff:'取舍',validation:'验证',principle_refs:['G1']},limitations:[]}],uncertainties:[]}})};
    if (url === '/api/deep-analysis/cancel') return {ok:true,json:async()=>({id:'job-1',session_id:'leaf',status:'cancelled',usage:null,error:null})};
    if (url.startsWith('/api/deep-analysis/prompt?')) return {ok:true,json:async()=>({title:'修复 Prompt',prompt:'按证据修复'})};
    throw new Error('unexpected '+url);
  };
  await vm.runInContext("openSession('leaf')", context);
  assert.equal(requests.filter(x=>x[1] && x[1].method === 'POST').length, 0, 'opening a session never runs the model');
  let analysis = nodes.get('detailBody').children.find(x=>x.className.includes('analysis-card'));
  const focus = analysis.children.find(x=>x.className === 'analysis-focus');
  focus.value = '约束丢失';
  const prepare = analysis.children.find(x=>x.textContent === '准备诊断材料');
  const pending = prepare.listeners.click();
  await new Promise(resolve=>setImmediate(resolve));
  resolvePreview({ok:true,json:async()=>({preview_id:'p1',prompt:'分析内容',model:'gpt-6-sol',effort:'medium',estimate:{approx_input_tokens:100,evidence_chars:300},evidence:{coverage:{selected_events:1,omitted_events:0,partial:false,notes:[]},events:[{id:'E1',text:'证据文本',source:{path:'/tmp/log',line:4}}]}})});
  await pending;
  let preview = analysis.children.find(x=>x.className === 'analysis-preview');
  const copy = preview.children.find(x=>x.textContent === '预览/复制分析 Prompt');
  copy.listeners.click();
  assert.equal(nodes.get('promptText').value, '分析内容');
  nodes.get('promptDialog').close();
  const start = preview.children.find(x=>x.textContent && x.textContent.includes('开始 Codex 分析'));
  await start.listeners.click();
  assert.equal(requests.filter(x=>x[1] && x[1].method === 'POST').length, 1);
  assert.equal(JSON.parse(requests.find(x=>x[1] && x[1].method === 'POST')[1].body).preview_id, 'p1');
  let result = analysis.children.find(x=>x.className === 'analysis-result');
  assert.match(result.children[0].textContent, /分析中/);
  await result.children.find(x=>x.textContent === '取消诊断').listeners.click();
  assert.match(result.children[0].textContent, /已取消/);
  assert.equal(requests.filter(x=>x[1] && x[1].method === 'POST').length, 2);
  vm.runInContext("renderAnalysisJob(document.getElementById('detailBody').children.find(x=>x.className.includes('analysis-card')).children.find(x=>x.className==='analysis-result'),{id:'job-1',session_id:'leaf',status:'running',usage:null},'leaf',detailRequest)", context);
  await vm.runInContext("pollAnalysis(document.getElementById('detailBody').children.find(x=>x.className.includes('analysis-card')).children.find(x=>x.className==='analysis-result'),'job-1','leaf',detailRequest)", context);
  assert.match(result.children[0].textContent, /已完成/);
  assert.match(result.children.find(x=>x.className === 'analysis-finding').children.find(x=>x.className === 'analysis-ref').textContent, /\/tmp\/log:4/);
  const repair = result.children.find(x=>x.className === 'analysis-finding').children.find(x=>x.textContent === '复制修复 Prompt');
  await repair.listeners.click();
  assert.equal(nodes.get('promptText').value, '按证据修复');
  nodes.get('promptDialog').close();

  const failedJob = {id:'job-2',session_id:'leaf',status:'failed',created_at:'now',finished_at:'now',model:'gpt-6-sol',effort:'medium',usage:{input_tokens:8,cached_input_tokens:0,output_tokens:1},result:null,error:'runner failed',evidence:{events:[]},guidance:[],reused:false};
  context.fetch = async () => ({ok:true,json:async()=>failedJob});
  assert.equal((await vm.runInContext("get('/api/deep-analysis?id=job-2')", context)).status, 'failed');
  assert.equal((await vm.runInContext("post('/api/deep-analysis',{preview_id:'failed'})", context)).status, 'failed');
  await vm.runInContext("pollAnalysis(document.getElementById('detailBody').children.find(x=>x.className.includes('analysis-card')).children.find(x=>x.className==='analysis-result'),'job-2','leaf',detailRequest)", context);
  assert.match(result.children[0].textContent, /失败/);
  assert.match(result.children.find(x=>x.className === 'analysis-error').textContent, /runner failed/);
  assert.match(result.children.find(x=>x.className === 'muted').textContent, /输入 8/);
  context.fetch = async () => ({ok:false,json:async()=>({error:'bad request'})});
  await assert.rejects(vm.runInContext("get('/api/deep-analysis?id=missing')", context), /bad request/);

  const late = [];
  context.fetch = async url => {
    if (url.startsWith('/api/session?')) return {ok:true,json:async()=>detail};
    if (url.startsWith('/api/deep-analysis/latest?')) return new Promise(resolve=>{late.push(resolve);});
    throw new Error('unexpected '+url);
  };
  await vm.runInContext("openSession('leaf')", context);
  await vm.runInContext("openSession('other')", context);
  late[0]({ok:true,json:async()=>({job:{id:'old',status:'completed',result:{summary:'wrong session',findings:[]}}})});
  await new Promise(resolve=>setImmediate(resolve));
  analysis = nodes.get('detailBody').children.find(x=>x.className.includes('analysis-card'));
  assert.equal(analysis.children.find(x=>x.className === 'analysis-result').children.length, 0, 'late result must not appear in another session');

  document.getElementById('drawer').hidden = false;
  nodes.get('promptDialog').showModal();
  document.listeners.keydown({key: 'Escape'});
  assert.equal(nodes.get('drawer').hidden, false);
  nodes.get('promptDialog').close();
  document.listeners.keydown({key: 'Escape'});
  assert.equal(nodes.get('drawer').hidden, true);
})().catch(error => { console.error(error); process.exitCode = 1; });
