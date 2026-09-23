const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const nodes = new Map();
function node() {
  return {
    listeners: {}, classList: {toggle() {}}, style: {}, value: '', open: false,
    addEventListener(type, fn) { this.listeners[type] = fn; },
    append() {}, replaceChildren() {}, focus() { this.focused = true; },
    select() { this.selected = true; },
    showModal() { this.open = true; }, close() { this.open = false; },
  };
}
const document = {
  listeners: {},
  getElementById(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); },
  createElement: node,
  addEventListener(type, fn) { this.listeners[type] = fn; },
};
const context = vm.createContext({document, window: {}, navigator: {clipboard: {
  async writeText(value) { context.copied = value; },
}}, Intl, Date, Number, Math, String, Boolean, Promise, setTimeout() {}, clearTimeout() {}});
const source = fs.readFileSync('web/app.js', 'utf8').replace(/refresh\(\);setInterval\(refresh,10000\);\s*refreshQuota\(\);setInterval\(refreshQuota,60000\);setInterval\(renderQuota,1000\);\s*$/, '');
vm.runInContext(source, context);

(async () => {
  context.fetch = async () => ({ok: true, json: async () => ({prompt: '可编辑内容'})});
  const button = vm.runInContext("promptButton('issue-1')", context);
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

  document.getElementById('drawer').hidden = false;
  document.listeners.keydown({key: 'Escape'});
  assert.equal(nodes.get('drawer').hidden, false);
  nodes.get('promptDialog').close();
  document.listeners.keydown({key: 'Escape'});
  assert.equal(nodes.get('drawer').hidden, true);
})().catch(error => { console.error(error); process.exitCode = 1; });
