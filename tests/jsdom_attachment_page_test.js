/**
 * 页面级集成验证（jsdom 加载真实 index.html + 真实 app.js，fetch 走桩数据）：
 * 1. 上下架请求页：请求卡片里出现「附件资料」，列出真实接口返回的资料，并有上传入口
 * 2. 审核管理（具体实施）：审核卡片里同样能取到资料，并可重命名 / 删除
 * 3. 信息维护（服务器信息）：操作列出现「资料 N」按钮，点击弹出资料弹窗
 * 4. 三处看到的都是同一份按序列号归属的资料
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

const ATTACHMENTS = [
  { id: 'a1', serial_number: 'SN-1', filename: '验收单.pdf', size: 2048, uploader: 'admin', uploaded_at: '2026-10-30 10:00:00', request_id: 'req1', source: 'rack-request', can_manage: true },
  { id: 'a2', serial_number: 'SN-1', filename: '接线图.png', size: 1048576, uploader: 'room1', uploaded_at: '2026-10-30 11:00:00', request_id: 'req1', source: 'implementation', can_manage: true },
];

const RACK_REQUEST = {
  id: 'req1', request_type: '上架', status: 'pending', stage: 'implementation',
  '序列号': 'SN-1', '所在机房': '机房A', '客户id': '1', '客户名称': '客户一',
  request_data: { '序列号': 'SN-1', '所在机房': '机房A', '客户id': '1', '客户名称': '客户一', '是否重装系统': '是', '备注': '双路电源' },
  stage_data: { '所在机房': '机房A', '所属机柜': 'C1', '所属机位': '3-4', '管理ip': '10.1.1.5' },
  submitted_at: '2026-10-30 09:00:00', reject_reason: '',
};

const fixtures = [
  [/^\/api\/attachments\/summary/, () => ({ counts: { 'SN-1': ATTACHMENTS.length } })],
  [/^\/api\/attachments\?serials=/, () => ({ items: ATTACHMENTS })],
  [/^\/api\/rack-requests\?/, () => ({ requests: [RACK_REQUEST] })],
  [/^\/api\/customer-options/, () => ({ customers: [{ '客户id': '1', '客户名称': '客户一', '负责人': '老王' }] })],
  [/^\/api\/cabinet-options/, () => ({ rooms: [{ name: '机房A', cabinets: [{ name: 'C1', slots: [{ slot: '3', status: 'free', server: '', selectable: true }] }] }] })],
  [/^\/api\/changes\?/, () => ({ changes: [] })],
  [/^\/api\/servers/, () => ({
    records: [{ '序列号': 'SN-1', '客户id': '1', '客户名称': '客户一', '所在机房': '机房A', '所属机柜': 'C1', '所属机位': '3-4' }],
    meta: { label: '服务器', key: ['序列号'], sheet: '服务器信息', headers: ['序列号', '客户id', '客户名称', '所在机房', '所属机柜', '所属机位'] },
    record_state: 'official',
  })],
  [/^\/api\/cabinets/, () => ({
    records: [{ '机房': '机房A', '机柜': 'C1', '机位': '1-10' }],
    meta: { label: '机柜', key: ['机房', '机柜'], sheet: '机柜信息表', headers: ['机房', '机柜', '机位'] },
    record_state: 'official',
  })],
];

const requests = [];
function fakeFetch(url) {
  requests.push(String(url));
  const hit = fixtures.find(([pattern]) => pattern.test(String(url)));
  const payload = hit ? hit[1]() : {};
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
    window.fetch = (url) => fakeFetch(url);
    window.alert = () => {};
    window.confirm = () => true;
    window.prompt = () => '改名后.txt';
  },
});
dom.window.eval(appJs);

const $ = s => dom.window.document.querySelector(s);
const $$ = s => [...dom.window.document.querySelectorAll(s)];
const tick = (ms = 20) => new Promise(r => setTimeout(r, ms));

async function main() {
  await tick();

  // ===== T1: 上下架请求页 =====
  $$('.nav-item').find(b => b.dataset.page === 'rackRequests').onclick();
  await tick();
  const requestCard = $('#rackRequestList .rack-request-card');
  check('T1 渲染出上下架请求卡片', !!requestCard);
  const requestBlock = requestCard?.querySelector('[data-attachment-block]');
  check('T1 请求卡片里有附件资料区块', !!requestBlock);
  check('T1 资料区块绑定序列号', requestBlock?.dataset.serial === 'SN-1', requestBlock?.dataset.serial);
  check('T1 资料区块带请求 id', requestBlock?.dataset.requestId === 'req1', requestBlock?.dataset.requestId);
  const names = [...(requestBlock?.querySelectorAll('.attachment-name') || [])].map(a => a.textContent);
  check('T1 列出两个资料', names.length === 2 && names.includes('验收单.pdf') && names.includes('接线图.png'), names.join(','));
  check('T1 有上传入口', !!requestBlock?.querySelector('[data-attachment-upload]'));
  check('T1 有重命名 / 删除按钮',
    !!requestBlock?.querySelector('[data-attachment-rename]') && !!requestBlock?.querySelector('[data-attachment-delete]'));
  check('T1 请求已按接口加载资料',
    requests.some(u => u.startsWith('/api/attachments?serials=')), requests.slice(0, 8).join(' | '));

  // ===== T2: 审核管理 - 具体实施 =====
  $$('.nav-sub-item').find(b => b.dataset.navReviewMode === 'implementation').onclick();
  await tick();
  const reviewCard = $('#reviewList .rack-request-card');
  check('T2 渲染出具体实施审核卡片', !!reviewCard);
  const reviewBlock = reviewCard?.querySelector('[data-attachment-block]');
  check('T2 审核卡片里能取到资料', !!reviewBlock);
  check('T2 资料与请求页是同一份', reviewBlock?.dataset.serial === 'SN-1');
  check('T2 审核页资料带刷新上下文 rackReview', reviewBlock?.dataset.reload === 'rackReview',
    reviewBlock?.dataset.reload);
  check('T2 审核页两个资料都在',
    (reviewBlock?.querySelectorAll('.attachment-item') || []).length === 2);

  // ===== T3: 服务器信息表「资料」按钮 + 弹窗 =====
  $$('.entity-tab').find(b => b.dataset.entity === 'servers').onclick();
  await tick();
  const attachBtn = $('#dataBody .row-actions .mini.attach');
  check('T3 服务器表操作列有资料按钮', !!attachBtn);
  check('T3 按钮带资料数量', attachBtn?.textContent.trim() === '资料 2', attachBtn?.textContent.trim());
  attachBtn.onclick();
  await tick();
  check('T3 资料弹窗已打开', $('#attachmentModal').classList.contains('open'));
  check('T3 弹窗标题带序列号', $('#attachmentModalTitle').textContent.includes('SN-1'),
    $('#attachmentModalTitle').textContent);
  const modalNames = [...$('#attachmentModalBody').querySelectorAll('.attachment-name')].map(a => a.textContent);
  check('T3 弹窗里是同一份资料', modalNames.length === 2, modalNames.join(','));

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
