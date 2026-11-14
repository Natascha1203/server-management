/**
 * 「字段选择框」模糊搜索（jsdom，提取 app.js 真实代码执行）：
 * 1. renderFilters / renderSorts / renderReviewFilters / renderRackRequestFilters 渲染出 .field-combo
 *    （输入框 + 候选项菜单），不再使用原生 <select>
 * 2. comboMatch 模糊匹配：忽略空格与大小写、按“包含”匹配（管理ip → 管理ip；管理 → 管理ip/管理密码）
 * 3. 打开菜单显示全部字段；输入关键词后只保留命中项；无命中显示空态
 * 4. 选中候选项 → data-value / 输入框 / .on 高亮同步；关闭时未提交的输入还原为已选字段
 * 5. 键盘交互（ArrowDown / ArrowUp / Enter / Escape）与鼠标交互（mousedown 选候选项）
 * 6. collectFilters / collectSorts / collectReviewFilters 从 [data-combo] 取字段、
 *    从 [data-pattern] / [data-direction] 取其余值
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const APP_DIR = path.resolve(__dirname, '..');
const src = fs.readFileSync(path.join(APP_DIR, 'app/static/app.js'), 'utf8');
const css = fs.readFileSync(path.join(APP_DIR, 'app/static/style.css'), 'utf8');

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

const SERVERS_HEADERS = ['序列号', '品牌', '型号', '客户id', '客户名称', '负责人', '归属单位', 'CPU型号', '网卡', '管理ip', '交易ip', '带外ip', '管理密码', '带外密码', '服务器成本(万)', '用途分类', '用途', '防火墙白名单', '资源申请编号', '是否液冷', '所在机房', '所属机柜', '所属机位', '上架时间', '接入行情', '接入柜台', '备注'];

const code = [
  extractLine('const $ = s => document.querySelector(s);'),
  extractLine('const $$ = s =>'),
  extractLine('const RACK_REVIEW_FILTER_FIELDS ='),
  extractBlock('function esc(v)'),
  extractBlock('function fieldComboHtml(field, options)'),
  extractBlock('function comboInput(el)'),
  extractBlock('function comboOptions(el)'),
  extractBlock('function comboValue(el)'),
  extractBlock('function comboMatch(text, query)'),
  extractBlock('function setActiveComboOption(el, value)'),
  extractBlock('function activeComboOption(el)'),
  extractBlock('function filterFieldCombo(el, query)'),
  extractBlock('function openFieldCombo(el)'),
  extractBlock('function closeFieldCombo(el, revert = true)'),
  extractBlock('function closeFieldCombos(keep)'),
  extractBlock('function chooseFieldCombo(el, value)'),
  extractBlock('function moveActiveComboOption(el, step)'),
  extractBlock('function bindFieldCombos()'),
  extractBlock('function renderFilters()'),
  extractBlock('function filterFields()'),
  extractBlock('function collectFilters()'),
  extractBlock('function renderSorts()'),
  extractBlock('function collectSorts()'),
  extractBlock('function renderReviewFilters()'),
  extractBlock('function collectReviewFilters()'),
  extractBlock('function renderRackRequestFilters()'),
  extractBlock('function collectRackRequestFilters()'),
].join('\n\n');

const dom = new JSDOM(`<!DOCTYPE html><html><body>
  <div id="filterRows" class="filter-rows"></div>
  <div id="sortRows" class="filter-rows"></div>
  <div id="reviewFilterRows" class="filter-rows"></div>
  <div id="rackRequestFilterRows" class="filter-rows"></div>
</body></html>`, { runScripts: 'outside-only' });

const state = {
  meta: { label: '服务器', key: ['序列号'], sheet: '服务器信息', filename: '服务器信息表.xlsx', headers: SERVERS_HEADERS },
  filters: [],
  sorts: [],
  reviewFilters: [],
  rackRequestFilters: [],
};

const build = dom.window.Function('state', `
  ${code}
  return { state, bindFieldCombos, renderFilters, filterFields, collectFilters, renderSorts, collectSorts,
    renderReviewFilters, collectReviewFilters, renderRackRequestFilters, collectRackRequestFilters,
    fieldComboHtml, comboValue, comboMatch, openFieldCombo, closeFieldCombo, filterFieldCombo,
    chooseFieldCombo, activeComboOption, comboOptions, comboInput };
`);
const api = build(state);
const doc = dom.window.document;
const $ = s => doc.querySelector(s);
const $$ = s => [...doc.querySelectorAll(s)];
const fire = (el, type, init = {}) => el.dispatchEvent(new dom.window.Event(type, { bubbles: true, ...init }));
const fireKey = (el, key) => el.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key, bubbles: true }));

api.bindFieldCombos();

// ===== T1: 渲染出可搜索的字段选择框（不再是原生 select） =====
api.renderFilters();
const combo = $('#filterRows [data-combo]');
const comboInputEl = $('#filterRows .combo-input');
check('T1 查询条件渲染出 .field-combo', !!combo);
check('T1 输入框存在且带 placeholder', !!comboInputEl && comboInputEl.placeholder.includes('搜索'));
check('T1 默认字段为表头首列且已高亮 .on',
  api.comboValue(combo) === SERVERS_HEADERS[0] && combo.querySelector('.combo-option.on')?.dataset.value === SERVERS_HEADERS[0],
  api.comboValue(combo));
check('T1 候选项 = 在途状态 + 全部表头', api.comboOptions(combo).length === SERVERS_HEADERS.length + 1, api.comboOptions(combo).length);
check('T1 候选项含「管理ip」与「备注」',
  api.comboOptions(combo).some(o => o.dataset.value === '管理ip') && api.comboOptions(combo).some(o => o.dataset.value === '备注'));
check('T1 查询条件里不再有原生 select', $('#filterRows filter-row select') === null && $('#filterRows .filter-row select') === null);
check('T1 正则输入框带 data-pattern 便于取值', !!$('#filterRows input[data-pattern]'));

// ===== T2: 模糊匹配规则 =====
check('T2 「管理ip」命中「管理ip」', api.comboMatch('管理ip', '管理ip'));
check('T2 「管理」命中「管理ip」', api.comboMatch('管理ip', '管理'));
check('T2 「管理」命中「管理密码」', api.comboMatch('管理密码', '管理'));
check('T2 「ip」命中「管理ip」', api.comboMatch('管理ip', 'ip'));
check('T2 忽略空格（管理 ip）', api.comboMatch('管理ip', '管理 ip'));
check('T2 忽略大小写（IP）', api.comboMatch('管理ip', 'IP'));
check('T2 不相关词不命中', !api.comboMatch('备注', '管理ip'));
check('T2 反向不命中（备注 不命中 管理ip）', !api.comboMatch('管理ip', '备注'));
check('T2 空关键词命中全部', api.comboMatch('任意字段', ''));

// ===== T3: 打开菜单 + 输入关键词过滤 =====
api.openFieldCombo(combo);
check('T3 打开后菜单可见、输入框 aria-expanded=true',
  combo.classList.contains('open') && !combo.querySelector('.combo-menu').hidden
  && comboInputEl.getAttribute('aria-expanded') === 'true');
check('T3 打开时展示全部候选', api.comboOptions(combo).every(o => !o.hidden));

api.filterFieldCombo(combo, '管理');
const visible = api.comboOptions(combo).filter(o => !o.hidden).map(o => o.dataset.value);
check('T3 输入「管理」只剩管理ip / 管理密码',
  JSON.stringify(visible) === JSON.stringify(['管理ip', '管理密码']), JSON.stringify(visible));
check('T3 首条命中项被高亮 active', api.activeComboOption(combo)?.dataset.value === '管理ip',
  api.activeComboOption(combo)?.dataset.value);
check('T3 有命中时空态隐藏', combo.querySelector('.combo-empty').hidden);

api.filterFieldCombo(combo, '管理ip');
check('T3 输入「管理ip」只剩该项',
  api.comboOptions(combo).filter(o => !o.hidden).map(o => o.dataset.value).join() === '管理ip');
check('T3 精确命中时高亮该项', api.activeComboOption(combo)?.dataset.value === '管理ip');

api.filterFieldCombo(combo, 'zzz');
check('T3 无命中时显示空态', !combo.querySelector('.combo-empty').hidden
  && api.comboOptions(combo).every(o => o.hidden));
api.filterFieldCombo(combo, '');
check('T3 清空关键词恢复全部候选', api.comboOptions(combo).every(o => !o.hidden));

// ===== T4: 选中候选项（鼠标 mousedown） =====
const whitelistOption = api.comboOptions(combo).find(o => o.dataset.value === '防火墙白名单');
whitelistOption.dispatchEvent(new dom.window.MouseEvent('mousedown', { bubbles: true, cancelable: true }));
check('T4 mousedown 选中后 data-value 更新', api.comboValue(combo) === '防火墙白名单', api.comboValue(combo));
check('T4 输入框同步显示已选字段', comboInputEl.value === '防火墙白名单', comboInputEl.value);
check('T4 .on 高亮移动到已选字段',
  combo.querySelector('.combo-option.on')?.dataset.value === '防火墙白名单');
check('T4 选完菜单收起', !combo.classList.contains('open') && combo.querySelector('.combo-menu').hidden);

// ===== T5: 关闭时把未提交的输入还原为已选字段 =====
comboInputEl.value = '管理';
api.closeFieldCombo(combo);
check('T5 放弃搜索后输入框还原为已选字段', comboInputEl.value === '防火墙白名单', comboInputEl.value);
check('T5 还原不影响 data-value', api.comboValue(combo) === '防火墙白名单');

// ===== T6: 键盘交互（真实事件） =====
api.renderFilters();
const kCombo = $('#filterRows [data-combo]');
const kInput = $('#filterRows .combo-input');
const hasFocusEvents = (() => {
  let got = false;
  const probe = () => { got = true; };
  doc.addEventListener('focusin', probe);
  kInput.focus();
  doc.removeEventListener('focusin', probe);
  return got;
})();
check('T6 jsdom 支持 focusin（用于自动展开）', hasFocusEvents, String(hasFocusEvents));
check('T6 聚焦自动展开菜单', kCombo.classList.contains('open'));
check('T6 聚焦即全选文本（直接输入可替换）', kInput.selectionStart === 0 && kInput.selectionEnd === kInput.value.length,
  `${kInput.selectionStart}-${kInput.selectionEnd}`);

kInput.value = '管理';
fire(kInput, 'input');
check('T6 输入事件过滤候选', api.comboOptions(kCombo).filter(o => !o.hidden).map(o => o.dataset.value).join() === '管理ip,管理密码',
  api.comboOptions(kCombo).filter(o => !o.hidden).map(o => o.dataset.value).join());
check('T6 过滤后高亮首个命中项', api.activeComboOption(kCombo)?.dataset.value === '管理ip',
  api.activeComboOption(kCombo)?.dataset.value);
fireKey(kInput, 'ArrowDown');
check('T6 ArrowDown 移动到下一命中项', api.activeComboOption(kCombo)?.dataset.value === '管理密码',
  api.activeComboOption(kCombo)?.dataset.value);
fireKey(kInput, 'ArrowUp');
check('T6 ArrowUp 回到上一命中项', api.activeComboOption(kCombo)?.dataset.value === '管理ip');
fireKey(kInput, 'Enter');
check('T6 Enter 选中高亮候选', api.comboValue(kCombo) === '管理ip', api.comboValue(kCombo));
check('T6 Enter 后菜单收起且输入框=已选字段',
  !kCombo.classList.contains('open') && kInput.value === '管理ip', kInput.value);

kInput.value = '管理密码';
fire(kInput, 'input');
fireKey(kInput, 'Enter');
check('T6 直接输入完整字段名后 Enter 也能选中', api.comboValue(kCombo) === '管理密码', api.comboValue(kCombo));

kInput.value = 'zzz';
fire(kInput, 'input');
fireKey(kInput, 'Escape');
check('T6 Escape 放弃输入并还原', kInput.value === '管理密码' && !kCombo.classList.contains('open'), kInput.value);

// 未过滤时可在完整字段列表中按上下键移动
api.chooseFieldCombo(kCombo, '序列号');
check('T6 键盘选中后菜单收起', !kCombo.classList.contains('open'));

// 输入框已聚焦时再次点击不会触发 focusin，需要点击后能重新展开
kInput.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
check('T6 已聚焦时点击输入框可重新展开', kCombo.classList.contains('open'));
check('T6 重新展开时高亮回落到已选字段', api.activeComboOption(kCombo)?.dataset.value === '序列号',
  api.activeComboOption(kCombo)?.dataset.value);
fireKey(kInput, 'ArrowDown');
check('T6 ArrowDown 移动到下一字段（品牌）', api.activeComboOption(kCombo)?.dataset.value === '品牌',
  api.activeComboOption(kCombo)?.dataset.value);
fireKey(kInput, 'ArrowUp');
check('T6 ArrowUp 回到上一字段（序列号）', api.activeComboOption(kCombo)?.dataset.value === '序列号');

// 右侧箭头可切换展开/收起
const caret = kCombo.querySelector('.combo-caret');
caret.dispatchEvent(new dom.window.MouseEvent('mousedown', { bubbles: true, cancelable: true }));
check('T6 已展开时点箭头收起', !kCombo.classList.contains('open'));
caret.dispatchEvent(new dom.window.MouseEvent('mousedown', { bubbles: true, cancelable: true }));
check('T6 收起后点箭头再次展开', kCombo.classList.contains('open'));

// ===== T7: 取值（collectFilters / collectSorts） =====
api.chooseFieldCombo(kCombo, '备注');
$('#filterRows input[data-pattern]').value = '双路 PDU';
check('T7 collectFilters 字段取自 [data-combo].data-value',
  JSON.stringify(api.collectFilters()) === JSON.stringify([{ field: '备注', pattern: '双路 PDU' }]),
  JSON.stringify(api.collectFilters()));
check('T7 collectFilters 不再误取组合框内的输入框',
  api.collectFilters()[0].pattern === '双路 PDU');

state.sorts = [{ field: '管理ip', direction: 'desc' }];
api.renderSorts();
const sCombo = $('#sortRows [data-combo]');
check('T7 排序条件也渲染为字段选择框', !!sCombo && api.comboValue(sCombo) === '管理ip');
check('T7 排序方向仍为原生 select', $('#sortRows [data-direction]')?.value === 'desc');
check('T7 collectSorts 字段取自组合框、方向取自 [data-direction]',
  JSON.stringify(api.collectSorts()) === JSON.stringify([{ field: '管理ip', direction: 'desc' }]),
  JSON.stringify(api.collectSorts()));

// ===== T8: 审核页 / 上下架请求页的筛选框 =====
api.renderReviewFilters();
api.renderRackRequestFilters();
const rCombo = $('#reviewFilterRows [data-combo]');
const qCombo = $('#rackRequestFilterRows [data-combo]');
check('T8 审核页筛选框为字段选择框', !!rCombo && api.comboValue(rCombo) === '序列号');
check('T8 上下架请求页筛选框为字段选择框', !!qCombo && api.comboValue(qCombo) === '序列号');
check('T8 审核页候选 = RACK_REVIEW_FILTER_FIELDS（4 项）', api.comboOptions(rCombo).length === 4, api.comboOptions(rCombo).length);
check('T8 审核页候选含「客户名称」', api.comboOptions(rCombo).some(o => o.dataset.value === '客户名称'));
$('#rackRequestFilterRows input[data-pattern]').value = 'SN-1';
api.chooseFieldCombo(qCombo, '客户id');
check('T8 collectReviewFilters 取组合框字段',
  JSON.stringify(api.collectRackRequestFilters()) === JSON.stringify([{ field: '客户id', pattern: 'SN-1' }]),
  JSON.stringify(api.collectRackRequestFilters()));

// ===== T9: 样式 =====
check('T9 CSS 含 .field-combo 定位样式', /\.field-combo\{position:relative/.test(css));
check('T9 CSS 含候选项菜单浮层样式（z-index）', /\.field-combo \.combo-menu\{[^}]*z-index/.test(css));
check('T9 CSS 空态/隐藏态样式齐备',
  /\.combo-option\[hidden\]\{display:none\}/.test(css) && /\.combo-empty\[hidden\]\{display:none\}/.test(css)
  && /\.combo-menu\[hidden\]\{display:none\}/.test(css));

console.log('RESULT: ' + passed + ' passed, ' + failures.length + ' failed');
if (failures.length) {
  console.log(failures.map(f => '  FAIL ' + f).join('\n'));
  process.exitCode = 1;
}
