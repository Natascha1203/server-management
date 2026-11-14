/**
 * 服务器「备注」列前端验证（jsdom，提取 app.js 真实代码执行）：
 * 1. SERVERS_HEADERS 契约：servers 共 27 列、末列为「备注」；
 *    SERVER_APPROVAL_HEADERS = servers 27 列 −「负责人」（负责人由客户表同步，不进分配表单）
 * 2. 服务器编辑弹窗（renderServerForm）：渲染出 name="备注" 的输入框并带出当前值
 * 3. 上下架分配/实施表单（rackApprovalForm）：渲染出 name="备注"，
 *    且**默认带出上下架请求里填写的备注**（request_data / stage_data 优先）
 * 4. 「上架时间」为 date 控件：默认取请求提交日期，stage_data 改过的优先
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const APP_DIR = path.resolve(__dirname, '..');
const src = fs.readFileSync(path.join(APP_DIR, 'app/static/app.js'), 'utf8');

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

// ===== T1: 表头契约（镜像 app/core.py ENTITIES.servers.headers） =====
const SERVERS_HEADERS = ['序列号', '品牌', '型号', '客户id', '客户名称', '负责人', '归属单位', 'CPU型号', '网卡', '管理ip', '交易ip', '带外ip', '管理密码', '带外密码', '服务器成本(万)', '用途分类', '用途', '防火墙白名单', '资源申请编号', '是否液冷', '所在机房', '所属机柜', '所属机位', '上架时间', '接入行情', '接入柜台', '备注'];
const approvalHeaders = JSON.parse(
  extractLine('const SERVER_APPROVAL_HEADERS =')
    .replace('const SERVER_APPROVAL_HEADERS =', '').replace(/;\s*$/, '')
    .replace(/'/g, '"')
);
check('T1 servers 表头共 27 列', SERVERS_HEADERS.length === 27);
check('T1 servers 末列为「备注」', SERVERS_HEADERS[26] === '备注');
check('T1 SERVER_APPROVAL_HEADERS 含「备注」', approvalHeaders.includes('备注'));
// 分配表单字段是服务器的子集（顺序按表单分组重排），唯一差异是「负责人」（由客户表同步，不进分配表单）
const approvalSet = [...approvalHeaders].sort();
const serverSet = SERVERS_HEADERS.filter(h => h !== '负责人').sort();
check('T1 SERVER_APPROVAL_HEADERS = servers 27 列 − 负责人',
  JSON.stringify(approvalSet) === JSON.stringify(serverSet),
  '额外:' + JSON.stringify(approvalHeaders.filter(h => !SERVERS_HEADERS.includes(h)))
  + ' 缺失:' + JSON.stringify(serverSet.filter(h => !approvalHeaders.includes(h))));

// ===== 提取真实函数 =====
const code = [
  extractLine('const $ = s => document.querySelector(s);'),
  extractLine('const $$ = s =>'),
  extractBlock('function toast(msg, error = false)'),
  extractBlock('function esc(v)'),
  extractLine('let cellTooltipTimer = null;'),
  extractBlock('function ensureCellTooltip()'),
  extractBlock('function selectionInTooltip(tip)'),
  extractBlock('function scheduleCellTooltipHide()'),
  extractBlock('function showCopyTooltip(anchor, text)'),
  extractBlock('function bindSlotInfoTooltip(btn, info)'),
  extractBlock('function slotNumbers(value)'),
  extractBlock('function slotRangeValue(start, end)'),
  extractBlock('function slotStatusText(slot)'),
  extractBlock('function isSlotSelectable(slot, allowOccupied = false)'),
  extractBlock('function slotInfoText(slot)'),
  extractBlock('function takeoverMode()'),
  extractBlock('function canSwapSlots()'),
  extractBlock('function swapRangeText(range, fallbackSlot)'),
  extractBlock('function requestSlotSwap(slotValue)'),
  extractBlock('function renderSwapNotice()'),
  extractBlock('function chooseSlot(slot)'),
  extractBlock('function customerInput(field, value = \'\', extra = \'\')'),
  extractLine('const MULTILINE_FIELDS ='),
  extractBlock('function isMultilineField(field)'),
  extractLine('const TAG_FIELDS ='),
  extractLine('const TAG_SPLIT_RE ='),
  extractBlock('function isTagField(field)'),
  extractBlock('function uniqueTags(list)'),
  extractBlock('function splitTags(value)'),
  extractBlock('function joinTags(tags)'),
  extractBlock('function tagChipHtml(tag, removable)'),
  extractBlock('function tagFieldHtml(field, value, readonly)'),
  extractLine('const DATE_FIELDS ='),
  extractBlock('function isDateField(field)'),
  extractBlock('function toDateInputValue(value)'),
  extractBlock('function todayDateText()'),
  extractBlock('function textFieldInput(field, value = \'\', extra = \'\')'),
  extractBlock('function fieldLabelClass(field)'),
  extractBlock('function bindCustomerInputs(root = document)'),
  extractBlock('function isKeyReadonlyInForm(field)'),
  extractBlock('function serverDraftFromRackRequest(item)'),
  extractBlock('function roomCabinetsForRackRequest(item)'),
  extractBlock('function rackApprovalForm(item)'),
  extractBlock('function renderServerForm(row)'),
].join('\n\n');

const dom = new JSDOM(`<!DOCTYPE html><html><body>
  <div id="toast"></div>
  <div id="formFields"></div>
  <div id="serverSlotPicker"></div>
  <div id="swapNotice" hidden></div>
  <button id="syncOwnerBtn"></button>
</body></html>`, { runScripts: 'outside-only' });

const rooms = [{
  name: '机房A', cabinets: [{
    name: 'C1',
    slots: [
      { slot: '1', status: 'free', server: '', selectable: true },
      { slot: '2', status: 'occupied', server: 'OTHER-1', selectable: false, owner_range: '2-3' },
      { slot: '3', status: 'occupied', server: 'OTHER-1', selectable: false, owner_range: '2-3' },
    ],
  }],
}];

const serversMeta = { label: '服务器', key: ['序列号'], sheet: '服务器信息', filename: '服务器信息表.xlsx', headers: SERVERS_HEADERS };

const state = {
  entity: 'servers',
  meta: serversMeta,
  editing: null,
  pendingEditing: null,
  cabinetOptions: { rooms },
  customerOptions: [{ 客户id: '1', 客户名称: '客户一', 负责人: '老王' }],
  slotPick: { start: null, end: null },
  swapTarget: null,
  rackApprovalOptions: { rooms },
};

// 用 jsdom 的 Function 构造，注入真实 state（函数体直接执行，末尾 return 导出）
const build = dom.window.Function('state', 'SERVER_APPROVAL_HEADERS', `
  const confirm = () => false;
  const isAdmin = () => true;
  ${code}
  return { state, renderServerForm, rackApprovalForm, serverDraftFromRackRequest, isMultilineField, MULTILINE_FIELDS };
`);
const api = build(state, approvalHeaders);
const $ = s => dom.window.document.querySelector(s);

// ===== T2: 服务器编辑弹窗带出「备注」 =====
api.renderServerForm({
  '序列号': 'SN-1', '客户id': '1', '客户名称': '客户一',
  '所在机房': '机房A', '所属机柜': 'C1', '所属机位': '1-1',
  '防火墙白名单': '10.0.0.0/8\n192.168.1.20',
  '备注': '原厂三年保修，机柜 U 位贴标',
});
const remarkInput = $('#formFields [name="备注"]');
check('T2 服务器表单渲染出 备注 输入框', !!remarkInput);
check('T2 备注 带出当前行数据', remarkInput && remarkInput.value === '原厂三年保修，机柜 U 位贴标',
  remarkInput && remarkInput.value);
const fieldNames = [...$('#formFields').querySelectorAll('input,select,textarea')].map(x => x.name);
check('T2 备注 位于表单字段之中', fieldNames.includes('备注'));
check('T2 表单仍保留既有字段（序列号/所属机位）', fieldNames.includes('序列号') && fieldNames.includes('所属机位'));

// ===== T2b: 备注 / 防火墙白名单 为可调整大小的多行编辑框（textarea） =====
check('T2b 备注 渲染为 textarea', remarkInput && remarkInput.tagName === 'TEXTAREA',
  remarkInput && remarkInput.tagName);
const whitelistInput = $('#formFields [name="防火墙白名单"]');
check('T2b 防火墙白名单 渲染为 textarea', whitelistInput && whitelistInput.tagName === 'TEXTAREA',
  whitelistInput && whitelistInput.tagName);
check('T2b 防火墙白名单 保留换行内容',
  whitelistInput && whitelistInput.value === '10.0.0.0/8\n192.168.1.20',
  whitelistInput && JSON.stringify(whitelistInput.value));
check('T2b 备注 label 独占整行（multiline 类）',
  remarkInput && remarkInput.closest('label').classList.contains('multiline'));
check('T2b 防火墙白名单 label 独占整行（multiline 类）',
  whitelistInput && whitelistInput.closest('label').classList.contains('multiline'));
check('T2b 其余字段仍为单行 input',
  $('#formFields [name="序列号"]')?.tagName === 'INPUT' && $('#formFields [name="型号"]')?.tagName === 'INPUT');
check('T2b 多行字段集合 = [防火墙白名单, 备注]',
  JSON.stringify(api.MULTILINE_FIELDS) === JSON.stringify(['防火墙白名单', '备注']),
  JSON.stringify(api.MULTILINE_FIELDS));

// ===== T3: 上下架分配表单默认带出请求里的备注 =====
const rackItem = {
  id: 'req1', status: 'pending', request_type: '上架',
  request_data: { '序列号': 'SN-2', '客户id': '1', '客户名称': '客户一', '所在机房': '机房A', '备注': '上下架请求备注：双路PDU' },
  stage_data: {},
};
const html = api.rackApprovalForm(rackItem);
const wrap = dom.window.document.createElement('div');
wrap.innerHTML = html;
const rackRemark = wrap.querySelector('[name="备注"]');
check('T3 分配表单渲染出 备注 输入框', !!rackRemark);
check('T3 备注 默认来自上下架请求', rackRemark && rackRemark.value === '上下架请求备注：双路PDU',
  rackRemark && rackRemark.value);
check('T3 分配表单的 备注 为 textarea', rackRemark && rackRemark.tagName === 'TEXTAREA',
  rackRemark && rackRemark.tagName);
check('T3 分配表单的 防火墙白名单 为 textarea',
  wrap.querySelector('[name="防火墙白名单"]')?.tagName === 'TEXTAREA');

// stage_data 已进入具体实施阶段时，以 stage_data 的备注为准（分配阶段可改写）
const rackItem2 = {
  ...rackItem,
  stage_data: { '所属机柜': 'C1', '所属机位': '1-1', '管理ip': '10.1.1.5', '备注': '分配阶段改写：单路' },
};
const wrap2 = dom.window.document.createElement('div');
wrap2.innerHTML = api.rackApprovalForm(rackItem2);
const rackRemark2 = wrap2.querySelector('[name="备注"]');
check('T3 stage_data 的备注优先于 request_data', rackRemark2 && rackRemark2.value === '分配阶段改写：单路',
  rackRemark2 && rackRemark2.value);

check('T3 draft 保留 备注 字段', api.serverDraftFromRackRequest(rackItem)['备注'] === '上下架请求备注：双路PDU');

// ===== T4: 「上架时间」为 date 控件，默认取请求提交日期，分配/实施阶段可改 =====
const rackDated = {
  id: 'req2', status: 'pending', request_type: '上架',
  submitted_at: '2026-10-29 21:40:00',
  request_data: { '序列号': 'SN-3', '客户id': '1', '客户名称': '客户一', '所在机房': '机房A' },
  stage_data: {},
};
const draftDate = api.serverDraftFromRackRequest(rackDated)['上架时间'];
check('T4 draft 上架时间 默认取提交日期', draftDate === '2026-10-29', draftDate);

const wrapDate = dom.window.document.createElement('div');
wrapDate.innerHTML = api.rackApprovalForm(rackDated);
const dateInput = wrapDate.querySelector('[name="上架时间"]');
check('T4 分配表单渲染出 上架时间 输入框', !!dateInput);
check('T4 上架时间 为 date 控件', dateInput && dateInput.type === 'date', dateInput && dateInput.type);
check('T4 上架时间 默认带出提交日期', dateInput && dateInput.value === '2026-10-29', dateInput && dateInput.value);

const rackDated2 = {
  ...rackDated,
  stage_data: { '所属机柜': 'C1', '所属机位': '1-1', '上架时间': '2026/9/1' },
};
check('T4 stage_data 的上架时间优先且归一为 ISO',
  api.serverDraftFromRackRequest(rackDated2)['上架时间'] === '2026-09-01',
  api.serverDraftFromRackRequest(rackDated2)['上架时间']);
const wrapDate2 = dom.window.document.createElement('div');
wrapDate2.innerHTML = api.rackApprovalForm(rackDated2);
check('T4 表单里可编辑的上架时间取 stage_data 值',
  wrapDate2.querySelector('[name="上架时间"]')?.value === '2026-09-01',
  wrapDate2.querySelector('[name="上架时间"]')?.value);

// 服务器编辑弹窗同样渲染 date 控件，可手工改
api.renderServerForm({ '序列号': 'SN-4', '客户id': '1', '所在机房': '机房A', '所属机柜': 'C1', '所属机位': '1-1', '上架时间': '2026-09-02' });
const formDate = $('#formFields [name="上架时间"]');
check('T4 服务器编辑表单也渲染 上架时间 date 控件',
  formDate && formDate.type === 'date' && formDate.value === '2026-09-02',
  formDate && formDate.type + ':' + formDate.value);

console.log('RESULT: ' + passed + ' passed, ' + failures.length + ' failed');
if (failures.length) {
  console.log(failures.map(f => '  FAIL ' + f).join('\n'));
  process.exitCode = 1;
}
