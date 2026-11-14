/**
 * 多值标签字段（接入行情 / 接入柜台）页面级验证。
 * jsdom 加载真实 index.html + 真实 app.js，只桩 window.fetch。
 *
 * 1. 服务器表格里两个字段渲染成一串小标签
 * 2. 修改弹窗里渲染成标签多选，已有值显示成 chip，hidden input 存 "A, B"
 * 3. 点 × 能移除、点候选项能加回
 * 4. 管理人员输入新值时出现「新建标签」并调用 POST /api/field-options
 * 5. 设置页有「字段标签管理」，两个字段各一个面板，带使用次数与改名/删除按钮
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const APP_DIR = path.resolve(__dirname, '..');
const appJs = fs.readFileSync(path.join(APP_DIR, 'app/static/app.js'), 'utf8');
let html = fs.readFileSync(path.join(APP_DIR, 'app/templates/index.html'), 'utf8');

html = html
  .replace(/<link rel="stylesheet"[^>]*>/, '')
  .replace(/<script src="[^"]*app\.js"><\/script>/, '')
  .replace('{{ role|tojson }}', '"admin"')
  .replace('{{ app_user|tojson }}', JSON.stringify({ '用户id': 'admin', '身份': '管理人员' }))
  .replace('{{ capabilities|tojson }}', JSON.stringify(['manage_all', 'review_operations', 'review_rack_requests', 'submit_rack_request', 'manage_settings', 'view_statistics']))
  .replace('{{ username[0]|upper }}', 'A')
  .replace('{{ username }}', 'admin')
  .replace('{{ role_name }}', '管理人员')
  .replace(/\{\{[^}]*\}\}/g, '');

const SERVER = {
  '序列号': 'SRV-1', '客户id': '1', '客户名称': '客户一',
  '所在机房': '机房A', '所属机柜': 'C1', '所属机位': '3-4',
  '接入行情': 'CTP行情, Level-2', '接入柜台': 'CTP柜台',
};

const FIELD_OPTIONS = {
  entity: 'servers',
  fields: ['接入行情', '接入柜台'],
  options: {
    '接入行情': [
      { value: 'CTP行情', source: 'manual', usage: { official: 1, pending: 0 } },
      { value: 'Level-2', source: 'manual', usage: { official: 1, pending: 0 } },
      { value: 'Level-3', source: 'manual', usage: { official: 0, pending: 0 } },
    ],
    '接入柜台': [
      { value: 'CTP柜台', source: 'manual', usage: { official: 1, pending: 0 } },
      { value: 'XTP柜台', source: 'data', usage: { official: 0, pending: 0 } },
    ],
  },
};

const posts = [];
const fixtures = [
  [/^\/api\/field-options\?/, () => FIELD_OPTIONS],
  [/^\/api\/field-options$/, (opt) => {
    posts.push({ url: '/api/field-options', method: opt?.method || 'GET', body: opt?.body });
    const body = JSON.parse(opt.body || '{}');
    return { message: `标签「${body.value}」已添加`, value: body.value };
  }],
  [/^\/api\/customer-options/, () => ({ customers: [{ '客户id': '1', '客户名称': '客户一', '负责人': '老王' }] })],
  [/^\/api\/cabinet-options/, () => ({
    rooms: [{ name: '机房A', cabinets: [{ name: 'C1', slots: [{ slot: '3', status: 'free', server: '', selectable: true }, { slot: '4', status: 'free', server: '', selectable: true }] }] }],
  })],
  [/^\/api\/attachments\/summary/, () => ({ counts: {} })],
  [/^\/api\/changes\?/, () => ({ changes: [] })],
  [/^\/api\/servers/, () => ({
    records: [SERVER],
    meta: { label: '服务器', key: ['序列号'], sheet: '服务器信息', headers: Object.keys(SERVER) },
    record_state: 'official',
  })],
  [/^\/api\/cabinets/, () => ({
    records: [{ '机房': '机房A', '机柜': 'C1', '机位': '1-10' }],
    meta: { label: '机柜', key: ['机房', '机柜'], sheet: '机柜信息表', headers: ['机房', '机柜', '机位'] },
    record_state: 'official',
  })],
];

function fakeFetch(url, opt) {
  const hit = fixtures.find(([pattern]) => pattern.test(String(url)));
  const payload = hit ? hit[1](opt) : {};
  return Promise.resolve({ ok: true, json: () => Promise.resolve(payload) });
}

let passed = 0;
const failures = [];
function check(name, ok, detail) {
  if (ok) { passed++; return; }
  failures.push(name + (detail ? ' → ' + detail : ''));
}

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  url: 'http://localhost/',
  beforeParse(window) {
    window.fetch = (url, opt) => fakeFetch(url, opt);
    window.alert = () => {};
    window.confirm = () => true;
    window.prompt = () => 'CTP行情V2';
  },
});
dom.window.eval(appJs);

const $ = s => dom.window.document.querySelector(s);
const $$ = s => [...dom.window.document.querySelectorAll(s)];
const tick = (ms = 30) => new Promise(r => setTimeout(r, ms));

async function main() {
  await tick();

  // ===== T1: 信息维护 → 服务器表格 =====
  $$('.entity-tab').find(b => b.dataset.entity === 'servers').onclick();
  await tick();
  const headerIndex = $$('#dataHead th').findIndex(th => th.textContent === '接入行情');
  check('T1 表头有「接入行情」列', headerIndex > 0, String(headerIndex));
  const cells = $$('#dataBody tr td.cell-text');
  const marketCell = cells[headerIndex - 3];
  const marketTags = [...(marketCell?.querySelectorAll('.cell-tags .tag') || [])].map(t => t.textContent);
  check('T1 接入行情渲染成 2 个小标签', marketTags.length === 2 && marketTags[0] === 'CTP行情' && marketTags[1] === 'Level-2', marketTags.join('|'));

  // ===== T2: 修改弹窗的标签控件 =====
  $$('#dataBody .row-actions button').find(b => b.textContent === '修改').onclick();
  await tick(60);
  const marketBox = $('#formFields .tag-field[data-tag-field="接入行情"]');
  check('T2 弹窗里有接入行情标签控件', !!marketBox);
  check('T2 已有 2 个标签 chip', marketBox?.querySelectorAll('.tag-chip').length === 2);
  const hidden = marketBox?.querySelector('input[type=hidden]');
  check('T2 hidden input 存 "A, B" 文本', hidden?.value === 'CTP行情, Level-2', hidden?.value);
  check('T2 chip 上有移除按钮', !!marketBox?.querySelector('[data-tag-remove="CTP行情"]'));

  // ===== T3: 移除一个标签 =====
  marketBox.querySelector('[data-tag-remove="CTP行情"]').click();
  await tick();
  check('T3 移除后只剩 1 个 chip', marketBox.querySelectorAll('.tag-chip').length === 1);
  check('T3 hidden 同步更新', marketBox.querySelector('input[type=hidden]').value === 'Level-2',
    marketBox.querySelector('input[type=hidden]').value);

  // ===== T4: 打开菜单并加回 =====
  const tagInput = marketBox.querySelector('.tag-input');
  tagInput.dispatchEvent(new dom.window.Event('focusin', { bubbles: true }));
  await tick();
  const menu = marketBox.querySelector('.tag-menu');
  check('T4 菜单已展开', menu && !menu.hidden);
  const optionValues = [...menu.querySelectorAll('.tag-option')].map(b => b.dataset.tagValue);
  check('T4 菜单列出池里未选的标签', optionValues.includes('CTP行情') && optionValues.includes('Level-3'), optionValues.join('|'));
  check('T4 已在列表里的不再出现', !optionValues.includes('Level-2'), optionValues.join('|'));
  menu.querySelector('[data-tag-value="CTP行情"]').click();
  await tick();
  check('T4 点选后 chip 回到 2 个', marketBox.querySelectorAll('.tag-chip').length === 2);
  check('T4 hidden 恢复完整值', marketBox.querySelector('input[type=hidden]').value === 'Level-2, CTP行情',
    marketBox.querySelector('input[type=hidden]').value);

  // ===== T5: 管理人员输入新值 → 新建标签 =====
  const input2 = marketBox.querySelector('.tag-input');
  input2.value = '新行情源';
  input2.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  await tick();
  const createBtn = marketBox.querySelector('.tag-menu .tag-create');
  check('T5 出现「新建标签」入口', !!createBtn, marketBox.querySelector('.tag-menu')?.innerHTML?.slice(0, 120));
  createBtn?.click();
  await tick(60);
  const created = posts.find(p => p.method === 'POST');
  check('T5 新建调用 POST /api/field-options', !!created, JSON.stringify(posts));
  const payload = created ? JSON.parse(created.body) : {};
  check('T5 新建请求体正确', payload.field === '接入行情' && payload.value === '新行情源' && payload.entity === 'servers', created?.body);
  check('T5 新建后标签被选中', marketBox.querySelectorAll('.tag-chip').length === 3);

  // ===== T6: 设置页的标签管理 =====
  $$('.nav-item').find(b => b.dataset.page === 'settings').onclick();
  await tick(60);
  const panels = $$('#fieldOptionsPanels .tag-admin-panel');
  check('T6 设置页有两个字段面板', panels.length === 2, String(panels.length));
  const marketPanel = panels.find(p => p.dataset.tagAdmin === '接入行情');
  check('T6 接入行情面板列出 3 个标签', marketPanel?.querySelectorAll('.tag-admin-row').length === 3);
  const usageText = [...(marketPanel?.querySelectorAll('.tag-admin-usage') || [])].map(e => e.textContent);
  check('T6 显示使用次数', usageText.some(t => t.includes('服务器 1 台')), usageText.join('|'));
  check('T6 未使用的标签标为未使用', usageText.includes('未使用'), usageText.join('|'));
  check('T6 每行有改名/删除按钮',
    marketPanel?.querySelectorAll('.tag-admin-actions .mini').length === 6,
    String(marketPanel?.querySelectorAll('.tag-admin-actions .mini').length));
  check('T6 面板有新增输入框', !!marketPanel?.querySelector('.tag-admin-input'));

  report();
}

function report() {
  console.log(`\n多值标签字段：${passed} 项通过${failures.length ? `，${failures.length} 项失败` : ''}`);
  failures.forEach(name => console.log('  ✗ ' + name));
  if (failures.length) process.exit(1);
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
