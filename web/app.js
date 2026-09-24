const $ = id => document.getElementById(id);
const nf = new Intl.NumberFormat('zh-CN');
const fmt = n => n == null ? '—' : nf.format(n);
const short = s => (s || '').length > 70 ? s.slice(0, 70) + '…' : (s || '—');
let days = 7, tab = 'models', searchTimer, issueOffset = 0, observationOffset = 0, sessionOffset = 0;
let quotaSnapshot = null;
let promptRequest = 0;
let detailRequest = 0, activeSession = null, analysisPreview = null, analysisJob = null, analysisTimer = null;
const expandedSessions = new Map();

function quotaTime(seconds) {
  return seconds == null ? '—' : new Date(seconds * 1000).toLocaleString('zh-CN');
}
function renderQuota() {
  const q = quotaSnapshot, week = q && q.weekly;
  $('quotaRemaining').textContent = week && week.remaining_percent != null ? `${week.remaining_percent}%` : '—';
  $('quotaUsed').textContent = week && week.used_percent != null ? `${week.used_percent}%` : '—';
  $('quotaReset').textContent = week ? quotaTime(week.resets_at) : '—';
  const seconds = week && week.resets_at != null ? Math.ceil(week.resets_at - Date.now()/1000) : null;
  $('quotaCountdown').textContent = seconds == null ? '—' : seconds <= 0 ? '等待官方额度刷新' : `${Math.floor(seconds/86400)} 天 ${Math.floor(seconds%86400/3600)} 小时 ${Math.floor(seconds%3600/60)} 分`;
  $('quotaUpdated').textContent = q && q.updated_at ? `额度更新于 ${new Date(q.updated_at).toLocaleString('zh-CN')}` : '尚未取得官方额度';
  const status = $('quotaStatus');
  status.textContent = !q ? '读取失败' : q.stale ? `数据已陈旧：${q.error || '读取失败'}` : q.error || (q.available ? '官方额度' : '读取中');
  status.classList.toggle('stale', Boolean(q && (q.stale || (q.error && !q.available))));
}
async function refreshQuota() {
  try {
    const response = await fetch('/api/quota');
    if (!response.ok) throw new Error('额度接口不可用');
    quotaSnapshot = await response.json();
  } catch (e) {
    quotaSnapshot = quotaSnapshot ? {...quotaSnapshot, stale: true, error: e.message} : {error: e.message};
  }
  renderQuota();
}

