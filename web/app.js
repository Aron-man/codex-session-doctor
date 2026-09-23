const $ = id => document.getElementById(id);
const nf = new Intl.NumberFormat('zh-CN');
const fmt = n => n == null ? '—' : nf.format(n);
const short = s => (s || '').length > 70 ? s.slice(0, 70) + '…' : (s || '—');
let days = 7, tab = 'models', searchTimer, issueOffset = 0, sessionOffset = 0;
let quotaSnapshot = null;
let promptRequest = 0;

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
  if (!response.ok || data.error) throw new Error(data.error || response.statusText);
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
  [['文件',`${fmt(c.scanned_files)} / ${fmt(c.files)}`],['字节',`${fmt(c.scanned_bytes)} / ${fmt(c.bytes)}`],['解析错误',fmt(c.parse_errors)],['缺用量会话',fmt(c.missing_usage_sessions)],['覆盖告警',fmt(c.warning_count)]].forEach(([a,b])=>line(panel,'coverage-line',[['span','',a],['b','',b]]));
  const note=el('div','coverage-note'); note.textContent=(c.error ? `扫描错误：${c.error}。` : '') + c.limitations.join('；'); panel.append(note);
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
function promptButton(id) {
  const button=el('button','prompt-button','生成修复 Prompt'); button.type='button';
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
async function renderIssues() {
  const data=await get(`/api/issues?days=${days}&limit=100&offset=${issueOffset}`), box=$('issues'); clear(box); $('issueCount').textContent=fmt(data.total)+' 条';
  if (!data.items.length) box.append(el('div','empty','当前范围暂无规则诊断'));
  for (const x of data.items) {
    const item=el('div','issue'); const top=el('div','issue-top'); top.append(el('span','badge '+x.severity,x.severity==='info'?'待检查':'建议处理'),el('span','issue-title',x.title)); item.append(top);
    item.append(el('p','',`${short(x.session_title)} · ${x.evidence}`),promptButton(x.id)); item.addEventListener('click',()=>openSession(x.session_id)); box.append(item);
  }
  pager($('issueMore'),data.total,issueOffset,100,x=>{issueOffset=x;renderIssues();});
}
async function renderSessions() {
  const q=encodeURIComponent($('search').value), data=await get(`/api/sessions?days=${days}&q=${q}&limit=100&offset=${sessionOffset}`), body=$('sessions'); clear(body);
  for (const x of data.items) {
    const tr=el('tr','clickable'); [[short(x.title || x.id),'name'],[x.source,''],[short(x.cwd),'path'],[`${fmt(x.input)} / ${fmt(x.cached)}`,''],[fmt(x.output),''],[fmt(x.total),''],[fmt(x.requests),''],[fmt(x.issue_count),'']].forEach(([v,c])=>tr.append(el('td',c,v)));
    tr.addEventListener('click',()=>openSession(x.id)); body.append(tr);
  }
  if (!data.items.length) { const tr=el('tr'); const td=el('td','empty','当前范围没有匹配会话'); td.colSpan=8; tr.append(td); body.append(tr); }
  pager($('sessionMore'),data.total,sessionOffset,100,x=>{sessionOffset=x;renderSessions();});
}
function detailSection(body,title,items,format) {
  const card=el('section','detail-card'); card.append(el('h3','',`${title} · ${fmt(items.length)}`)); const list=el('div','detail-list');
  items.forEach(x=>list.append(el('div','detail-item',format(x)))); if (!items.length) list.append(el('div','detail-item','暂无记录')); card.append(list); body.append(card);
}
function renderQuotaEstimate(card, estimate) {
  const q=estimate || {}, box=el('div','estimate');
  const value=q.percent == null ? '—' : q.percent > 0 && q.percent < 0.001 ? '<0.001%' : `≈${q.percent.toFixed(q.percent < 0.1 ? 3 : 2)}%`;
  box.append(el('div','estimate-label','本周额度占用（估算）'),el('div','estimate-value',value));
  if(q.reason)box.append(el('p','estimate-reason',q.reason));
  if(q.stale)box.append(el('p','estimate-reason','额度快照已陈旧'));
  box.append(el('p','estimate-meta',`额度周期 ${quotaTime(q.window_start)} 至 ${quotaTime(q.window_end)} · 快照 ${q.as_of ? new Date(q.as_of).toLocaleString('zh-CN') : '—'} · 仅本周期会话自身用量`));
  const details=el('details','estimate-method');details.append(el('summary','','计算口径'));
  details.append(el('p','','估算占整周额度百分比 = 账号本周期已用百分比 × 本会话本周期加权用量 ÷ 本机本周期已知模型加权总用量。官方 Standard credit 费率仅作模型相对权重，不能确定订阅额度实际扣额。'));
  details.append(el('p','',`账号已用 ${q.account_used_percent == null ? '—' : q.account_used_percent + '%'} · 会话权重 ${q.session_weight == null ? '—' : q.session_weight.toFixed(6)} · 总权重 ${q.total_weight == null ? '—' : q.total_weight.toFixed(6)} · 已知模型 token 覆盖率 ${q.known_token_coverage_percent == null ? '—' : q.known_token_coverage_percent.toFixed(2) + '%'} · 未知模型 ${q.unknown_models && q.unknown_models.length ? q.unknown_models.join('、') : '无'}`));
  for(const assumption of q.assumptions || [])details.append(el('p','',assumption));
  const source=el('a','','官方费率来源');source.href=q.rate_source || 'https://learn.chatgpt.com/docs/pricing';source.target='_blank';source.rel='noopener noreferrer';details.append(source);box.append(details);card.append(box);
}
async function openSession(id) {
  const data=await get('/api/session?id='+encodeURIComponent(id)), s=data.session;
  $('detailTitle').textContent=s.title || s.id; $('drawer').hidden=false; $('backdrop').hidden=false;
  const body=$('detailBody'); clear(body);
  const card=el('section','detail-card'); card.append(el('h3','','会话概览')); const grid=el('div','detail-grid');
  [['总 token',fmt(s.total)],['模型请求',fmt(s.requests)],['工具调用',fmt(s.tool_calls)],['输入 / 缓存',`${fmt(s.input)} / ${fmt(s.cached)}`],['输出 / 推理',`${fmt(s.output)} / ${fmt(s.reasoning)}`],['来源',s.source]].forEach(([a,b])=>{const cell=el('div','',a);cell.append(el('b','',b));grid.append(cell);});card.append(grid); renderQuotaEstimate(card,data.weekly_quota_estimate); card.append(el('p','muted',`ID ${s.id} · ${s.cwd || '未知目录'} · ${s.status}`)); body.append(card);
  if(s.parent_id){const parent=el('p','linklike','父会话 '+s.parent_id);parent.addEventListener('click',()=>openSession(s.parent_id));card.append(parent);}
  const issueCard=el('section','detail-card'); issueCard.append(el('h3','','诊断建议 · '+fmt(data.issues.length))); const issueList=el('div','detail-list');
  for (const x of data.issues) { const item=el('div','detail-item'); item.append(el('div','',`${x.title}：${x.evidence} 建议：${x.suggestion} · ${x.path}:${x.line}`),promptButton(x.id)); issueList.append(item); }
  if (!data.issues.length) issueList.append(el('div','detail-item','暂无记录')); issueCard.append(issueList); body.append(issueCard);
  detailSection(body,'轮次',data.turns,x=>`${x.id} · ${fmt(x.total)} token · ${fmt(x.requests)} 请求`);
  detailSection(body,'用量明细'+(data.truncated.usage?'（最近 500 条）':''),data.usage,x=>`${x.timestamp} · ${x.purpose==='compaction'?'上下文压缩':x.model} · 输入 ${fmt(x.input)}（缓存 ${fmt(x.cached)}）· 输出 ${fmt(x.output)} · 总 ${fmt(x.total)} · ${x.basis} · ${x.path}:${x.line}`);
  detailSection(body,'工具调用'+(data.truncated.calls?'（最近 500 条）':''),data.calls,x=>`${x.timestamp} · ${x.name} · ${x.command} · ${x.status} · ${fmt(x.output_chars)} 字符 / 约 ${fmt(x.estimated_tokens)} token · ${x.path}:${x.line}`);
  const children=el('section','detail-card');children.append(el('h3','',`子会话 · ${fmt(data.children.length)}`));for(const child of data.children){const item=el('div','detail-item linklike',`${child.title || child.id} · ${fmt(child.total)} token`);item.addEventListener('click',()=>openSession(child.id));children.append(item);}if(!data.children.length)children.append(el('div','detail-item','暂无子会话'));body.append(children);
  detailSection(body,'覆盖告警',data.warnings,x=>`${x.message} · ${x.path}:${x.line}`);
}
async function refresh() {
  try { const [overview]=await Promise.all([get(`/api/overview?days=${days}`),renderIssues(),renderSessions()]); renderOverview(overview); }
  catch (e) {$('scanStatus').textContent='读取失败：'+e.message;}
}
$('days').addEventListener('change',e=>{days=Number(e.target.value);issueOffset=0;sessionOffset=0;refresh();});
$('search').addEventListener('input',()=>{sessionOffset=0;clearTimeout(searchTimer);searchTimer=setTimeout(renderSessions,220);});
$('breakTabs').addEventListener('click',e=>{if (!e.target.dataset.tab)return;tab=e.target.dataset.tab;document.querySelectorAll('#breakTabs button').forEach(x=>x.classList.toggle('active',x.dataset.tab===tab));if(window.breakdown)renderBreakdown(window.breakdown);});
function close(){ $('drawer').hidden=true;$('backdrop').hidden=true; }
$('close').addEventListener('click',close);$('backdrop').addEventListener('click',close);
$('promptCopy').addEventListener('click',copyIssuePrompt);
$('promptDialog').addEventListener('close',()=>{promptRequest++;});
for (const id of ['promptClose','promptDone']) $(id).addEventListener('click',()=>$('promptDialog').close());
document.addEventListener('keydown',e=>{if(e.key==='Escape' && !$('promptDialog').open)close();});
refresh();setInterval(refresh,10000);
refreshQuota();setInterval(refreshQuota,60000);setInterval(renderQuota,1000);
