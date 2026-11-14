/**
 * 机柜统计页渲染验证（jsdom，提取 app.js 真实代码执行）：
 * 1. 按机房分组渲染（room-block / room-head）
 * 2. 服务器条目可点击 → 触发跳转信息维护并编辑（navigateToServerEdit stub 记录调用）
 * 3. 属性组合筛选：机房 / 机柜 / 序列号 / 客户名（四个都是可输入搜索的筛选框，含候选 datalist），
 *    机房/机柜为包含匹配，条件之间为「与」，不选则显示全部
 * 4. 双击机位：有服务器 → 进入编辑；空闲 → 提示；待实施 → 跳转具体实施
 * 5. 兼容旧接口（无 rooms 字段时按 cabinets 分组）
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

const code = [
  extractLine('const $ = s => document.querySelector(s);'),
  extractLine('const $$ = s =>'),
  extractBlock('function toast(msg, error = false)'),
  extractBlock('function esc(v)'),
  extractBlock('function slotStatusText(slot)'),
  extractBlock('function statsMark(text, q)'),
  extractBlock('function statsFilterValues()'),
  extractBlock('function statsFilterActive(f)'),
  extractBlock('function statsFilterKeywords(f)'),
  extractBlock('function statsServerMatches(server, f)'),
  extractBlock('function statsSlotMatches(slot, f)'),
  extractBlock('function statsSlotView(slot, f)'),
  extractBlock('function statsRoomMatches(room, f)'),
  extractBlock('function statsCabinetMatches(cabinet, f)'),
  extractBlock('function statsRoomsOf(stats)'),
  extractBlock('function renderStatsFilterOptions()'),
  extractBlock('function renderStatsFilterSummary(f, hits)'),
  extractBlock('function groupCabinetsByRoom(cabinets)'),
  extractBlock('function renderStatCabinet(cabinet, slots, q)'),
  extractBlock('function renderStats()'),
  extractBlock('function handleStatsSlotDblClick(el)'),
  extractBlock('function bindStatsInteractions()'),
].join('\n\n');

const dom = new JSDOM(`<!DOCTYPE html><html><body>
  <div id="toast"></div>
  <input id="sfRoom" list="sfRoomList">
  <datalist id="sfRoomList"></datalist>
  <input id="sfCabinet" list="sfCabinetList">
  <datalist id="sfCabinetList"></datalist>
  <input id="sfSerial" list="sfSerialList">
  <datalist id="sfSerialList"></datalist>
  <input id="sfClient" list="sfClientList">
  <datalist id="sfClientList"></datalist>
  <button id="sfReset"></button>
  <span id="sfSummary"></span>
  <div id="statCards"></div>
  <div id="cabinetGrid"></div>
</body></html>`);
global.document = dom.window.document;
global.window = dom.window;

const serverA = {
  '序列号': 'SRV-A', '品牌': 'Dell', '型号': 'R740', '所属机位': '5-5',
  '管理ip': '10.1.1.5', '客户名称': '客户一', '用途分类': '交易', '用途': '撮合',
};
const serverC = {
  '序列号': 'SRV-C', '品牌': 'HP', '型号': 'DL380', '所属机位': '2-2',
  '管理ip': '10.2.2.9', '客户名称': '客户二', '用途分类': '行情', '用途': '转发',
};
const stats = {
  summary: { cabinet_count: 2, server_count: 2, client_count: 2 },
  rooms: [
    {
      name: '机房A', cabinet_count: 1, slot_total: 10, slot_occupied: 1,
      cabinets: [{
        room: '机房A', name: 'C1', rack_range: '1-10',
        slots: [
          { slot: '2', status: 'free', servers: [] },
          { slot: '5', status: 'occupied', occupied_by: 'SRV-A', servers: [serverA] },
          { slot: '8', status: 'pending_implementation', occupied_by: 'SRV-B', pending_server: 'SRV-B', pending_request_id: 'reqB', servers: [] },
        ],
      }],
    },
    {
      name: '机房B', cabinet_count: 1, slot_total: 5, slot_occupied: 1,
      cabinets: [{
        room: '机房B', name: 'D9', rack_range: '1-5',
        slots: [
          { slot: '1', status: 'free', servers: [] },
          { slot: '2', status: 'occupied', occupied_by: 'SRV-C', servers: [serverC] },
        ],
      }],
    },
  ],
};

const factory = new Function(`
  const state = { stats: ${JSON.stringify(stats)} };
  const navCalls = [];
  const reviewCalls = [];
  const navigateToServerEdit = serial => { navCalls.push(serial); };
  const gotoReviewMode = mode => { reviewCalls.push(mode); };
  ${code}
  return { state, renderStats, renderStatsFilterOptions, statsFilterValues, statsFilterActive,
           handleStatsSlotDblClick, navCalls, reviewCalls };
`);
const api = factory();
const navCalls = api.navCalls;
const reviewCalls = api.reviewCalls;

let passed = 0, failed = 0;
function check(name, cond) {
  if (cond) { passed++; console.log('PASS ' + name); }
  else { failed++; console.log('FAIL ' + name); }
}
const $ = s => dom.window.document.querySelector(s);
const $$ = s => [...dom.window.document.querySelectorAll(s)];
const setFilter = o => {
  $('#sfRoom').value = o.room || '';
  $('#sfCabinet').value = o.cabinet || '';
  $('#sfSerial').value = o.serial || '';
  $('#sfClient').value = o.client || '';
  api.renderStats();
};

api.renderStatsFilterOptions();
api.renderStats();

// ===== T1: 按机房分组 =====
const blocks = $$('#cabinetGrid .room-block');
check('T1 渲染出 2 个机房分区', blocks.length === 2);
check('T1 第一个分区标题=机房A', blocks[0].querySelector('.room-head h3').textContent === '机房A');
check('T1 第二分区标题=机房B', blocks[1].querySelector('.room-head h3').textContent === '机房B');
check('T1 机房A 下机柜卡片数=1', blocks[0].querySelectorAll('.cabinet').length === 1);
check('T1 统计卡片渲染', $$('#statCards .stat-card').length === 3);
check('T1 无筛选时汇总为空', $('#sfSummary').textContent === '');

// ===== T2: 筛选选项来源 =====
check('T2 机房候选含 2 个机房', $('#sfRoomList').children.length === 2);
check('T2 机房候选值正确', [...$('#sfRoomList').children].map(o => o.value).join(',') === '机房A,机房B');
check('T2 机柜候选含 2 个机柜', $('#sfCabinetList').children.length === 2);
check('T2 机柜候选带机房归属', $('#sfCabinetList').children[0].textContent === '机房A / C1');
check('T2 四个筛选框均为可搜索输入框（input + list）', ['sfRoom', 'sfCabinet', 'sfSerial', 'sfClient']
  .every(id => document.getElementById(id).tagName === 'INPUT' && document.getElementById(id).getAttribute('list')));
check('T2 序列号候选含 SRV-A/SRV-B/SRV-C', ['SRV-A', 'SRV-B', 'SRV-C'].every(x => $('#sfSerialList').innerHTML.includes(x)));
check('T2 客户名候选含 客户一/客户二', ['客户一', '客户二'].every(x => $('#sfClientList').innerHTML.includes(x)));

// ===== T3: 服务器条目可点击 =====
const clickable = $('#cabinetGrid .server.clickable');
check('T3 服务器条目带 clickable 类', !!clickable);
check('T3 服务器条目绑定 onclick', typeof clickable?.onclick === 'function');
clickable.onclick({ stopPropagation() {} });
check('T3 点击服务器触发跳转编辑', navCalls[0] === 'SRV-A');

// ===== T4: 按机房筛选 =====
setFilter({ room: '机房B' });
check('T4 机房筛选后仅 1 个分区', $$('#cabinetGrid .room-block').length === 1);
check('T4 保留的是机房B', $('#cabinetGrid .room-head h3').textContent === '机房B');
check('T4 汇总提示含机房B', $('#sfSummary').textContent.includes('机房：机房B'));

// ===== T5: 按机柜筛选 =====
setFilter({ cabinet: 'C1' });
check('T5 机柜筛选后仅 1 个机柜', $$('#cabinetGrid .cabinet').length === 1);
check('T5 保留的是 C1', $('#cabinetGrid .cabinet-head b').textContent.includes('C1'));
setFilter({ cabinet: 'D9' });
check('T5 选 D9 时展示其全部 2 个机位', $$('#cabinetGrid .slot').length === 2);

// ===== T6: 按序列号筛选（含待实施机位）+ 高亮 =====
setFilter({ serial: 'SRV-A' });
let slots = $$('#cabinetGrid .slot');
check('T6 序列号命中 1 个机位', slots.length === 1 && slots[0].dataset.slot === '5');
check('T6 命中序列号被 mark 高亮', $('#cabinetGrid mark')?.textContent === 'SRV-A');
setFilter({ serial: 'SRV-B' });
slots = $$('#cabinetGrid .slot');
check('T6 待实施机位的服务器也能被序列号筛出', slots.length === 1 && slots[0].dataset.slot === '8');

// ===== T7: 按客户名筛选 =====
setFilter({ client: '客户二' });
slots = $$('#cabinetGrid .slot');
check('T7 客户名命中 1 个机位', slots.length === 1 && slots[0].dataset.slot === '2');
check('T7 命中客户名被 mark 高亮', $$('#cabinetGrid mark').some(m => m.textContent === '客户二'));
check('T7 汇总提示含命中台数', $('#sfSummary').textContent.includes('命中 1 台服务器'));

// ===== T8: 条件之间为「与」 =====
setFilter({ room: '机房A', client: '客户二' });
check('T8 机房A + 客户二 无交集 → 空态', $('#cabinetGrid .empty') !== null);
check('T8 空态文案提示筛选无结果', $('#cabinetGrid .empty').textContent === '没有符合筛选条件的机位');
setFilter({ cabinet: 'C1', serial: 'SRV-A' });
check('T8 机柜C1 + 序列号SRV-A 命中 1 机位', $$('#cabinetGrid .slot').length === 1);
setFilter({ serial: 'SRV-A', client: '客户二' });
check('T8 同一台服务器须同时满足两条件 → 空态', $('#cabinetGrid .empty') !== null);

// ===== T9: 清空筛选恢复全部 =====
setFilter({});
check('T9 清空条件后恢复 2 个分区', $$('#cabinetGrid .room-block').length === 2);
check('T9 清空条件后机位数=5', $$('#cabinetGrid .slot').length === 5);
check('T9 清空后汇总清空', $('#sfSummary').textContent === '');

// ===== T10: 机房输入时机柜候选收敛（且不打断已输入的机柜值） =====
$('#sfCabinet').value = 'D9';
$('#sfRoom').value = '机房B';
api.renderStatsFilterOptions();
check('T10 机房输入「机房B」后机柜候选仅 D9', $('#sfCabinetList').children.length === 1 && $('#sfCabinetList').children[0].value === 'D9');
check('T10 机柜候选显示「机房 / 机柜」', $('#sfCabinetList').children[0].textContent === '机房B / D9');
check('T10 重建候选不会清空已输入的机柜值', $('#sfCabinet').value === 'D9');
$('#sfRoom').value = '机房A';
api.renderStatsFilterOptions();
check('T10 机房改A 后机柜候选收敛为 C1', $('#sfCabinetList').children.length === 1 && $('#sfCabinetList').children[0].value === 'C1');
check('T10 机房为模糊输入时不收敛候选', (() => {
  $('#sfRoom').value = '机房';
  api.renderStatsFilterOptions();
  return $('#sfCabinetList').children.length === 2;
})());
$('#sfRoom').value = '';
$('#sfCabinet').value = '';
api.renderStatsFilterOptions();

// ===== T11: 双击机位 =====
setFilter({});
const slot5 = $('#cabinetGrid .slot[data-slot="5"]');
check('T11 机位绑定 ondblclick', typeof slot5?.ondblclick === 'function');
navCalls.length = 0;
slot5.ondblclick();
check('T11 双击有服务器的机位 → 进入编辑', navCalls[0] === 'SRV-A');
const slot8 = $('#cabinetGrid .slot[data-slot="8"]');
check('T11 待实施机位 data-status 正确', slot8.dataset.status === 'pending_implementation');
reviewCalls.length = 0;
slot8.ondblclick();
check('T11 双击待实施机位 → 跳转具体实施', reviewCalls[0] === 'implementation');
const slot2 = $('#cabinetGrid .slot[data-slot="2"]');
slot2.ondblclick();
check('T11 双击空闲机位提示 toast', $('#toast').textContent === '该机位当前空闲');

// ===== T12: 机房 / 机柜也支持模糊搜索（包含匹配） =====
setFilter({ room: '机房' });
check('T12 机房模糊输入「机房」命中 2 个分区', $$('#cabinetGrid .room-block').length === 2);
setFilter({ cabinet: 'D' });
check('T12 机柜模糊输入「D」仅命中 D9', $$('#cabinetGrid .cabinet').length === 1 && $('#cabinetGrid .cabinet-head b').textContent.includes('D9'));
setFilter({ room: 'A', cabinet: 'C' });
check('T12 机房输入「A」+ 机柜输入「C」命中 C1', $$('#cabinetGrid .cabinet').length === 1 && $('#cabinetGrid .cabinet-head b').textContent.includes('C1'));
setFilter({});

// ===== T13: 兼容旧接口（无 rooms 字段时按 cabinets 分组） =====
api.state.stats = { summary: stats.summary, cabinets: [stats.rooms[0].cabinets[0]] };
api.renderStatsFilterOptions();
api.renderStats();
check('T12 无 rooms 字段时仍按机房分组', $$('#cabinetGrid .room-block').length === 1);

console.log(`\nRESULT: ${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
