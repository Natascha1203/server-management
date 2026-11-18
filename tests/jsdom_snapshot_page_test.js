/**
 * 设置页「数据恢复」页面级验证（jsdom 加载真实 index.html + 真实 app.js，fetch 走桩）：
 * 1. 设置有「快照目录」输入框，保存时带上 snapshot_dir
 * 2. 列出快照：区分定时备份包 / 手动备份 / 旧版备份，标注是否含 app.db
 * 3. 不完整的快照标为不可恢复，且不给恢复按钮
 * 4. 恢复前先调 preview 取行数，确认框要求输入 RESTORE
 * 5. 输入正确后按选项提交 include_db / drop_pending
 * 6. 每张卡片都有删除按钮，确认后 DELETE /api/snapshots 带 snapshot 名
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
  .replace('{{ capabilities|tojson }}', JSON.stringify([
    'manage_all', 'review_operations', 'review_rack_requests', 'submit_rack_request',
    'manage_settings', 'view_statistics', 'view_trash',
  ]))
  .replace('{{ username[0]|upper }}', 'A')
  .replace('{{ username }}', 'admin')
  .replace('{{ role_name }}', '管理人员')
  .replace(/\{\{[^}]*\}\}/g, '');

const SNAPSHOTS = [
  {
    name: 'data_20261103.zip', kind: 'zip', origin: 'cron', size: 38690,
    mtime: '2026-11-03 17:00:02', has_db: true, has_users: true,
    valid: true, errors: [], record_counts: {},
  },
  {
    name: 'manual/B20261118-120000-0001', kind: 'dir', origin: 'manual', size: 200704,
    mtime: '2026-11-18 12:00:00', has_db: true, has_users: true,
    valid: true, errors: [], record_counts: {},
  },
  {
    name: 'auto/B20261118-130000-0001', kind: 'dir', origin: 'auto', size: 200704,
    mtime: '2026-11-18 13:00:00', has_db: true, has_users: false,
    valid: true, errors: [], record_counts: {},
  },
  {
    name: 'data_20260101.zip', kind: 'zip', origin: 'cron', size: 1024,
    mtime: '2026-01-01 17:00:00', has_db: false, has_users: false,
    valid: false, errors: ['缺少服务器表'], record_counts: {},
  },
];

const requests = [];
let promptAnswer = 'RESTORE';

function fakeFetch(url, opt = {}) {
  const method = (opt.method || 'GET').toUpperCase();
  requests.push({ url: String(url), method, body: opt.body || '' });
  const ok = body => Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
  if (String(url) === '/api/settings' && method === 'GET') {
    return ok({ settings: {
      backup_keep_count: 5, backup_command: '', audit_keep_count: 200,
      notify_enabled: 0, notify_webhook_url: '', notify_message_type: 'markdown',
      snapshot_dir: '/home/cyn/backup/server-management-zips', auto_backup_keep: 20,
    } });
  }
  if (String(url) === '/api/settings' && method === 'PUT') return ok({ message: '设置已保存', settings: {} });
  if (String(url) === '/api/snapshots' && method === 'DELETE') {
    return ok({ message: '已删除 x，剩余 2 份备份', snapshot: 'x', kind: 'zip', remaining: 2 });
  }
  if (String(url) === '/api/snapshots') {
    return ok({
      root: '/home/cyn/backup/server-management-zips',
      exists: true, scan_error: '', items: SNAPSHOTS,
      manual_dir: '/home/cyn/backup/server-management-zips/manual',
      auto_dir: '/home/cyn/backup/server-management-zips/auto',
      last_backup_at: '2026-11-18 12:00:00', last_restore_at: '', auto_backup_keep: 20,
    });
  }
  if (String(url) === '/api/snapshots/preview') {
    return ok({ snapshot: {
      name: 'manual/B20261118-120000-0001', restorable: true, has_db: true,
      missing_columns: {},
      tables: [
        { entity: 'customers', label: '客户表', records: 13 },
        { entity: 'cabinets', label: '机柜表', records: 12 },
        { entity: 'servers', label: '服务器表', records: 40 },
      ],
    } });
  }
  if (String(url) === '/api/snapshots/restore') {
    return ok({ message: '已恢复到快照 x', snapshot: 'x', backup_before_restore: 'B20261118-121000-0001' });
  }
  if (String(url) === '/api/system-users') return ok({ users: [] });
  if (String(url).startsWith('/api/field-options')) return ok({ options: {} });
  if (String(url).startsWith('/api/')) return ok({ records: [], meta: { label: '表', headers: [] }, changes: [], requests: [] });
  return ok({});
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
    window.prompt = () => promptAnswer;
  },
});
dom.window.eval(appJs);

const $ = s => dom.window.document.querySelector(s);
const $$ = s => [...dom.window.document.querySelectorAll(s)];
const tick = (ms = 30) => new Promise(r => setTimeout(r, ms));

async function main() {
  await tick();

  const settingsNav = $$('.nav-item').find(b => b.dataset.page === 'settings');
  settingsNav.onclick();
  await tick(60);

  // ===== T1: 快照目录输入框 =====
  check('T1 有快照目录输入框', !!$('#snapshotDir'));
  check('T1 回填当前快照目录', $('#snapshotDir')?.value === '/home/cyn/backup/server-management-zips',
    $('#snapshotDir')?.value);

  // ===== T2: 列表渲染 =====
  const cards = $$('#snapshotList .snapshot-card');
  check('T2 渲染四条快照', cards.length === 4, String(cards.length));
  const zipCard = cards.find(c => c.textContent.includes('data_20261103.zip'));
  const manualCard = cards.find(c => c.textContent.includes('manual/B20261118'));
  const autoCard = cards.find(c => c.textContent.includes('auto/B20261118'));
  const badCard = cards.find(c => c.textContent.includes('data_20260101.zip'));
  check('T2 识别定时备份包', zipCard.textContent.includes('定时备份包'));
  check('T2 识别手动备份', manualCard.textContent.includes('手动备份'));
  check('T2 识别写前自动备份', autoCard.textContent.includes('写前自动备份'), autoCard.textContent.slice(0, 90));
  check('T2 标注含 app.db', zipCard.textContent.includes('含 app.db'));
  check('T2 不完整快照标为不可恢复', badCard.textContent.includes('不可恢复'));
  check('T2 不完整快照显示原因', badCard.textContent.includes('缺少服务器表'));
  check('T2 可用快照有恢复按钮', !!manualCard.querySelector('[data-snapshot-restore]'));
  check('T2 每张卡片都有删除按钮', cards.every(c => !!c.querySelector('[data-snapshot-delete]')));
  check('T2 提示里带自动备份目录', ($('#snapshotHint')?.textContent || '').includes('/auto'),
    $('#snapshotHint')?.textContent);

  // ===== T3: 保存设置带 snapshot_dir =====
  $('#snapshotDir').value = '/data/backups';
  $('#settingsForm').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true }));
  await tick(40);
  const put = requests.find(r => r.url === '/api/settings' && r.method === 'PUT');
  check('T3 保存设置调用了 PUT /api/settings', !!put);
  check('T3 请求体带 snapshot_dir', put && put.body.includes('snapshot_dir'), put && put.body.slice(0, 120));

  // ===== T4: 恢复需要 preview + 输入 RESTORE =====
  promptAnswer = 'nope';
  manualCard.querySelector('[data-snapshot-restore]').click();
  await tick(60);
  check('T4 恢复前调用了 preview', requests.some(r => r.url === '/api/snapshots/preview'));
  check('T4 未输入 RESTORE 不会提交恢复', !requests.some(r => r.url === '/api/snapshots/restore'));

  // ===== T5: 输入正确后提交，带上 include_db / drop_pending =====
  promptAnswer = 'restore';
  $('#snapshotIncludeDb').checked = true;
  $('#snapshotDropPending').checked = true;
  manualCard.querySelector('[data-snapshot-restore]').click();
  await tick(60);
  const restoreReq = requests.find(r => r.url === '/api/snapshots/restore');
  check('T5 提交了恢复请求', !!restoreReq);
  const sent = restoreReq ? JSON.parse(restoreReq.body) : {};
  check('T5 请求体带 snapshot 名', sent.snapshot === 'manual/B20261118-120000-0001', sent.snapshot);
  check('T5 请求体带 confirm', sent.confirm === 'RESTORE', sent.confirm);
  check('T5 请求体带 include_db', sent.include_db === true, String(sent.include_db));
  check('T5 请求体带 drop_pending', sent.drop_pending === true, String(sent.drop_pending));
  check('T5 恢复后重新扫描列表', requests.filter(r => r.url === '/api/snapshots').length >= 2,
    String(requests.filter(r => r.url === '/api/snapshots').length));

  // ===== T6: 删除备份包 =====
  check('T6 删除按钮带名字', zipCard.querySelector('[data-snapshot-delete]')?.dataset.snapshotDelete === 'data_20261103.zip',
    zipCard.querySelector('[data-snapshot-delete]')?.dataset.snapshotDelete);
  zipCard.querySelector('[data-snapshot-delete]').click();
  await tick(60);
  const delReq = requests.find(r => r.url === '/api/snapshots' && r.method === 'DELETE');
  check('T6 删除调用 DELETE /api/snapshots', !!delReq);
  const delBody = delReq ? JSON.parse(delReq.body) : {};
  check('T6 请求体带 snapshot 名', delBody.snapshot === 'data_20261103.zip', delBody.snapshot);
  check('T6 删除后重新扫描', requests.filter(r => r.url === '/api/snapshots' && r.method === 'GET').length >= 3,
    String(requests.filter(r => r.url === '/api/snapshots' && r.method === 'GET').length));

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
