/**
 * 回收站页面级验证（jsdom 加载真实 index.html + 真实 app.js，fetch 走桩数据）：
 * 1. 页面顶部有「手动加入回收站」表单（序列号必填）
 * 2. 提交表单 → POST /api/trash，body 带序列号，返回后刷新列表
 * 3. 手动登记记录（origin=manual）在卡片上带「手动登记」标签，展开后提示补齐信息
 * 4. 下架/删除快照仍显示「删除时记录」与来源
 * 5. 重新上架表单覆盖服务器整行字段（含序列号），提交时把改动一起发回后端
 * 6. 「保存修改」只 PATCH 回收站记录（不上架），保存后仍留在列表且保持展开
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

const TRASH_ITEMS = [
  {
    id: 't-manual', serial_number: 'SN-MANUAL', origin: 'manual',
    reason: '手动登记（信息待补齐）', deleted_at: '2026-11-13 11:20:00', deleted_by: 'admin',
    request_id: '', '序列号': 'SN-MANUAL', 客户id: '', 所在机房: '', 所属机柜: '', 所属机位: '',
  },
  {
    id: 't-down', serial_number: 'SN-DOWN', origin: '',
    reason: '下架实施完成', deleted_at: '2026-11-13 10:00:00', deleted_by: 'admin',
    request_id: 'req1', '序列号': 'SN-DOWN', 客户id: '1', 客户名称: '客户一',
    所在机房: '机房A', 所属机柜: 'C1', 所属机位: '3-4', 品牌: 'Dell',
  },
];

const requests = [];
function fakeFetch(url, opt = {}) {
  requests.push({ url: String(url), method: (opt.method || 'GET').toUpperCase(), body: opt.body || '' });
  if (String(url).includes('/restore')) {
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ message: '序列号 SN-DOWN 已重新上架', record: {} }) });
  }
  if (String(url) === '/api/trash/t-down' && (opt.method || '').toUpperCase() === 'PATCH') {
    return Promise.resolve({
      ok: true,
      json: () => Promise.resolve({ message: '序列号 SN-DOWN 的信息已保存（仍在回收站）', record: {} }),
    });
  }
  if (String(url) === '/api/trash' && (opt.method || '').toUpperCase() === 'POST') {
    return Promise.resolve({
      ok: true,
      json: () => Promise.resolve({ message: '序列号 SN-NEW 已加入回收站，重新上架时再补齐信息', id: 't-new' }),
    });
  }
  if (String(url) === '/api/trash') return Promise.resolve({
    ok: true,
    // 不给 headers：前端回退到 SERVER_APPROVAL_HEADERS（服务器全部字段），与原测试预期一致
    json: () => Promise.resolve({ items: TRASH_ITEMS, can_manage: true, can_edit: true }),
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

  // ===== T1: 手动登记表单 =====
  check('T1 回收站页有手动登记表单', !!$('#trashAddForm'));
  check('T1 表单有序列号输入框', !!$('#trashAddSerial'));
  check('T1 输入框提示必填', ($('#trashAddSerial')?.placeholder || '').includes('必填'),
    $('#trashAddSerial')?.placeholder);

  // ===== T2: 列表区分手动登记与快照 =====
  const cards = $$('#trashList .review-card');
  check('T2 渲染两条回收站记录', cards.length === 2, String(cards.length));
  const manualCard = cards.find(c => c.textContent.includes('SN-MANUAL'));
  const downCard = cards.find(c => c.textContent.includes('SN-DOWN'));
  check('T2 手动登记卡片带标签', !!manualCard?.querySelector('.tag') &&
    manualCard.querySelector('.tag').textContent.trim() === '手动登记',
    manualCard?.querySelector('.tag')?.textContent);
  manualCard.querySelector('[data-trash-expand-id]').onclick();
  await tick();
  check('T2 手动登记卡片展开后提示补齐信息',
    manualCard.textContent.includes('补齐'), manualCard.textContent.slice(0, 80));
  check('T2 手动登记卡片恢复表单为空',
    (manualCard.querySelector('input[name="客户id"]')?.value || '') === '');
  check('T2 普通快照卡片显示删除时记录', downCard.textContent.includes('删除时记录'));
  check('T2 普通快照卡片显示来源', downCard.textContent.includes('下架实施完成'));
  check('T2 普通快照卡片预填客户id',
    downCard.querySelector('input[name="客户id"]')?.value === '1',
    downCard.querySelector('input[name="客户id"]')?.value);

  // ===== T3: 提交手动登记 =====
  const form = $('#trashAddForm');
  $('#trashAddSerial').value = 'SN-NEW';
  form.dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true }));
  await tick(40);
  const post = requests.find(r => r.url === '/api/trash' && r.method === 'POST');
  check('T3 提交调用了 POST /api/trash', !!post);
  check('T3 Body 带序列号', (post?.body || '').includes('SN-NEW'), post?.body);
  check('T3 提交后清空输入框', $('#trashAddSerial').value === '', $('#trashAddSerial').value);
  const gets = requests.filter(r => r.url === '/api/trash' && r.method === 'GET');
  check('T3 提交后刷新了列表', gets.length >= 2, String(gets.length));

  // ===== T4: 快照记录的重新上架表单覆盖整行字段 =====
  downCard.querySelector('[data-trash-expand-id]').onclick();
  await tick();
  const editable = ['序列号', '品牌', '型号', 'CPU型号', '网卡', '服务器成本(万)', '归属单位',
    '用途分类', '用途', '防火墙白名单', '资源申请编号', '是否液冷', '客户id', '客户名称',
    '所在机房', '所属机柜', '所属机位', '管理ip', '交易ip', '带外ip', '管理密码', '带外密码',
    '上架时间', '接入行情', '接入柜台', '备注'];
  const missing = editable.filter(field => !downCard.querySelector(`[data-trash-restore] [name="${field}"]`));
  check('T4 表单覆盖服务器表全部字段', missing.length === 0, missing.join('、'));
  check('T4 品牌预填快照值', downCard.querySelector('[name="品牌"]')?.value === 'Dell',
    downCard.querySelector('[name="品牌"]')?.value);
  check('T4 序列号可编辑', !downCard.querySelector('[name="序列号"]')?.readOnly);
  check('T4 备注是多行输入', downCard.querySelector('[name="备注"]')?.tagName === 'TEXTAREA',
    downCard.querySelector('[name="备注"]')?.tagName);
  check('T4 删除时记录默认收起',
    downCard.querySelector('details.diff') && !downCard.querySelector('details.diff').open);
  check('T4 卡片有「保存修改」按钮', !!downCard.querySelector('[data-trash-action="save"]'));

  // 改序列号 + 品牌 + 备注后先「保存修改」（不上架）
  downCard.querySelector('[name="序列号"]').value = 'SN-DOWN-NEW';
  downCard.querySelector('[name="品牌"]').value = 'HPE';
  downCard.querySelector('[name="备注"]').value = '等客户确认后再上架';
  downCard.querySelector('[data-trash-action="save"]').click();
  await tick(40);

  // ===== T5: 保存修改 =====
  const saveReq = requests.find(r => r.url === '/api/trash/t-down' && r.method === 'PATCH');
  check('T5 保存修改调用 PATCH /api/trash/<id>', !!saveReq, requests.map(r => r.method + ' ' + r.url).join(' | '));
  const saved = saveReq ? JSON.parse(saveReq.body).server_data : {};
  check('T5 请求体带新序列号', saved['序列号'] === 'SN-DOWN-NEW', saved['序列号']);
  check('T5 请求体带改后的品牌', saved['品牌'] === 'HPE', saved['品牌']);
  check('T5 请求体带多行备注', saved['备注'] === '等客户确认后再上架', saved['备注']);
  check('T5 保存不触发重新上架', !requests.some(r => r.url.includes('/restore')));
  check('T5 保存后记录仍在回收站',
    $$('#trashList .review-card').length === 2, String($$('#trashList .review-card').length));
  const keptCard = $$('#trashList .review-card').find(c => c.textContent.includes('SN-DOWN'));
  check('T5 保存后卡片保持展开',
    !!keptCard && !keptCard.querySelector('.rack-request-body').hidden);

  // ===== T6: 重新上架（保存之后仍然可以走这条路）=====
  keptCard.querySelector('[name="品牌"]').value = 'Lenovo';
  keptCard.querySelector('[data-trash-action="restore"]').click();
  await tick(40);
  const restoreReq = requests.find(r => r.url === '/api/trash/t-down/restore' && r.method === 'POST');
  check('T6 重新上架调用 restore 接口', !!restoreReq, restoreReq?.url);
  const sent = restoreReq ? JSON.parse(restoreReq.body).server_data : {};
  check('T6 请求体带改后的品牌', sent['品牌'] === 'Lenovo', sent['品牌']);

  // ===== T7: 按属性筛选（字段下拉 + 子串，多条件「与」）=====
  check('T7 有筛选面板', !!$('#trashSearchPanel') && !!$('#trashFilterRows'));
  check('T7 默认渲染一行筛选条件', $$('#trashFilterRows .trash-filter-row').length === 1,
    String($$('#trashFilterRows .trash-filter-row').length));
  // 初始一行「序列号」+ 关键词 SN-DOWN → 只命中一条
  let row = $('#trashFilterRows .trash-filter-row');
  row.querySelector('[data-combo]').dataset.value = '序列号';
  row.querySelector('[data-pattern]').value = 'SN-DOWN';
  $('#applyTrashFilterBtn').onclick();
  await tick();
  check('T7 序列号子串筛选命中 1 条', $$('#trashList .review-card').length === 1,
    String($$('#trashList .review-card').length));
  check('T7 命中正确的记录', $('#trashList').textContent.includes('SN-DOWN'));

  // 加第二行「所在机房 = 机房Z」→ 与条件冲突，0 条
  $('#addTrashFilterBtn').onclick();
  await tick();
  check('T7 添加条件后有两行', $$('#trashFilterRows .trash-filter-row').length === 2,
    String($$('#trashFilterRows .trash-filter-row').length));
  row = $$('#trashFilterRows .trash-filter-row')[1];
  row.querySelector('[data-combo]').dataset.value = '所在机房';
  row.querySelector('[data-pattern]').value = '机房Z';
  $('#applyTrashFilterBtn').onclick();
  await tick();
  check('T7 多条件与冲突时 0 条', $$('#trashList .review-card').length === 0,
    String($$('#trashList .review-card').length));
  check('T7 空结果显示提示文案', $('#trashList').textContent.includes('没有服务器'));

  // 清空 → 恢复全部 2 条
  $('#clearTrashFilterBtn').onclick();
  await tick();
  check('T7 清空后恢复全部记录', $$('#trashList .review-card').length === 2,
    String($$('#trashList .review-card').length));
  check('T7 清空后回到一行空条件', $$('#trashFilterRows .trash-filter-row').length === 1);

  // 删除元信息筛选：删除人 = admin → 两条都命中
  row = $('#trashFilterRows .trash-filter-row');
  row.querySelector('[data-combo]').dataset.value = '删除人';
  row.querySelector('[data-pattern]').value = 'admin';
  $('#applyTrashFilterBtn').onclick();
  await tick();
  check('T7 删除人元信息字段可筛选', $$('#trashList .review-card').length === 2,
    String($$('#trashList .review-card').length));
  $('#clearTrashFilterBtn').onclick();
  await tick();

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
