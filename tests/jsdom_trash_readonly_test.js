/**
 * 回收站只读视图（业务人员）验证：
 * 1. can_edit=false 时卡片不渲染可编辑表单，也没有「保存修改 / 重新上架 / 永久删除」按钮
 * 2. 展开后只展示整行快照的只读详情，并给出只读权限提示
 * 3. 手动登记表单对业务人员隐藏
 * 4. 只读用户仍可使用筛选面板
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
  .replace('{{ role|tojson }}', '"business"')
  .replace('{{ app_user|tojson }}', JSON.stringify({ '用户id': 'biz1', '身份': '业务人员', '业务单位': '单位A' }))
  .replace('{{ capabilities|tojson }}', JSON.stringify(['submit_rack_request', 'view_trash']))
  .replace('{{ username[0]|upper }}', 'B')
  .replace('{{ username }}', 'biz1')
  .replace('{{ role_name }}', '业务人员')
  .replace(/\{\{[^}]*\}\}/g, '');

const TRASH_ITEMS = [
  {
    id: 't-ua', serial_number: 'SRV-UA', origin: '',
    reason: '下架实施完成', deleted_at: '2026-11-25 10:00:00', deleted_by: 'admin',
    request_id: 'req1', '序列号': 'SRV-UA', 客户id: '1', 客户名称: '客户一', 归属单位: '单位A',
    所在机房: '机房A', 所属机柜: 'C1', 所属机位: '3-4', 品牌: 'Dell',
  },
];

const requests = [];
function fakeFetch(url, opt = {}) {
  requests.push({ url: String(url), method: (opt.method || 'GET').toUpperCase() });
  if (String(url) === '/api/trash') return Promise.resolve({
    ok: true,
    json: () => Promise.resolve({
      items: TRASH_ITEMS,
      headers: ['序列号', '客户id', '客户名称', '归属单位', '所在机房', '所属机柜', '所属机位', '品牌'],
      can_manage: false, can_edit: false,
    }),
  });
  if (String(url).startsWith('/api/changes')) return Promise.resolve({ ok: true, json: () => Promise.resolve({ changes: [] }) });
  if (String(url).startsWith('/api/rack-requests')) return Promise.resolve({ ok: true, json: () => Promise.resolve({ requests: [] }) });
  return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
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
    window.prompt = () => '';
  },
});
dom.window.eval(appJs);

const $ = s => dom.window.document.querySelector(s);
const $$ = s => [...dom.window.document.querySelectorAll(s)];
const tick = (ms = 20) => new Promise(r => setTimeout(r, ms));

async function main() {
  await tick();

  $$('.nav-item').find(b => b.dataset.page === 'trash').onclick();
  await tick();

  // ===== T1: 只读卡片，无任何编辑/操作按钮 =====
  check('T1 渲染本单位记录', $$('#trashList .review-card').length === 1,
    String($$('#trashList .review-card').length));
  check('T1 手动登记表单隐藏', $('#trashAddForm')?.hidden === true);
  const card = $('#trashList .review-card');
  check('T1 无编辑表单', !card.querySelector('[data-trash-restore]'));
  check('T1 无保存修改按钮', !card.querySelector('[data-trash-action="save"]'));
  check('T1 无重新上架按钮', !card.querySelector('[data-trash-action="restore"]'));
  check('T1 无永久删除按钮', !card.querySelector('[data-trash-action="purge"]'));
  check('T1 无可编辑的输入框', card.querySelectorAll('input[name]').length === 0,
    String(card.querySelectorAll('input[name]').length));

  // ===== T2: 展开后是只读详情 + 权限提示 =====
  card.querySelector('[data-trash-expand-id]').onclick();
  await tick();
  check('T2 展开显示只读详情', card.textContent.includes('SRV-UA'));
  check('T2 显示只读权限提示', card.textContent.includes('只读权限'), card.textContent.slice(-120));

  // ===== T3: 只读用户仍可筛选 =====
  check('T3 筛选面板可用', !!$('#addTrashFilterBtn') && !!$('#applyTrashFilterBtn'));
  const row = $('#trashFilterRows .trash-filter-row');
  row.querySelector('[data-combo]').dataset.value = '序列号';
  row.querySelector('[data-pattern]').value = 'NOT-MATCH';
  $('#applyTrashFilterBtn').onclick();
  await tick();
  check('T3 筛选不匹配的记录被隐藏', $$('#trashList .review-card').length === 0,
    String($$('#trashList .review-card').length));
  $('#clearTrashFilterBtn').onclick();
  await tick();
  check('T3 清空后记录恢复', $$('#trashList .review-card').length === 1);

  // ===== T4: 全程没有写请求 =====
  check('T4 未发出 PATCH/restore/DELETE',
    !requests.some(r => ['PATCH', 'DELETE'].includes(r.method) || r.url.includes('/restore')),
    requests.map(r => r.method + ' ' + r.url).join(' | '));

  console.log('RESULT: ' + passed + ' passed, ' + failures.length + ' failed');
  if (failures.length) {
    console.log(failures.map(f => '  FAIL ' + f).join('\n'));
    process.exitCode = 1;
  }
}

main().catch(err => {
  console.log('RESULT: 0 passed, 1 failed');
  console.log('  FAIL 运行异常 → ' + (err && err.stack || err));
  process.exitCode = 1;
});
