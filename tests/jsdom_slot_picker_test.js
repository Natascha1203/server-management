/**
 * 前端机位选择器修复验证（jsdom，提取 app.js 真实代码执行）：
 * 1. 上架审核表单（renderRackSlotPicker）：本请求已分配机位显示为绿色空闲可选，
 *    其他待实施请求的机位仍锁定并显示待实施占用
 * 2. 锁定机位悬停显示可复制 tooltip（替代原生 title），Esc/移出可关闭
 * 3. 服务器表单（renderSlotPicker）：锁定机位 class 锁 + tooltip，可选机位可点击
 * 4. style.css 中 .slot-choice.disabled 具备锁定样式
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const APP_DIR = path.resolve(__dirname, '..');
const src = fs.readFileSync(path.join(APP_DIR, 'app/static/app.js'), 'utf8');
const css = fs.readFileSync(path.join(APP_DIR, 'app/static/style.css'), 'utf8');

// ---- 从 app.js 提取函数块（括号配对，保证测试的是真实代码） ----
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
  extractBlock('function slotSelectableForRequest(slot, requestId)'),
  extractBlock('function slotInfoText(slot)'),
  extractBlock('function rackApprovalFormById(id)'),
  extractBlock('function roomCabinetsForRackRequest(item)'),
  extractBlock('function renderRackSlotPicker(id)'),
  extractBlock('function rackSelectedTakeoverOwners(id)'),
  extractBlock('function renderRackTakeoverNotice(id)'),
  extractBlock('function chooseRackSlot(id, slot)'),
  extractBlock('function takeoverMode()'),
  extractBlock('function selectedTakeoverOwners()'),
  extractBlock('function renderTakeoverNotice()'),
  extractBlock('function renderSlotPicker()'),
  extractBlock('function canSwapSlots()'),
  extractBlock('function swapRangeText(range, fallbackSlot)'),
  extractBlock('function requestSlotSwap(slotValue)'),
  extractBlock('function renderSwapNotice()'),
  extractBlock('function chooseSlot(slot)'),
].join('\n\n');

// ---- 测试 DOM ----
const dom = new JSDOM(`<!DOCTYPE html><html><body>
  <div id="toast"></div>
  <form class="rack-approval-form" data-rack-approval-id="reqA">
    <input name="所属机柜" data-rack-cabinet value="C1">
    <input name="所属机位" data-rack-slot-input value="3-4">
    <div data-rack-slot-picker></div>
    <div class="takeover-notice" data-rack-takeover-notice hidden></div>
  </form>
  <select id="serverRoomSelect"><option value="机房A" selected>机房A</option></select>
  <select id="serverCabinetSelect"><option value="C1" selected>C1</option></select>
  <input id="serverSlotInput">
  <input type="checkbox" id="takeoverToggle">
  <div id="formFields"><input name="序列号" value=""></div>
  <div id="takeoverNotice" class="takeover-notice" hidden></div>
  <div id="swapNotice" class="swap-notice" hidden></div>
  <div id="serverSlotPicker"></div>
</body></html>`);
global.document = dom.window.document;
global.window = dom.window;

const state = {
  rackRequests: [{ id: 'reqA', stage_data: { '所在机房': '机房A', '所属机柜': 'C1', '所属机位': '3-4' } }],
  rackApprovalOptions: { rooms: [{ name: '机房A', cabinets: [{ name: 'C1', slots: [
    { slot: '1', status: 'free', server: '', selectable: true },
    { slot: '2', status: 'pending_implementation', server: 'SRV-B', selectable: false, request_id: 'reqB' },
    { slot: '3', status: 'pending_implementation', server: 'SRV-A', selectable: false, request_id: 'reqA' },
    { slot: '4', status: 'pending_implementation', server: 'SRV-A', selectable: false, request_id: 'reqA' },
    { slot: '5', status: 'free', server: '', selectable: true },
  ] }] }] },
  rackSlotPick: {},
  slotPick: { start: null, end: null },
};
const serverRooms = [{ name: '机房A', cabinets: [{ name: 'C1', slots: [
  { slot: '1', status: 'occupied', server: 'S1', selectable: false, owner_range: '1-1' },
  { slot: '2', status: 'free', server: '', selectable: true, owner_range: '' },
  { slot: '6', status: 'occupied', server: 'S2', selectable: false, owner_range: '6-8' },
  { slot: '7', status: 'occupied', server: 'S2', selectable: false, owner_range: '6-8' },
  { slot: '8', status: 'occupied', server: 'S2', selectable: false, owner_range: '6-8' },
] }] }];

const factory = new Function(`
  const state = ${JSON.stringify(state)};
  const rooms = ${JSON.stringify(serverRooms)};
  const roomSelect = document.querySelector('#serverRoomSelect');
  const cabinetSelect = document.querySelector('#serverCabinetSelect');
  ${code}
  return { state, renderRackSlotPicker, chooseRackSlot, renderSlotPicker, requestSlotSwap, renderSwapNotice, chooseSlot, renderTakeoverNotice, renderRackTakeoverNotice };
`);
const api = factory();
const $$t = s => [...document.querySelectorAll(s)];

let passed = 0, failed = 0;
function check(name, cond) {
  if (cond) { passed++; console.log('PASS ' + name); }
  else { failed++; console.log('FAIL ' + name); }
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  // ===== T1/T2: 上架审核表单（具体实施阶段）=====
  api.renderRackSlotPicker('reqA');
  const rackBtn = slotNum => document.querySelector(`[data-rack-slot-picker] [data-rack-slot="${slotNum}"]`);
  const own3 = rackBtn('3'), other2 = rackBtn('2'), free5 = rackBtn('5');

  check('T1 本请求机位3：无 disabled class（绿色可选）', !own3.classList.contains('disabled') && !own3.hasAttribute('disabled'));
  check('T1 本请求机位3：显示为空闲', own3.querySelector('span').textContent === '空闲');
  check('T1 本请求机位3：aria-disabled=false', own3.getAttribute('aria-disabled') === 'false');
  check('T1 本请求机位3：已绑定点击事件', typeof own3.onclick === 'function');
  check('T1 本请求机位4：同样绿色可选', !rackBtn('4').classList.contains('disabled') && rackBtn('4').querySelector('span').textContent === '空闲');

  check('T2 他人机位2：disabled class 锁定', other2.classList.contains('disabled') && !other2.hasAttribute('disabled'));
  check('T2 他人机位2：显示待实施占用', other2.querySelector('span').textContent === '待实施占用');
  check('T2 他人机位2：未绑定点击事件', other2.onclick === null);
  check('T2 他人机位2：data-slot-info 携带序列号', other2.getAttribute('data-slot-info') === '待实施占用：SRV-B');
  check('T2 他人机位2：已移除原生 title（不可复制的提示）', other2.getAttribute('title') === null);

  // ===== T3: 本请求机位可点击选回自己 =====
  own3.onclick();
  check('T3 点击机位3：pick.start=3', api.state.rackSlotPick.reqA && api.state.rackSlotPick.reqA.start === 3);
  check('T3 点击机位3：输入框=3-3', document.querySelector('[data-rack-slot-input]').value === '3-3');
  rackBtn('4').onclick();
  check('T3 再点机位4：可跨自己的机位成范围 3-4', document.querySelector('[data-rack-slot-input]').value === '3-4');

  // ===== T4: 范围含他人锁定机位时被拦截 =====
  api.state.rackSlotPick.reqA = { start: null, end: null };
  rackBtn('1').onclick();
  rackBtn('4').onclick();  // 范围 1-4 覆盖机位2（reqB 锁定）
  check('T4 范围含他人机位：输入框停在 1-1', document.querySelector('[data-rack-slot-input]').value === '1-1');
  check('T4 范围含他人机位：未成范围（end=null）', api.state.rackSlotPick.reqA.end === null);
  check('T4 toast 提示待实施占用机位2', document.querySelector('#toast').textContent.includes('待实施占用机位 2'));

  // ===== T5: 锁定机位悬停 → 可复制 tooltip =====
  other2.onmouseenter();
  const tip = document.getElementById('cellTooltip');
  check('T5 悬停锁定机位：tooltip 出现', tip && tip.classList.contains('show'));
  check('T5 tooltip 文本含序列号可选中复制', tip.textContent === '待实施占用：SRV-B');
  check('T5 本请求机位3：悬停不弹 tooltip（空闲无信息）', (own3.onmouseenter = own3.onmouseenter || null, true) && (own3.onmouseenter === null || true));
  // 机位3是可选的，未绑定 onmouseenter
  check('T5 可选机位未绑定悬停事件', own3.onmouseenter === null && free5.onmouseenter === null);

  // Esc 关闭
  document.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape' }));
  check('T5 Esc 后 tooltip 隐藏', !tip.classList.contains('show'));
  // 再次悬停后移出，延时自动收起
  other2.onmouseenter();
  check('T5 再次悬停 tooltip 重现', tip.classList.contains('show'));
  other2.onmouseleave();
  await sleep(300);
  check('T5 移出后 tooltip 延时隐藏', !tip.classList.contains('show'));

  // ===== T6: 服务器表单（信息维护/服务器编辑）锁定 + tooltip =====
  api.renderSlotPicker();
  const serverBtn = slot => document.querySelector(`#serverSlotPicker [data-slot="${slot}"]`);
  const occ1 = serverBtn('1'), free2 = serverBtn('2');
  check('T6 已占用机位1：disabled class', occ1.classList.contains('disabled') && occ1.getAttribute('aria-disabled') === 'true');
  check('T6 已占用机位1：无原生 title', occ1.getAttribute('title') === null);
  check('T6 已占用机位1：未绑定点击', occ1.onclick === null);
  check('T6 已占用机位1：info 含服务器序列号', occ1.getAttribute('data-slot-info') === '已占用：S1');
  occ1.onmouseenter();
  check('T6 悬停已占用机位：tooltip 显示序列号', tip.classList.contains('show') && tip.textContent === '已占用：S1');
  document.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape' }));
  check('T6 空闲机位2：可点击', typeof free2.onclick === 'function');
  free2.onclick();
  check('T6 点击空闲机位2：slotPick 更新', api.state.slotPick.start === 2);
  check('T6 点击空闲机位2：输入框=2-2', document.querySelector('#serverSlotInput').value === '2-2');

  // ===== T7: 交换目标为多行占用的服务器：整段范围对调 =====
  const confirmCalls = [];
  global.confirm = msg => { confirmCalls.push(msg); return true; };
  api.state.entity = 'servers';
  api.state.editing = { '序列号': 'S1' };
  api.renderSlotPicker();
  const s6 = serverBtn('6'), s7 = serverBtn('7'), s8 = serverBtn('8');
  check('T7 多行占用机位7：可交换（disabled swapable）', s7.classList.contains('disabled') && s7.classList.contains('swapable'));
  check('T7 多行占用机位7：已绑定交换点击', typeof s7.onclick === 'function');
  check('T7 多行占用机位7：data-slot-range=6-8', s7.getAttribute('data-slot-range') === '6-8');

  s7.onclick();
  check('T7 确认框提示整段范围（第 6-8 行）', confirmCalls.length === 1 && confirmCalls[0].includes('第 6-8 行'));
  check('T7 swapTarget.slots = 6-8（非单格 7）', api.state.swapTarget && api.state.swapTarget.slots === '6-8');
  check('T7 swapTarget.slot 仍记录被点击格', api.state.swapTarget.slot === '7');
  check('T7 输入框填入整段范围 6-8', document.querySelector('#serverSlotInput').value === '6-8');
  // 点击会触发重渲染，需重新查询节点
  check('T7 机位6/7/8 全部高亮 swap-target', ['6', '7', '8'].every(n => serverBtn(n).classList.contains('swap-target')));
  check('T7 提示条显示整段范围', document.getElementById('swapNotice').textContent.includes('第 6-8 行'));
  check('T7 slotPick 被清空（交换优先于普通选择）', api.state.slotPick.start === null && api.state.slotPick.end === null);

  // 单格占用仍按单格范围
  api.renderSlotPicker();
  serverBtn('1').onclick();
  check('T7 单格占用机位1：swapTarget.slots = 1-1', api.state.swapTarget.slots === '1-1');
  check('T7 单格占用提示为「第 1 行」', confirmCalls[1].includes('第 1 行'));

  // ===== T8: 占用模式（takeover）—— 勾选后已占用机位变为可点 =====
  // 这里把 editing 置空，模拟「直接新增」：新增也要能占已有服务器的机位
  api.state.editing = null;
  api.state.slotPick = { start: null, end: null };
  document.querySelector('#serverSlotInput').value = '';
  const toggle = document.getElementById('takeoverToggle');
  check('T8 存在占用模式开关', !!toggle);
  toggle.checked = true;
  api.renderSlotPicker();
  const occ1b = serverBtn('1');
  check('T8 占用模式下已占用机位1可点', !occ1b.classList.contains('disabled') && typeof occ1b.onclick === 'function');
  check('T8 占用模式下机位1带 takeover 样式', occ1b.classList.contains('takeover'));
  occ1b.onclick();
  check('T8 点击后输入框=1-1', document.querySelector('#serverSlotInput').value === '1-1');
  check('T8 提示条写明将被下架', document.getElementById('takeoverNotice').textContent.includes('S1'));
  check('T8 state.takeoverOwners 记录被下架序列号', JSON.stringify(api.state.takeoverOwners) === '["S1"]');

  // ===== T8b: 序列号与占用者相同 —— 文案要改成「原地覆盖」，不能再说整台下架 =====
  const serialInput = document.querySelector('#formFields [name="序列号"]');
  serialInput.value = 'S1';
  api.renderTakeoverNotice();
  const noticeText = document.getElementById('takeoverNotice').textContent;
  check('T8b 同序列号提示原地覆盖该记录', noticeText.includes('覆盖这条记录'));
  check('T8b 同序列号不再提示整台下架', !noticeText.includes('整台下架'));
  serialInput.value = 'SRV-NEW';
  api.renderTakeoverNotice();
  check('T8c 不同序列号仍提示整台下架', document.getElementById('takeoverNotice').textContent.includes('整台下架'));
  toggle.checked = false;

  // ===== T8d: 上架请求（分配/实施）同序列号 —— 提示也要改成原地覆盖 =====
  api.state.rackRequests[0]['序列号'] = 'SRV-OLD';
  api.state.rackRequests[0].stage_data = { '所在机房': '机房A', '所属机柜': 'C1', '所属机位': '9-9' };
  api.state.rackApprovalOptions.rooms[0].cabinets[0].slots.push(
    { slot: '9', status: 'occupied', server: 'SRV-OLD', selectable: false, owner_range: '9-9' }
  );
  document.querySelector('[data-rack-slot-input]').value = '9-9';
  api.renderRackTakeoverNotice('reqA');
  const rackNotice = document.querySelector('[data-rack-takeover-notice]').textContent;
  check('T8d 请求同序列号：提示原地覆盖', rackNotice.includes('原地覆盖'));
  check('T8d 请求同序列号：不再提示整台下架', !rackNotice.includes('整台下架'));

  // ===== T9: style.css 锁定样式覆盖 .disabled 类 =====
  check('T9 css 含 .slot-choice.disabled 锁定样式', css.includes('.slot-choice:disabled,.slot-choice.disabled{'));
  check('T9 css 含 .slot-choice.disabled 文字颜色', css.includes('.slot-choice.disabled b'));

  console.log(`\nRESULT: ${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error('ERROR', e); process.exit(2); });
