/**
 * API Token 自助生成（jsdom 加载真实 index.html + 真实 app.js，fetch 走桩数据）：
 * 1. 账号钥匙弹层：没有 token 时直接给「生成 Token」，生成后显示完整值 + 复制 + 重新生成
 * 2. 设置页用户表**不再**有 API Token 列与「Token」按钮（token 不归管理员管）
 * 3. 设置页保存整表时不下发 api_token（后端一律保留原值）
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

const NEW_TOKEN = '1111222233334444555566667777888899990000abc';
const SECOND_TOKEN = 'BBBBAABBAABBAABBAABBAABBAABBAABBAABBAABBAAB';

let myToken = '';
let users = [
  { '用户id': 'admin', '密码': 'pw', '身份': '管理人员', '业务单位': '', '所属机房': '' },
  { '用户id': 'room1', '密码': 'pw', '身份': '机房人员', '业务单位': '', '所属机房': '机房A' },
];

const calls = [];
function ok(payload) {
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload) });
}

function fakeFetch(url, opt = {}) {
  const target = String(url);
  const method = String(opt.method || 'GET').toUpperCase();
  calls.push({ url: target, method });

  if (target === '/api/my-token') return ok({ api_token: myToken });
  if (target === '/api/my-token/regenerate') {
    myToken = myToken ? SECOND_TOKEN : NEW_TOKEN;
    return ok({ api_token: myToken, message: 'Token 已生成，此前的 token 已立即失效' });
  }
  if (target === '/api/system-users') return ok({ users });
  if (target.startsWith('/api/changes')) return ok({ changes: [] });
  if (target.startsWith('/api/rack-requests')) return ok({ requests: [] });
  if (target === '/api/settings') return ok({ settings: { backup_keep_count: 20, backup_command: '', audit_keep_count: 200, notify_enabled: 0, notify_webhook_url: '', notify_message_type: 'markdown' } });
  return ok({});
}

let passed = 0;
const failures = [];
function check(name, condition, detail) {
  if (condition) { passed++; return; }
  failures.push(name + (detail === undefined ? '' : ' → ' + detail));
}

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  url: 'http://localhost/',
  beforeParse(window) {
    window.fetch = (url, opt) => fakeFetch(url, opt);
    window.alert = () => {};
    window.confirm = () => true;
    window.prompt = () => '';
    window.navigator.clipboard = { writeText: () => Promise.resolve() };
  },
});
dom.window.eval(appJs);

const $ = s => dom.window.document.querySelector(s);
const $$ = s => [...dom.window.document.querySelectorAll(s)];
const tick = (ms = 25) => new Promise(r => setTimeout(r, ms));

async function main() {
  await tick();

  // ===== T1: 账号弹层，无 token → 一键生成 =====
  $('#tokenBtn').onclick({ stopPropagation() {} });
  $('#keyMenuToken').onclick();
  await tick();
  check('T1 弹层已打开', !$('#tokenPop').hidden);
  check('T1 无 token 时提示生成', !!$('#tokenCreate'), $('#tokenBody').innerHTML.slice(0, 80));
  check('T1 不再提示联系管理员', !$('#tokenBody').textContent.includes('联系管理员'));

  $('#tokenCreate').onclick();
  await tick();
  check('T1 生成走了 POST /api/my-token/regenerate',
    calls.some(c => c.url === '/api/my-token/regenerate' && c.method === 'POST'));
  check('T1 生成后显示完整 token', $('#tokenBody code') && $('#tokenBody code').textContent === NEW_TOKEN,
    $('#tokenBody code') && $('#tokenBody code').textContent);
  check('T1 生成后有复制与重新生成', !!$('#tokenCopy') && !!$('#tokenRegen'));

  // ===== T2: 重新生成（本人操作，旧 token 失效） =====
  $('#tokenRegen').onclick();
  await tick();
  check('T2 重新生成走同一自助端点',
    calls.filter(c => c.url === '/api/my-token/regenerate' && c.method === 'POST').length === 2,
    calls.map(c => `${c.method} ${c.url}`).join(' | '));
  check('T2 显示新 token', $('#tokenBody code') && $('#tokenBody code').textContent === SECOND_TOKEN,
    $('#tokenBody code') && $('#tokenBody code').textContent);

  // ===== T3: 设置页不再替他人管 token =====
  $$('.nav-item').find(b => b.dataset.page === 'settings').onclick();
  await tick();
  const headers = $$('#systemUsersTable thead th').map(th => th.textContent.trim());
  check('T3 表头没有 API Token 列', !headers.includes('API Token'), headers.join(','));
  check('T3 操作列仍在最后', headers[headers.length - 1] === '操作', headers.join(','));
  check('T3 没有任何「Token」按钮', $$('#systemUsersBody .mini.token').length === 0);
  check('T3 行内不再带 api_token 隐藏域', $$('#systemUsersBody input[name="api_token"]').length === 0);
  const hint = $$('#settings p.muted').map(p => p.textContent).join(' ');
  check('T3 提示改为用户自助生成', hint.includes('自行生成') && hint.includes('获取 Token'), hint);

  // ===== T4: 设置页保存整表不带 api_token =====
  const collected = dom.window.eval('collectSystemUsers()');
  check('T4 保存时不带 api_token 字段', collected.every(u => !('api_token' in u)),
    JSON.stringify(collected[0]));
  check('T4 用户字段仍然完整', collected.length === 2 && collected[0]['用户id'] === 'admin');

  // ===== T5: 已生成后弹层可复制 =====
  $('#tokenBtn').onclick({ stopPropagation() {} });
  await tick();
  if ($('#tokenPop').hidden) { $('#keyMenuToken').onclick(); await tick(); }
  check('T5 弹层显示已生成的 token', $('#tokenBody code')?.textContent === SECOND_TOKEN,
    $('#tokenBody code')?.textContent);
  let copied = '';
  dom.window.navigator.clipboard.writeText = text => { copied = text; return Promise.resolve(); };
  $('#tokenCopy').onclick();
  await tick();
  check('T5 复制拿到完整 token', copied === SECOND_TOKEN, copied);

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