function el(tag, cls, value) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (value != null) node.textContent = String(value);
  return node;
}
function clear(node) { node.replaceChildren(); }
async function get(url) {
  const response = await fetch(url);
  const data = await response.json();
  if (!response.ok || apiError(data)) throw new Error(data.error || response.statusText);
  return data;
}
function apiError(data) { return data && typeof data==='object' && Object.keys(data).length===1 && Object.prototype.hasOwnProperty.call(data,'error'); }
async function post(url, payload) {
  const response = await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const data = await response.json();
  if (!response.ok || apiError(data)) throw new Error(data.error || response.statusText);
  return data;
}
function line(parent, cls, parts) {
  const row = el('div', cls);
  for (const p of parts) row.append(el(p[0], p[1], p[2]));
  parent.append(row);
  return row;
}
function renderOverview(data) {
  const labels = [['total','总 token','输入 + 输出'],['input','输入 token','包含缓存输入'],['cached','缓存输入','输入的一部分'],['output','输出 token','包含推理输出'],['reasoning','推理输出','输出的一部分'],['requests','模型请求','会话 ' + fmt(data.totals.sessions)]];
  const box = $('metrics'); clear(box);
  for (const [key,label,hint] of labels) {
    const card = el('div','metric'); line(card,'label', [['span','',label]]); line(card,'value',[['span','',fmt(data.totals[key])]]); line(card,'hint',[['span','',hint]]); box.append(card);
  }
  const c=data.coverage;
  $('scanStatus').textContent=c.scanning ? `扫描中 · ${fmt(c.scanned_files)} / ${fmt(c.files)} 文件` : `上次扫描 ${c.last_scan ? new Date(c.last_scan).toLocaleString('zh-CN') : '尚未完成'}`;
  $('scanStatus').classList.toggle('scanning',c.scanning);
  const panel=$('coverage'); clear(panel);
  [['文件',`${fmt(c.scanned_files)} / ${fmt(c.files)}`],['结构化轨迹',c.trace_files == null ? '尚无覆盖记录' : `${fmt(c.scanned_trace_files)} / ${fmt(c.trace_files)} 文件`],['字节',`${fmt(c.scanned_bytes)} / ${fmt(c.bytes)}`],['解析错误',fmt(c.parse_errors)],['缺用量会话',fmt(c.missing_usage_sessions)],['覆盖告警',fmt(c.warning_count)]].forEach(([a,b])=>line(panel,'coverage-line',[['span','',a],['b','',b]]));
  const note=el('div','coverage-note'); note.textContent=(c.error ? `扫描错误：${c.error}。` : '') + (c.trace_indexing ? '结构化轨迹索引中。' : '') + (c.limitations || []).join('；'); panel.append(note);
  const trend=$('trend');clear(trend);const daily=data.daily.slice(-30),max=Math.max(1,...daily.map(x=>x.total));
  if(!daily.length)trend.append(el('div','empty','当前范围暂无每日用量'));
  for(const x of daily){const col=el('div','barcol'),bar=el('div','bar');bar.style.height=Math.max(2,Math.round(80*x.total/max))+'px';bar.title=`${x.day} · ${fmt(x.total)} token`;col.append(bar,el('span','',x.day.slice(5)));trend.append(col);}
  renderBreakdown(data);
}
function pager(node,total,offset,limit,onPage){clear(node);node.append(el('span','',`${fmt(total?offset+1:0)}–${fmt(Math.min(total,offset+limit))} / ${fmt(total)}`));const prev=el('button','','上一页'),next=el('button','','下一页');prev.disabled=offset===0;next.disabled=offset+limit>=total;prev.addEventListener('click',()=>onPage(Math.max(0,offset-limit)));next.addEventListener('click',()=>onPage(offset+limit));node.append(prev,next);}
async function showIssuePrompt(id) {
  const dialog=$('promptDialog'), status=$('promptStatus'), field=$('promptText');
  const request=++promptRequest;
  status.textContent='正在生成…'; field.value=''; dialog.showModal();
  try {
    const result=await get('/api/issue-prompt?id='+encodeURIComponent(id));
    if (!dialog.open || request!==promptRequest) return;
    field.value=result.prompt; status.textContent='可编辑并复制，发送给你自己的 Codex。';
  } catch (e) { if (dialog.open && request===promptRequest) status.textContent='生成失败：'+e.message; }
}
function promptButton(id, observation=false) {
  const button=el('button','prompt-button',observation ? '生成分析 Prompt' : id.startsWith('config:') ? '生成修复 Prompt' : '生成核对 Prompt'); button.type='button';
  button.addEventListener('click',event=>{event.stopPropagation();showIssuePrompt(id);});
  return button;
}
async function copyIssuePrompt() {
  const field=$('promptText'), status=$('promptStatus');
  try {
    if (!navigator.clipboard || !navigator.clipboard.writeText) throw new Error('剪贴板不可用');
    await navigator.clipboard.writeText(field.value);
    status.textContent='已复制';
  } catch (e) { field.focus(); field.select(); status.textContent='复制失败，文本已选中，请手动复制。'; }
}
function renderBreakdown(data) {
  window.breakdown=data;
  const head=$('breakHead'), body=$('breakBody'); clear(head); clear(body);
  const h=el('tr'); ['名称','总 token','请求数'].forEach(x=>h.append(el('th','',x))); head.append(h);
  for (const x of data[tab].slice(0,12)) { const tr=el('tr'); [x.name,fmt(x.total),fmt(x.requests)].forEach((v,i)=>tr.append(el('td',i===0?'name':'',v))); body.append(tr); }
  if (!data[tab].length) body.append(el('tr','', ''));
}
function issueItem(x, observation=false) {
  const item=el('div','issue'), top=el('div','issue-top');
  top.append(el('span','badge',observation ? '观察' : `${x.confidence || '未知'} · 事件证据可信度`),el('span','issue-title',x.title));item.append(top);
  const object=[x.tool,x.command,x.target_path && `${x.target_path}${x.target_line ? ':'+x.target_line : ''}`].filter(Boolean).join(' · ');
  if(object)item.append(el('p','issue-object',object));
  item.append(el('p','',`${x.session_title ? short(x.session_title)+' · ' : ''}${x.timestamp || '时间未知'} · ${x.evidence || '无证据摘要'}`));
  if(x.suggestion)item.append(el('p','issue-suggestion','建议：'+x.suggestion));
  if(x.id && !observation)item.append(promptButton(x.id));
  if(x.session_id)item.addEventListener('click',()=>openSession(x.session_id));
  return item;
}
async function renderIssues(view='actionable') {
  const observation=view==='observations', offset=observation ? observationOffset : issueOffset;
  const data=await get(`/api/issues?days=${days}&view=${view}&limit=100&offset=${offset}`), box=$(observation?'observations':'issues'); clear(box);
  $(observation?'observationCount':'issueCount').textContent=fmt(data.total)+' 条';
  if (!data.items.length) box.append(el('div','empty',observation?'当前范围暂无运行观察':'当前范围暂无规则线索'));
  for (const x of data.items) box.append(issueItem(x,observation));
  pager($(observation?'observationMore':'issueMore'),data.total,offset,100,x=>{if(observation)observationOffset=x;else issueOffset=x;renderIssues(view);});
}
async function renderConfigAudit() {
  const box=$('configFindings'), observations=$('configObservations');clear(box);clear(observations);
  try {
    const data=await get('/api/config-audit');
    $('configStatus').textContent=`检查于 ${data.checked_at ? new Date(data.checked_at).toLocaleString('zh-CN') : '时间未知'}`;
    for(const x of data.findings || [])box.append(issueItem(x));
    if(!(data.findings || []).length)box.append(el('div','empty',data.errors && data.errors.length ? '检查存在缺口，无法确认配置无问题' : '未发现有证据的配置问题'));
    $('configObservationCount').textContent=fmt((data.observations || []).length)+' 条';
    for(const x of data.observations || [])observations.append(issueItem(x,true));
    if(!(data.observations || []).length)observations.append(el('div','empty','暂无配置观察'));
    const scope=typeof data.scope==='string' ? data.scope : JSON.stringify(data.scope || {});
    $('configScope').textContent=`范围：${scope} · 检查文件 ${fmt(data.files_checked)}${(data.errors || []).length ? ' · 检查缺口：'+data.errors.map(x=>typeof x==='string'?x:JSON.stringify(x)).join('；') : ''}`;
  } catch(e) { $('configStatus').textContent='检查失败';box.append(el('div','empty','配置检查不可用：'+e.message));$('configScope').textContent='未完成配置覆盖检查'; }
}
async function renderSessions() {
  const search=$('search').value, q=encodeURIComponent(search), data=await get(`/api/sessions?grouped=1&days=${days}&q=${q}&limit=100&offset=${sessionOffset}`), body=$('sessions'); clear(body);
  function showTree(row,visible) {
    row.hidden=!visible;
    if(row._toggle)row._toggle.textContent=row._open?'▾':'▸';
    for(const child of row._childRows)showTree(child,visible && row._open);
  }
  function addRow(x,depth=0) {
    const tr=el('tr','clickable');
    tr._open=expandedSessions.has(x.id) ? expandedSessions.get(x.id) : Boolean(search && x.search_path);
    const usage=x.group_usage || x.self_usage || x;
    const name=el('td','name');name.style.paddingLeft=`${12+depth*18}px`;
    if((x.children || []).length){const toggle=el('button','tree-toggle','▸');toggle.type='button';toggle.setAttribute('aria-label',`展开 ${x.title || x.id} 的子任务`);name.append(toggle);toggle.addEventListener('click',e=>{e.stopPropagation();tr._open=!tr._open;expandedSessions.set(x.id,tr._open);showTree(tr,!tr.hidden);});tr._toggle=toggle;}
    name.append(document.createTextNode(short(x.title || x.id)));
    if(x.descendant_count)name.append(el('small','muted',` · ${fmt(x.descendant_count)} 个子任务`));
    if(x.hierarchy_warning)name.append(el('small','hierarchy-warning',` · ${x.hierarchy_warning}`));
    tr.append(name);
    [[x.source,''],[short(x.cwd),'path'],[`${fmt(usage.input)} / ${fmt(usage.cached)}`,''],[fmt(usage.output),''],[fmt(usage.total),''],[fmt(usage.requests),''],[fmt(usage.issue_count),''],[quotaEstimateValue(x.group_weekly_quota_estimate || x.weekly_quota_estimate),'']].forEach(([v,c])=>tr.append(el('td',c,v)));
    if(x.descendant_count)tr.title=`含子任务 ${fmt(usage.total)} token；自身 ${fmt(x.total)} token。子任务明细已包含在汇总中。`;
    tr.addEventListener('click',()=>openSession(x.id));body.append(tr);
    tr._childRows=(x.children || []).map(child=>addRow(child,depth+1));
    return tr;
  }
  for(const x of data.items)showTree(addRow(x),true);
  if (!data.items.length) { const tr=el('tr'); const td=el('td','empty','当前范围没有匹配会话'); td.colSpan=9; tr.append(td); body.append(tr); }
  pager($('sessionMore'),data.total,sessionOffset,100,x=>{sessionOffset=x;renderSessions();});
}
function quotaEstimateValue(q) {
  q=q || {};
  return q.state==='out_of_window' ? '本周期无记录' : q.state==='unobserved' ? '未观察到用量' : q.state!=='estimated' || q.percent == null ? '—' : q.percent > 0 && q.percent < 0.001 ? '<0.001%' : `≈${q.percent.toFixed(q.percent < 0.1 ? 3 : 2)}%`;
}
function detailSection(body,title,items,format) {
  const card=el('section','detail-card'); card.append(el('h3','',`${title} · ${fmt(items.length)}`)); const list=el('div','detail-list');
  items.forEach(x=>list.append(el('div','detail-item',format(x)))); if (!items.length) list.append(el('div','detail-item','暂无记录')); card.append(list); body.append(card);
}
function renderQuotaEstimate(card, estimate, label='本周额度占用（自身估算）') {
  const q=estimate || {}, box=el('div','estimate');
  box.append(el('div','estimate-label',label),el('div','estimate-value',quotaEstimateValue(q)));
  if(q.reason)box.append(el('p','estimate-reason',q.reason));
  if(q.stale)box.append(el('p','estimate-reason','额度快照已陈旧'));
  box.append(el('p','estimate-meta',`额度周期 ${quotaTime(q.window_start)} 至 ${quotaTime(q.window_end)} · 快照 ${q.as_of ? new Date(q.as_of).toLocaleString('zh-CN') : '—'} · ${fmt(q.session_count || 0)} 个会话`));
  const details=el('details','estimate-method');details.append(el('summary','','计算口径'));
  details.append(el('p','','估算占整周额度百分比 = 账号本周期已用百分比 × 本会话本周期加权用量 ÷ 本机本周期已知模型加权总用量。官方 Standard credit 费率仅作模型相对权重，不能确定订阅额度实际扣额。'));
  details.append(el('p','',`账号已用 ${q.account_used_percent == null ? '—' : q.account_used_percent + '%'} · 会话权重 ${q.session_weight == null ? '—' : q.session_weight.toFixed(6)} · 总权重 ${q.total_weight == null ? '—' : q.total_weight.toFixed(6)} · 已知模型 token 覆盖率 ${q.known_token_coverage_percent == null ? '—' : q.known_token_coverage_percent.toFixed(2) + '%'} · 未知模型 ${q.unknown_models && q.unknown_models.length ? q.unknown_models.join('、') : '无'}`));
  for(const assumption of q.assumptions || [])details.append(el('p','',assumption));
  const source=el('a','','官方费率来源');source.href=q.rate_source || 'https://learn.chatgpt.com/docs/pricing';source.target='_blank';source.rel='noopener noreferrer';details.append(source);box.append(details);card.append(box);
}
function analysisCurrent(session, request) { return activeSession===session && detailRequest===request && !$('drawer').hidden; }
function stopAnalysisPoll() { if (analysisTimer) clearTimeout(analysisTimer); analysisTimer=null; }
function showPrompt(title, prompt) {
  promptRequest++;
  $('promptTitle').textContent=title;
  $('promptText').value=prompt;
  $('promptStatus').textContent='可编辑并复制，发送给你自己的 Codex。';
  $('promptDialog').showModal();
}
function appendText(parent,label,value) {
  const row=el('p','analysis-line');row.append(el('strong','',label+'：'),document.createTextNode(value || '未提供'));parent.append(row);
}
function renderReferences(parent, ids, events) {
  const lookup=new Map((events || []).map(x=>[x.id,x]));
  for(const id of ids || []) {
    const event=lookup.get(id);
    const source=event && event.source;
    parent.append(el('div','analysis-ref',`${id} · ${event ? event.text : '原始证据不可用'}${source ? ` · ${source.path}:${source.line}` : ''}`));
  }
}
function renderAnalysisJob(box, job, session, request) {
  analysisJob=job;clear(box);
  const status={queued:'排队中',running:'分析中',completed:'已完成',failed:'失败',cancelled:'已取消',interrupted:'上次运行中断'}[job.status] || job.status;
  box.append(el('p','analysis-status',`状态：${status}${job.reused ? ' · 复用已有结果' : ''}`));
  if (job.status==='queued' || job.status==='running') {
    const cancel=el('button','prompt-button','取消诊断');cancel.type='button';
    cancel.addEventListener('click',async()=>{
      cancel.disabled=true;
      try { const updated=await post('/api/deep-analysis/cancel',{id:job.id});if(analysisCurrent(session,request))renderAnalysisJob(box,updated,session,request); }
      catch(e){if(analysisCurrent(session,request))box.append(el('p','analysis-error','取消失败：'+e.message));}
    });box.append(cancel);
    stopAnalysisPoll();analysisTimer=setTimeout(()=>pollAnalysis(box,job.id,session,request),1500);
  } else stopAnalysisPoll();
  if (job.error) box.append(el('p','analysis-error',job.error));
  if (job.usage) box.append(el('p','muted',`本次诊断实际用量：输入 ${fmt(job.usage.input_tokens)}（缓存 ${fmt(job.usage.cached_input_tokens)}），输出 ${fmt(job.usage.output_tokens)} token`));
  else box.append(el('p','muted','本次诊断实际用量：未记录'));
  if (!job.result) return;
  box.append(el('h4','','结论'),el('p','',job.result.summary));
  const events=job.evidence && job.evidence.events || [];
  const guidance=new Map((job.guidance || []).map(x=>[x.id,x]));
  (job.result.findings || []).forEach((finding,index)=>{
    const card=el('section','analysis-finding');
    card.append(el('h4','',finding.title),el('p','muted',`状态 ${finding.status} · 严重性 ${finding.severity} · 结论可信度 ${finding.confidence} · ${finding.category}`));
    appendText(card,'目标',finding.goal);appendText(card,'问题',finding.problem);
    card.append(el('h5','','有效证据'));renderReferences(card,finding.evidence_refs,events);
    card.append(el('h5','','反证'));renderReferences(card,finding.counterevidence_refs,events);
    const rec=finding.recommendation || {};
    appendText(card,'建议',rec.action);appendText(card,'原因',rec.reason);appendText(card,'备选',rec.alternative);
    appendText(card,'取舍',rec.tradeoff);appendText(card,'验证',rec.validation);
    if((finding.limitations || []).length)appendText(card,'限制',finding.limitations.join('；'));
    const sources=el('div','analysis-guidance');sources.append(el('strong','','公开评审依据：'));
    for(const id of rec.principle_refs || []){
      const source=guidance.get(id);if(!source)continue;
      if(/^https:\/\//.test(source.url)) {const link=el('a','',source.title);link.href=source.url;link.target='_blank';link.rel='noopener noreferrer';sources.append(link);}
      else sources.append(el('span','',source.title));
    }
    card.append(sources);
    const button=el('button','prompt-button',finding.status==='resolved' || finding.status==='superseded' ? '复制复盘 Prompt' : '复制修复 Prompt');
    button.type='button';button.addEventListener('click',async()=>{
      try {const result=await get(`/api/deep-analysis/prompt?id=${encodeURIComponent(job.id)}&index=${index}`);if(analysisCurrent(session,request))showPrompt(result.title || '修复 Prompt',result.prompt);}
      catch(e){if(analysisCurrent(session,request))box.append(el('p','analysis-error','生成 Prompt 失败：'+e.message));}
    });card.append(button);box.append(card);
  });
  if((job.result.uncertainties || []).length)appendText(box,'尚不确定',job.result.uncertainties.join('；'));
}
async function pollAnalysis(box,id,session,request) {
  if(!analysisCurrent(session,request))return;
  try {const job=await get('/api/deep-analysis?id='+encodeURIComponent(id));if(analysisCurrent(session,request))renderAnalysisJob(box,job,session,request);}
  catch(e){if(analysisCurrent(session,request)){box.append(el('p','analysis-error','读取状态失败：'+e.message));analysisTimer=setTimeout(()=>pollAnalysis(box,id,session,request),3000);}}
}
function renderDeepAnalysis(body,session,request) {
  const card=el('section','detail-card analysis-card');card.append(el('h3','','深度诊断'));
  card.append(el('p','muted','准备证据不会调用模型。只有点击开始后才会消耗 Codex 账号额度；结果需人工核对，不会自动修复项目。'));
  const focus=el('textarea','analysis-focus');focus.placeholder='可选：本次要重点核对的问题';focus.maxLength=2000;
  const children=el('input');children.type='checkbox';children.checked=true;
  const childLabel=el('label','analysis-check');childLabel.append(children,document.createTextNode('包含子任务'));
  const prepare=el('button','prompt-button','准备诊断材料');prepare.type='button';
  const previewBox=el('div','analysis-preview'),resultBox=el('div','analysis-result');
  card.append(focus,childLabel,prepare,previewBox,resultBox);body.append(card);
  const invalidate=()=>{analysisPreview=null;clear(previewBox);};focus.addEventListener('input',invalidate);children.addEventListener('change',invalidate);
  prepare.addEventListener('click',async()=>{
    prepare.disabled=true;clear(previewBox);previewBox.append(el('p','muted','正在准备证据…'));
    const selectedFocus=focus.value,selectedChildren=children.checked;
    try {
      const preview=await get(`/api/deep-analysis/preview?session_id=${encodeURIComponent(session)}&focus=${encodeURIComponent(selectedFocus)}&include_children=${selectedChildren?'1':'0'}`);
      if(!analysisCurrent(session,request)||focus.value!==selectedFocus||children.checked!==selectedChildren)return;
      analysisPreview=preview;clear(previewBox);
      previewBox.append(el('p','',`模型 ${preview.model} · 推理 ${preview.effort} · 估算输入 ${fmt(preview.estimate && preview.estimate.approx_input_tokens)} token · 证据 ${fmt(preview.estimate && preview.estimate.evidence_chars)} 字符`));
      if(preview.estimate && preview.estimate.note)previewBox.append(el('p','muted',preview.estimate.note));
      const coverage=preview.evidence && preview.evidence.coverage || {};
      previewBox.append(el('p','muted',`证据 ${fmt(coverage.selected_events)} 条 · 跳过 ${fmt(coverage.omitted_events)} 条${coverage.partial ? ' · 部分抽样' : ''}`));
      for(const note of coverage.notes || [])previewBox.append(el('p','muted',note));
      const evidence=el('details','analysis-evidence');evidence.append(el('summary','','查看准备的证据'));
      for(const event of preview.evidence && preview.evidence.events || [])evidence.append(el('p','analysis-ref',`${event.id} · ${event.text} · ${event.source && event.source.path || ''}:${event.source && event.source.line || ''}`));
      previewBox.append(evidence);
      const copy=el('button','prompt-button','预览/复制分析 Prompt');copy.type='button';copy.addEventListener('click',()=>showPrompt('分析 Prompt',preview.prompt));
      const start=el('button','prompt-button','开始 Codex 分析（消耗账号额度）');start.type='button';
      start.addEventListener('click',async()=>{
        start.disabled=true;
        try {const job=await post('/api/deep-analysis',{preview_id:preview.preview_id});if(analysisCurrent(session,request))renderAnalysisJob(resultBox,job,session,request);}
        catch(e){if(analysisCurrent(session,request)){resultBox.append(el('p','analysis-error','启动失败：'+e.message));start.disabled=false;}}
      });previewBox.append(copy,start);
    } catch(e){if(analysisCurrent(session,request)){clear(previewBox);previewBox.append(el('p','analysis-error','准备失败：'+e.message));}}
    finally{if(analysisCurrent(session,request))prepare.disabled=false;}
  });
  get('/api/deep-analysis/latest?session_id='+encodeURIComponent(session)).then(data=>{
    if(analysisCurrent(session,request) && !analysisJob && data.job)renderAnalysisJob(resultBox,data.job,session,request);
  }).catch(e=>{if(analysisCurrent(session,request))resultBox.append(el('p','analysis-error','读取历史诊断失败：'+e.message));});
}
async function openSession(id) {
  const request=++detailRequest;activeSession=id;analysisPreview=null;analysisJob=null;stopAnalysisPoll();
  const data=await get('/api/session?id='+encodeURIComponent(id));
  if(activeSession!==id || detailRequest!==request)return;
  const s=data.session;
  $('detailTitle').textContent=s.title || s.id; $('drawer').hidden=false; $('backdrop').hidden=false;
  const body=$('detailBody'); clear(body);
  const card=el('section','detail-card'); card.append(el('h3','','会话历史统计')); card.append(el('p','muted',`历史范围：${s.created ? new Date(s.created).toLocaleString('zh-CN') : '起始未知'} 至 ${s.last_active ? new Date(s.last_active).toLocaleString('zh-CN') : '结束未知'}。以下总量属于该会话历史；本周额度另按当前窗口估算。`)); const grid=el('div','detail-grid');
  [['总 token',fmt(s.total)],['模型请求',fmt(s.requests)],['工具调用',fmt(s.tool_calls)],['输入 / 缓存',`${fmt(s.input)} / ${fmt(s.cached)}`],['输出 / 推理',`${fmt(s.output)} / ${fmt(s.reasoning)}`],['来源',s.source]].forEach(([a,b])=>{const cell=el('div','',a);cell.append(el('b','',b));grid.append(cell);});card.append(grid); renderQuotaEstimate(card,data.weekly_quota_estimate);if(data.descendant_count)renderQuotaEstimate(card,data.group_weekly_quota_estimate,`本周额度占用（含 ${fmt(data.descendant_count)} 个子任务，估算）`);if(data.hierarchy_warning)card.append(el('p','hierarchy-warning',data.hierarchy_warning)); card.append(el('p','muted',`ID ${s.id} · ${s.cwd || '未知目录'} · ${s.status}`)); body.append(card);
  if(s.source==='subagent' && s.parent_id && !data.hierarchy_warning){const parent=el('p','linklike','父会话 '+s.parent_id);parent.addEventListener('click',()=>openSession(s.parent_id));card.append(parent);}
  renderDeepAnalysis(body,id,request);
  const issueCard=el('section','detail-card'); issueCard.append(el('h3','','规则线索（待核对） · '+fmt((data.issues || []).length))); const issueList=el('div','detail-list');
  for (const x of data.issues || []) issueList.append(issueItem(x));
  if (!(data.issues || []).length) issueList.append(el('div','detail-item','暂无线索')); issueCard.append(issueList); body.append(issueCard);
  const observationCard=el('details','detail-card observation');observationCard.append(el('summary','','历史诊断：运行观察 · '+fmt((data.observations || []).length)));const observationList=el('div','detail-list');
  for(const x of data.observations || [])observationList.append(issueItem(x,true));
  if(!(data.observations || []).length)observationList.append(el('div','detail-item','暂无观察'));observationCard.append(observationList);body.append(observationCard);
  detailSection(body,'轮次',data.turns,x=>`${x.id} · ${fmt(x.total)} token · ${fmt(x.requests)} 请求`);
  detailSection(body,'用量明细'+(data.truncated.usage?'（最近 500 条）':''),data.usage,x=>`${x.timestamp} · ${x.purpose==='compaction'?'上下文压缩':x.model} · 输入 ${fmt(x.input)}（缓存 ${fmt(x.cached)}）· 输出 ${fmt(x.output)} · 总 ${fmt(x.total)} · ${x.basis} · ${x.path}:${x.line}`);
  if((data.tool_events || []).length)detailSection(body,'结构化工具事件'+(data.truncated && data.truncated.tool_events?'（最近 500 条）':''),data.tool_events,x=>`${x.timestamp || '时间未知'} · ${x.tool || '工具未知'} · ${x.command || '无命令预览'} · cwd ${x.cwd || '未知'} · 退出码 ${x.exit_code == null ? '未知' : x.exit_code} · 状态 ${x.status || '未知'} · 目标 ${(x.target_paths || []).join('、') || '无'} · skill ${(x.skill_paths || []).join('、') || '无'} · ${x.path}:${x.line}`);
  else body.append(el('p','muted','未观察到原生结构化工具事件。下方旧调用记录仅是有限回退，不能完整反映批量命令、实际 cwd 或 skill 读取。'));
  detailSection(body,'旧调用记录（有限回退）'+(data.truncated && data.truncated.calls?'（最近 500 条）':''),data.calls || [],x=>`${x.timestamp} · ${x.name} · ${x.command} · ${x.status} · ${fmt(x.output_chars)} 字符 / 约 ${fmt(x.estimated_tokens)} token · ${x.path}:${x.line}`);
  const children=el('section','detail-card');children.append(el('h3','',`子会话 · ${fmt(data.children.length)}`));for(const child of data.children){const item=el('div','detail-item linklike',`${child.title || child.id} · ${fmt(child.total)} token`);item.addEventListener('click',()=>openSession(child.id));children.append(item);}if(!data.children.length)children.append(el('div','detail-item','暂无子会话'));body.append(children);
  detailSection(body,'覆盖告警',data.warnings,x=>`${x.message} · ${x.path}:${x.line}`);
}
async function refresh() {
  try { const [overview]=await Promise.all([get(`/api/overview?days=${days}`),renderIssues(),renderIssues('observations'),renderSessions(),renderConfigAudit()]); renderOverview(overview); }
  catch (e) {$('scanStatus').textContent='读取失败：'+e.message;}
}
$('days').addEventListener('change',e=>{days=Number(e.target.value);issueOffset=0;observationOffset=0;sessionOffset=0;refresh();});
$('search').addEventListener('input',()=>{sessionOffset=0;clearTimeout(searchTimer);searchTimer=setTimeout(renderSessions,220);});
$('breakTabs').addEventListener('click',e=>{if (!e.target.dataset.tab)return;tab=e.target.dataset.tab;document.querySelectorAll('#breakTabs button').forEach(x=>x.classList.toggle('active',x.dataset.tab===tab));if(window.breakdown)renderBreakdown(window.breakdown);});
function close(){ detailRequest++;activeSession=null;analysisPreview=null;stopAnalysisPoll();$('drawer').hidden=true;$('backdrop').hidden=true; }
$('close').addEventListener('click',close);$('backdrop').addEventListener('click',close);
$('promptCopy').addEventListener('click',copyIssuePrompt);
$('promptDialog').addEventListener('close',()=>{promptRequest++;});
for (const id of ['promptClose','promptDone']) $(id).addEventListener('click',()=>$('promptDialog').close());
document.addEventListener('keydown',e=>{if(e.key==='Escape' && !$('promptDialog').open)close();});
refresh();setInterval(refresh,10000);
refreshQuota();setInterval(refreshQuota,60000);setInterval(renderQuota,1000);
