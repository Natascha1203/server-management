/**
 * 「请求类型 + 回收站」（jsdom，提取 app.js 真实代码执行）：
 * 1. 请求表单不再有「是否上架」「上下架需求」，改为「请求类型」三选一
 * 2. 请求表单注入「接入行情」「接入柜台」两个标签字段，写回 hidden 文本
 * 3. 变更请求的实施表单以 base_data 为底（请求里留空的字段不覆盖现有值）
 * 4. 下架请求不渲染实施表单（实施动作只是确认）
 * 5. 回收站卡片渲染、角标计数、重新上架/永久删除按钮齐备
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const APP_DIR = path.resolve(__dirname, '..');
const src = fs.readFileSync(path.join(APP_DIR, 'app/static/app.js'), 'utf8');
const template = fs.readFileSync(path.join(APP_DIR, 'app/templates/index.html'), 'utf8');

function extractBlock(header) {
  const s = src.indexOf(header);
  if (s < 0) throw new Error('marker not found: ' + header);
  const open = src.indexOf('{', s);
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') {
      depth--;
      if (depth === 0) return src.slice(s, i + 1);
    }
  }
  throw new Error('unbalanced block: ' + header);
}
function extractLine(text) {
  const s = src.indexOf(text);
  if (s < 0) throw new Error('line not found: ' + text);
  const e = src.indexOf('\n', s);
  return src.slice(s, e);
}
let passed = 0;
const failures = [];
function check(name, ok, detail) {
  if (ok) { passed++; return; }
  failures.push(name + (detail ? ' → ' + detail : ''));
}

const html = `<!doctype html><body>
  <div id="rackTagFields"></div>
  <div id="trashList"></div>
  <i id="trashBadge" hidden>0</i>
</body>`;

const dom = new JSDOM(html, { url: 'http://localhost/', runScripts: 'dangerously' });
const { window } = dom;
const $ = s => window.document.querySelector(s);

const code = [
  'const $ = s => document.querySelector(s);',
  'const $$ = s => [...document.querySelectorAll(s)];',
  'var state = { records: [], fieldOptions: {}, trash: [], trashExpanded: [], reviewMode: "implementation", rackApprovalOptions: { rooms: [] } };',
  'var isAdmin = () => true;',
  'var pretty = o => JSON.stringify(o);',
  'const TAG_SPLIT_RE = /[,，、;；\\r\\n]+/;',
  'var MULTILINE_FIELDS = ["防火墙白名单", "备注"];',
  'var DATE_FIELDS = ["上架时间"];',
  extractLine('const TAG_FIELDS'),
  extractLine('const SERVER_APPROVAL_HEADERS'),
  extractLine('const RACK_TAG_FIELDS'),
  extractLine('const TRASH_EXTRA_FILTER_FIELDS'),
  extractBlock('const TRASH_FIELD_HINTS'),
  extractBlock('function esc('),
  extractBlock('function isTagField('),
  extractBlock('function uniqueTags('),
  extractBlock('function splitTags('),
  extractBlock('function joinTags('),
  extractBlock('function tagChipHtml('),
  extractBlock('function tagFieldHtml('),
  extractBlock('function tagsOf('),
  extractBlock('function setTags('),
  extractBlock('function isMultilineField('),
  extractBlock('function isDateField('),
  extractBlock('function toDateInputValue('),
  extractBlock('function textFieldInput('),
  extractBlock('function fieldLabelClass('),
  extractBlock('function customerInput('),
  extractBlock('function todayDateText('),
  extractBlock('function renderRackTagFields('),
  extractBlock('function serverDraftFromRackRequest('),
  extractBlock('function roomCabinetsForRackRequest('),
  extractBlock('function rackApprovalForm('),
  extractBlock('function trashFieldInput('),
  extractBlock('function trashHeaders('),
  extractBlock('function trashDetails('),
  extractBlock('function canPurgeTrash('),
  extractBlock('function canEditTrash('),
  extractBlock('function trashFilterFields('),
  extractBlock('function collectTrashFilters('),
  extractBlock('function trashSearchValue('),
  extractBlock('function filteredTrash('),
  extractBlock('function trashCard('),
  // 回收站表单渲染后会调 bindCustomerInputs；这个用例只关心结构，桩掉即可
  'var bindCustomerInputs = () => {};',
  extractBlock('function bindTrashActions('),
  extractBlock('function renderTrash('),
  extractBlock('function refreshTrashBadge('),
  'window.__api = { renderRackTagFields, serverDraftFromRackRequest, rackApprovalForm, trashCard, renderTrash, setTags, tagsOf };',
].join('\n');
window.eval(code);
const api = window.__api;

// ===== T1: 模板字段 =====
check('T1 模板有请求类型下拉', template.includes('name="请求类型"'));
check('T1 请求类型含上架/下架/变更', /<select name="请求类型"[^>]*>[\s\S]*?上架[\s\S]*?下架[\s\S]*?变更/.test(template));
check('T1 模板不再有是否上架', !template.includes('是否上架'));
check('T1 模板不再有上下架需求输入框', !template.includes('上下架需求'));
check('T1 模板有标签字段容器', template.includes('id="rackTagFields"'));
check('T1 模板有回收站页面', template.includes('id="trash"') && template.includes('data-page="trash"'));

// ===== T2: 请求表单的标签字段 =====
api.renderRackTagFields();
const boxes = [...window.document.querySelectorAll('#rackTagFields [data-tag-field]')];
check('T2 注入两个标签字段', boxes.length === 2, String(boxes.length));
check('T2 字段名是接入行情/接入柜台',
  boxes.map(b => b.dataset.tagField).join(',') === '接入行情,接入柜台',
  boxes.map(b => b.dataset.tagField).join(','));
const marketBox = window.document.querySelector('[data-tag-field="接入行情"]');
api.setTags(marketBox, ['CTP', 'UDP']);
check('T2 标签写回 hidden 文本',
  marketBox.querySelector('input[type=hidden]').value === 'CTP, UDP',
  marketBox.querySelector('input[type=hidden]').value);
check('T2 标签渲染为 chip', marketBox.querySelectorAll('.tag-chip').length === 2);

// ===== T3: 变更请求表单以 base_data 为底 =====
const changeItem = {
  id: 'r1', request_type: '变更', status: 'pending', submitted_at: '2026-11-01 10:00:00',
  request_data: { 序列号: 'SRV-1', 请求类型: '变更', 备注: '' },
  stage_data: {},
  base_data: { 序列号: 'SRV-1', 品牌: 'Dell', 用途: '旧用途', 所属机柜: 'C1', 所属机位: '3-4', 上架时间: '2025-01-02' },
};
const draft = api.serverDraftFromRackRequest(changeItem);
check('T3 base_data 字段保留', draft['品牌'] === 'Dell' && draft['用途'] === '旧用途', JSON.stringify(draft));
check('T3 base_data 机位保留', draft['所属机柜'] === 'C1' && draft['所属机位'] === '3-4', JSON.stringify(draft));
check('T3 上架时间沿用原值', draft['上架时间'] === '2025-01-02', draft['上架时间']);

const formHtml = api.rackApprovalForm(changeItem);
check('T3 变更请求渲染实施表单', formHtml.includes('data-rack-approval-id="r1"'));
check('T3 变更表单按钮文案', formHtml.includes('完成变更并写入正式表'), formHtml.slice(0, 80));
check('T3 变更表单预填原值', formHtml.includes('value="旧用途"'));

// ===== T4: 上架仍走分配，下架不渲染表单 =====
const rackUp = { id: 'r2', request_type: '上架', status: 'pending', submitted_at: '2026-11-01', request_data: {}, stage_data: {}, base_data: {} };
window.eval('state.reviewMode = "allocation"');
check('T4 上架在分配阶段渲染表单', api.rackApprovalForm(rackUp).includes('完成服务器机位IP分配'));
check('T4 上架分配阶段有自动 IP', api.rackApprovalForm(rackUp).includes('data-rack-auto-ip'));
window.eval('state.reviewMode = "implementation"');
const rackDown = { id: 'r3', request_type: '下架', status: 'pending', submitted_at: '2026-11-01', request_data: {}, stage_data: {}, base_data: {} };
check('T4 下架不渲染实施表单', api.rackApprovalForm(rackDown) === '');

// ===== T5: 回收站 =====
window.eval(`state.trash = [
  { id: 't1', serial_number: 'SRV-9', '序列号': 'SRV-9', '客户id': '1', '客户名称': '客户一',
    '所在机房': '机房A', '所属机柜': 'C1', '所属机位': '1-2', '用途分类': '行情交易',
    deleted_at: '2026-11-02 10:00:00', deleted_by: 'admin' }
]; state.trashExpanded = ['t1'];`);
api.renderTrash();
const list = $('#trashList');
check('T5 回收站渲染卡片', list.querySelectorAll('.review-card').length === 1);
check('T5 卡片显示序列号与位置', list.textContent.includes('SRV-9') && list.textContent.includes('机房A / C1 / 1-2'), list.textContent);
check('T5 卡片有恢复与永久删除', list.innerHTML.includes('data-trash-action="restore"') && list.innerHTML.includes('data-trash-action="purge"'));
check('T5 恢复表单预填客户与机位',
  list.querySelector('input[name="客户id"]').value === '1' && list.querySelector('input[name="所属机位"]').value === '1-2');
check('T5 角标显示数量', $('#trashBadge').textContent === '1' && $('#trashBadge').hidden === false);
check('T5 下架前记录含用途分类', list.textContent.includes('行情交易'));

// 机房人员视角：能看、能重新上架，但没有永久删除
window.eval('isAdmin = () => false');
api.renderTrash();
check('T5b 非管理人员不渲染永久删除', !$('#trashList').innerHTML.includes('data-trash-action="purge"'));
check('T5b 非管理人员仍可重新上架', $('#trashList').innerHTML.includes('data-trash-action="restore"'));
check('T5b 提示文案不提永久删除', !$('#trashList').textContent.includes('永久删除'));
window.eval('isAdmin = () => true');

window.eval('state.trash = []');
api.renderTrash();
check('T5 空回收站提示', $('#trashList').textContent.includes('回收站里没有服务器'));
check('T5 空回收站角标隐藏', $('#trashBadge').hidden === true);

console.log('RESULT: ' + passed + ' passed, ' + failures.length + ' failed');
if (failures.length) {
  console.log(failures.map(f => '  FAIL ' + f).join('\n'));
  process.exitCode = 1;
}
