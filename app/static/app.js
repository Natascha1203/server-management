const APP_CAPABILITIES = window.APP_CAPABILITIES || [];
const APP_USER = window.APP_USER || {};
const SERVER_APPROVAL_HEADERS = ['序列号', '品牌', '型号', 'CPU型号', '网卡', '服务器成本(万)', '归属单位', '用途分类', '用途', '防火墙白名单', '资源申请编号', '是否液冷', '客户id', '客户名称', '所在机房', '所属机柜', '所属机位', '管理ip', '交易ip', '带外ip', '管理密码', '带外密码', '上架时间', '接入行情', '接入柜台'];
const RACK_REVIEW_FILTER_FIELDS = ['序列号', '客户id', '客户名称', '所在机房'];
const state = { entity: 'cabinets', records: [], meta: null, editing: null, pendingEditing: null, rackEditing: null, rackRequestStatus: 'pending', status: 'pending', reviewMode: 'allocation', stats: null, filters: [], sorts: [], reviewFilters: [], rackRequestFilters: [], changes: [], rackRequests: [], rackApprovalOptions: { rooms: [] }, rackSlotPick: {}, rackRequestExpanded: [], rackReviewExpanded: [], systemUsers: [], customerOptions: [] };
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];

function hasCapability(name) {
  return name === 'view_maintenance' || APP_CAPABILITIES.includes(name) || APP_CAPABILITIES.includes('manage_all');
}

function isAdmin() {
  return APP_USER && APP_USER['身份'] === '管理人员';
}

function toast(msg, error = false) {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast show' + (error ? ' error' : '');
  setTimeout(() => el.className = 'toast', 3200);
}

async function api(url, opt = {}) {
  const r = await fetch(url, opt);
  let data = {};
  try { data = await r.json(); } catch {}
  if (!r.ok) {
    const err = new Error(data.error || '请求失败');
    err.data = data;
    throw err;
  }
  return data;
}

function formatImportErrorDetails(details) {
  return details.map(item => {
    const messages = Array.isArray(item.errors) ? item.errors : [item.error].filter(Boolean);
    return `第 ${item.row} 行：${messages.join('；')}`;
  }).join('\n');
}

function esc(v) {
  return String(v ?? '').replace(/[&<>'"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]));
}

async function loadCustomerOptions(force = false) {
  if (!force && state.customerOptions.length) return state.customerOptions;
  const data = await api('/api/customer-options');
  state.customerOptions = data.customers || [];
  renderCustomerDatalists();
  return state.customerOptions;
}

function renderCustomerDatalists() {
  const idOptions = state.customerOptions.map(item => `<option value="${esc(item['客户id'])}">${esc(item['客户名称'])}</option>`).join('');
  const nameOptions = state.customerOptions.map(item => `<option value="${esc(item['客户名称'])}">${esc(item['客户id'])}</option>`).join('');
  $$('datalist[data-customer-id-options]').forEach(list => list.innerHTML = idOptions);
  $$('datalist[data-customer-name-options]').forEach(list => list.innerHTML = nameOptions);
}

function customerInput(field, value = '', extra = '') {
  const listId = field === '客户id' ? 'customerIdOptions' : 'customerNameOptions';
  const marker = field === '客户id' ? 'data-customer-id' : 'data-customer-name';
  return `<input name="${esc(field)}" value="${esc(value || '')}" list="${listId}" ${marker} ${extra}>`;
}

function bindCustomerInputs(root = document) {
  const idInput = root.querySelector('[data-customer-id]');
  const nameInput = root.querySelector('[data-customer-name]');
  if (!idInput || !nameInput) return;
  const syncById = () => {
    const item = state.customerOptions.find(row => row['客户id'] === idInput.value.trim());
    if (item) nameInput.value = item['客户名称'] || '';
  };
  const syncByName = () => {
    const item = state.customerOptions.find(row => row['客户名称'] === nameInput.value.trim());
    if (item) idInput.value = item['客户id'] || '';
  };
  idInput.onchange = syncById;
  idInput.oninput = syncById;
  nameInput.onchange = syncByName;
  nameInput.oninput = syncByName;
  syncById();
  // 「自动同步负责人」按钮：按当前客户从客户表负责人列填充服务器负责人字段
  const syncOwnerBtn = root.querySelector('#syncOwnerBtn');
  const ownerInput = root.querySelector('input[name="负责人"]');
  if (syncOwnerBtn && ownerInput) {
    syncOwnerBtn.onclick = async () => {
      const id = idInput.value.trim();
      const name = nameInput.value.trim();
      if (!id && !name) {
        toast('请先选择客户', true);
        return;
      }
      // 强制重新拉取客户表最新数据（负责人可能刚在 xlsx 里改过）
      try {
        await loadCustomerOptions(true);
      } catch (e) {
        toast(e.message, true);
        return;
      }
      const item = state.customerOptions.find(row => row['客户id'] === id || row['客户名称'] === name);
      if (!item) {
        toast('未找到匹配客户', true);
        return;
      }
      ownerInput.value = item['负责人'] || '';
      toast(item['负责人'] ? '已同步负责人' : '该客户负责人为空');
    };
  }
}

function isRackReviewMode() {
  return state.reviewMode === 'allocation' || state.reviewMode === 'implementation';
}

function updateReviewControls() {
  const isRack = isRackReviewMode();
  if (isRack) state.status = 'pending';
  $$('.review-filters .filter').forEach(button => {
    button.style.display = isRack && button.dataset.status !== 'pending' ? 'none' : '';
    button.classList.toggle('active', button.dataset.status === state.status);
  });
  const searchPanel = $('#reviewSearchPanel');
  if (searchPanel) searchPanel.style.display = isRack && state.status === 'pending' ? 'flex' : 'none';
  const pendingButton = $('.review-filters .filter[data-status="pending"]');
  if (pendingButton) pendingButton.textContent = isRack ? '待处理' : '待审核';
  renderReviewFilters();
}

function openPage(b) {
  const p = b.dataset.page;
  $$('.nav-item').forEach(x => x.classList.toggle('active', x === b || (p === 'review' && x.dataset.page === 'review')));
  $$('.nav-sub-item').forEach(x => x.classList.toggle('active', x === b));
  $$('.page').forEach(x => x.classList.toggle('active', x.id === p));
  $('#pageTitle').textContent = { maintenance: '信息维护', review: '审核管理', statistics: '机柜统计', rackRequests: '上下架请求', settings: '设置' }[p];
  if (p === 'review') {
    if (b.dataset.navReviewMode) {
      if (state.reviewMode !== b.dataset.navReviewMode) {
        state.reviewFilters = [];
        state.rackReviewExpanded = [];
      }
      state.reviewMode = b.dataset.navReviewMode;
    }
    state.status = 'pending';
    updateReviewControls();
    loadChanges();
  }
  if (p === 'statistics') loadStats();
  if (p === 'rackRequests') loadRackRequests();
  if (p === 'settings') {
    loadSettings();
    loadSystemUsers();
  }
}

$$('.nav-item,.nav-sub-item').forEach(b => b.onclick = () => openPage(b));

$$('.entity-tab').forEach(b => b.onclick = () => {
  $$('.entity-tab').forEach(x => x.classList.toggle('active', x === b));
  state.entity = b.dataset.entity;
  loadEntity();
});

function applyCapabilityVisibility() {
  $$('.nav-item[data-capability]').forEach(item => {
    item.style.display = hasCapability(item.dataset.capability) ? '' : 'none';
  });
  $$('.nav-sub-item[data-capability]').forEach(item => {
    item.style.display = hasCapability(item.dataset.capability) ? '' : 'none';
  });
  if (APP_USER['身份'] === '机房人员') {
    $$('.entity-tab[data-entity="customers"]').forEach(item => item.style.display = 'none');
  }
  if (APP_USER['身份'] === '业务人员') {
    $$('.entity-tab[data-entity="cabinets"]').forEach(item => item.style.display = 'none');
    state.entity = 'customers';
    const customerTab = $('.entity-tab[data-entity="customers"]');
    if (customerTab) {
      $$('.entity-tab').forEach(x => x.classList.toggle('active', x === customerTab));
    }
  }
}

async function loadEntity() {
  const d = await api(`/api/${state.entity}`);
  state.records = d.records;
  state.meta = d.meta;
  state.filters = [];
  state.sorts = [];
  $('#downloadXlsxTemplateBtn').href = `/api/${state.entity}/template?format=xlsx`;
  $('#downloadCsvTemplateBtn').href = `/api/${state.entity}/template?format=csv`;
  $('#tableName').textContent = d.meta.label + '信息';
  renderFilters();
  renderSorts();
  renderTable();
}

function renderTable() {
  const rows = state.records;
  const cols = state.meta.headers;
  $('#recordCount').textContent = `${rows.length} 条记录`;
  $('#dataHead').innerHTML = '<tr><th class="select-col"><input id="selectAllRows" type="checkbox" title="全选"></th><th>在途状态</th><th>操作</th>' + cols.map(h => `<th>${esc(h)}</th>`).join('') + '</tr>';
  $('#dataBody').innerHTML = rows.length ? rows.map((r, index) => {
    const pending = Boolean(r._pending_operation);
    const rowClass = r._pending_operation === '删除' ? ' class="pending-delete"' : '';
    const disabled = pending ? ' disabled' : '';
    const encoded = encodeURIComponent(JSON.stringify(stripPendingFields(r)));
    const editPending = pending && r._pending_operation !== '删除' ? `<button class="mini" onclick="openPendingEdit(${index})">编辑待审批</button>` : '';
    const pendingAction = pending ? `<button class="mini" onclick="showPendingChange(${index})">查看待审批</button>${editPending}<button class="mini delete" onclick="withdrawPendingChange(${index})">撤回</button>` : '';
    return `<tr${rowClass}><td class="select-col"><input class="row-select" type="checkbox" data-index="${index}" title="选择"${disabled}></td><td>${pendingStatus(r)}</td><td><div class="row-actions">${pendingAction}<button class="mini" onclick="openEdit('${encoded}')"${disabled}>修改</button><button class="mini delete" onclick="removeRow('${encoded}')"${disabled}>删除</button></div></td>${cols.map(h => `<td class="cell-text">${esc(r[h])}</td>`).join('')}</tr>`;
  }).join('') : '<tr><td class="empty" colspan="99">暂无正式数据</td></tr>';
  $('#selectAllRows').onchange = e => {
    $$('.row-select:not(:disabled)').forEach(input => input.checked = e.target.checked);
    updateRowSelection();
  };
  $$('.row-select').forEach(input => input.onchange = updateRowSelection);
  updateRowSelection();
  bindCellTooltips();
  renderFreezeControl();
  applyFrozenColumns();
}

let cellTooltipTimer = null;

function ensureCellTooltip() {
  let tip = document.getElementById('cellTooltip');
  if (!tip) {
    tip = document.createElement('div');
    tip.id = 'cellTooltip';
    tip.className = 'cell-tooltip';
    document.body.appendChild(tip);
    tip.onmouseenter = () => clearTimeout(cellTooltipTimer);
    tip.onmouseleave = scheduleCellTooltipHide;
    document.addEventListener('keydown', e => { if (e.key === 'Escape') tip.classList.remove('show'); });
    document.addEventListener('mousedown', e => {
      if (tip.classList.contains('show') && !tip.contains(e.target)) tip.classList.remove('show');
    });
  }
  return tip;
}

function selectionInTooltip(tip) {
  const sel = window.getSelection();
  return Boolean(sel && sel.rangeCount && !sel.isCollapsed && tip.contains(sel.anchorNode) && tip.contains(sel.focusNode));
}

function scheduleCellTooltipHide() {
  clearTimeout(cellTooltipTimer);
  cellTooltipTimer = setTimeout(() => {
    const tip = document.getElementById('cellTooltip');
    if (!tip || !tip.classList.contains('show')) return;
    if (tip.matches(':hover') || selectionInTooltip(tip)) return;  // 正在悬停或已选中文本时不收起，便于复制
    tip.classList.remove('show');
  }, 220);
}

function showCopyTooltip(anchor, text) {
  clearTimeout(cellTooltipTimer);
  const tip = ensureCellTooltip();
  tip.textContent = text;
  tip.classList.add('show');
  const rect = anchor.getBoundingClientRect();
  const width = tip.offsetWidth, height = tip.offsetHeight;
  let left = Math.max(8, Math.min(rect.left, window.innerWidth - width - 12));
  let top = rect.bottom + 6;
  if (top + height > window.innerHeight - 8) top = Math.max(8, rect.top - height - 6);
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}

function bindCellTooltips() {
  $$('#dataBody td.cell-text').forEach(td => {
    td.onmouseenter = () => {
      if (td.scrollWidth <= td.clientWidth + 1) return;  // 未被截断的单元格不弹
      showCopyTooltip(td, td.textContent);
    };
    td.onmouseleave = scheduleCellTooltipHide;
  });
}

// ---------- 冻结列 ----------
const FREEZE_STORAGE_KEY = 'sm_freeze_cols';

function totalColumnCount() {
  // 选择框 + 在途状态 + 操作 + 数据列
  return 3 + (state.meta ? state.meta.headers.length : 0);
}

function getFreezeCols() {
  const raw = parseInt(localStorage.getItem(FREEZE_STORAGE_KEY) || '0', 10);
  if (Number.isNaN(raw) || raw < 0) return 0;
  return Math.min(raw, totalColumnCount());
}

function setFreezeCols(n) {
  localStorage.setItem(FREEZE_STORAGE_KEY, String(n));
}

function renderFreezeControl() {
  const sel = $('#freezeColsSelect');
  if (!sel) return;
  const total = totalColumnCount();
  const current = getFreezeCols();
  let options = '<option value="0">不冻结</option>';
  for (let n = 1; n <= total; n++) {
    options += `<option value="${n}"${n === current ? ' selected' : ''}>前 ${n} 列</option>`;
  }
  sel.innerHTML = options;
  sel.value = String(current);
  sel.onchange = () => {
    setFreezeCols(parseInt(sel.value, 10) || 0);
    applyFrozenColumns();
  };
}

function applyFrozenColumns() {
  const table = $('#dataBody');
  if (!table || !state.meta) return;
  const freezeCount = getFreezeCols();
  const headCells = $$('#dataHead th');
  const bodyRows = $$('#dataBody tr');
  const dataRows = bodyRows.filter(tr => !tr.classList.contains('empty-row') && !tr.querySelector('.empty'));

  // 清除旧的冻结标记
  $$('#dataHead th.freeze-col, #dataBody td.freeze-col').forEach(el => {
    el.classList.remove('freeze-col', 'freeze-last');
    el.style.left = '';
  });

  if (freezeCount <= 0) return;

  // 测量表头各列实际宽度（表头与表体同宽）
  const widths = headCells.map(th => th.getBoundingClientRect().width);

  // 累计偏移：第 i 列（0-based）的 left = 前 i 列宽度之和
  const offsets = [];
  let acc = 0;
  for (let i = 0; i < headCells.length; i++) {
    offsets[i] = acc;
    acc += widths[i];
  }

  const freezeTotal = Math.min(freezeCount, headCells.length);
  const lastIndex = freezeTotal - 1;

  headCells.forEach((th, i) => {
    if (i >= freezeTotal) return;
    th.classList.add('freeze-col');
    if (i === lastIndex) th.classList.add('freeze-last');
    th.style.left = offsets[i] + 'px';
  });

  dataRows.forEach(tr => {
    const tds = [...tr.children].filter(td => td.tagName === 'TD');
    tds.forEach((td, i) => {
      if (i >= freezeTotal) return;
      td.classList.add('freeze-col');
      if (i === lastIndex) td.classList.add('freeze-last');
      td.style.left = offsets[i] + 'px';
    });
  });
}

function bindSlotInfoTooltip(btn, info) {
  if (!info) return;
  btn.onmouseenter = () => showCopyTooltip(btn, info);
  btn.onmouseleave = scheduleCellTooltipHide;
}

function stripPendingFields(row) {
  return Object.fromEntries(Object.entries(row).filter(([key]) => !key.startsWith('_pending_')));
}

function pendingStatus(row) {
  if (!row._pending_operation) return '<span class="muted">正式</span>';
  return `<span class="pending-status ${row._pending_operation === '删除' ? 'delete' : ''}">待审批${esc(row._pending_operation)}</span>`;
}

window.showPendingChange = index => {
  const row = state.records[index];
  if (!row?._pending_operation) return;
  const next = row._pending_new_data ? `\n\n待审批数据：\n${Object.entries(row._pending_new_data).map(([k, v]) => `${k}：${v || '-'}`).join('\n')}` : '';
  alert(`状态：待审批${row._pending_operation}\n提交人：${row._pending_submitter || '-'}\n提交时间：${row._pending_submitted_at || '-'}${next}`);
};

window.openPendingEdit = index => {
  const row = state.records[index];
  if (!row?._pending_change_id || row._pending_operation === '删除') return;
  state.pendingEditing = row;
  const pendingRow = row._pending_new_data || stripPendingFields(row);
  openModal('pending', pendingRow);
};

window.withdrawPendingChange = async index => {
  const row = state.records[index];
  if (!row?._pending_change_id) return;
  if (!confirm(`确定撤回这条待审批${row._pending_operation}申请？撤回不需要审批。`)) return;
  try {
    const d = await api(`/api/changes/${row._pending_change_id}/withdraw`, { method: 'POST' });
    toast(d.message);
    await loadEntity();
    if (APP_ROLE === 'admin') refreshBadge();
  } catch (e) {
    toast(e.message, true);
  }
};

function selectedRows() {
  return $$('.row-select:checked').map(input => state.records[Number(input.dataset.index)]).filter(Boolean).map(stripPendingFields);
}

function updateRowSelection() {
  const selected = $$('.row-select:checked').length;
  const total = $$('.row-select:not(:disabled)').length;
  const selectAll = $('#selectAllRows');
  if (selectAll) {
    selectAll.checked = total > 0 && selected === total;
    selectAll.indeterminate = selected > 0 && selected < total;
  }
  const btn = $('#bulkDeleteBtn');
  if (btn) {
    btn.disabled = selected === 0;
    btn.textContent = selected ? `批量删除 (${selected})` : '批量删除';
  }
}

function renderFilters() {
  if (!state.meta || !$('#filterRows')) return;
  const rows = state.filters.length ? state.filters : [{ field: state.meta.headers[0], pattern: '' }];
  $('#filterRows').innerHTML = rows.map((filter, index) => `<div class="filter-row" data-index="${index}"><select>${filterFields().map(h => `<option value="${esc(h)}"${h === filter.field ? ' selected' : ''}>${esc(h)}</option>`).join('')}</select><input value="${esc(filter.pattern || '')}" placeholder="正则表达式，例如 ^Dell$"><button class="mini delete" type="button" onclick="removeFilter(${index})">删除</button></div>`).join('');
}

function filterFields() {
  return ['在途状态', ...state.meta.headers];
}

function collectFilters() {
  return $$('#filterRows .filter-row').map(row => {
    const field = row.querySelector('select')?.value || '';
    const pattern = row.querySelector('input')?.value.trim() || '';
    return { field, pattern };
  }).filter(item => item.field && item.pattern);
}

window.removeFilter = index => {
  const rows = $$('#filterRows .filter-row');
  rows[index]?.remove();
  if (!$$('#filterRows .filter-row').length) {
    state.filters = [];
    renderFilters();
  }
};

function renderSorts() {
  if (!state.meta || !$('#sortRows')) return;
  const rows = state.sorts.length ? state.sorts : [];
  $('#sortRows').innerHTML = rows.map((sort, index) => `<div class="sort-row" data-index="${index}"><select>${filterFields().map(h => `<option value="${esc(h)}"${h === sort.field ? ' selected' : ''}>${esc(h)}</option>`).join('')}</select><select><option value="asc"${sort.direction !== 'desc' ? ' selected' : ''}>升序</option><option value="desc"${sort.direction === 'desc' ? ' selected' : ''}>降序</option></select><button class="mini delete" type="button" onclick="removeSort(${index})">删除</button></div>`).join('');
}

function collectSorts() {
  return $$('#sortRows .sort-row').map(row => {
    const selects = row.querySelectorAll('select');
    return {
      field: selects[0]?.value || '',
      direction: selects[1]?.value || 'asc',
    };
  }).filter(item => item.field);
}

window.removeSort = index => {
  state.sorts = collectSorts();
  state.sorts.splice(index, 1);
  renderSorts();
};

$('#addFilterBtn').onclick = () => {
  if (!state.meta) return;
  state.filters = collectFilters();
  state.filters.push({ field: state.meta.headers[0], pattern: '' });
  renderFilters();
};

$('#addSortBtn').onclick = () => {
  if (!state.meta) return;
  state.sorts = collectSorts();
  state.sorts.push({ field: state.meta.headers[0], direction: 'asc' });
  renderSorts();
};

$('#searchBtn').onclick = async () => {
  try {
    state.filters = collectFilters();
    state.sorts = collectSorts();
    const d = await api(`/api/${state.entity}/search`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ filters: state.filters, sorts: state.sorts }) });
    state.records = d.records;
    state.meta = d.meta;
    renderTable();
  } catch (e) {
    toast(e.message, true);
  }
};

$('#resetSearchBtn').onclick = async () => {
  state.filters = [];
  state.sorts = [];
  renderFilters();
  renderSorts();
  await loadEntity();
};

async function exportFiltered(format) {
  try {
    state.filters = collectFilters();
    state.sorts = collectSorts();
    const response = await fetch(`/api/${state.entity}/export?format=${format}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ filters: state.filters, sorts: state.sorts }) });
    if (!response.ok) {
      let data = {};
      try { data = await response.json(); } catch {}
      throw new Error(data.error || '导出失败');
    }
    const blob = await response.blob();
    const disposition = response.headers.get('Content-Disposition') || '';
    const match = disposition.match(/filename\*=UTF-8''([^;]+)|filename="?([^"]+)"?/);
    const name = decodeURIComponent(match?.[1] || match?.[2] || `${state.meta.label}-检索结果.${format}`);
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    toast(e.message, true);
  }
}

$('#exportXlsxBtn').onclick = () => exportFiltered('xlsx');
$('#exportCsvBtn').onclick = () => exportFiltered('csv');

// 导出/模板下拉菜单开关
const exportMenuBtn = $('#exportMenuBtn');
const exportDropdown = $('#exportDropdown');
if (exportMenuBtn && exportDropdown) {
  exportMenuBtn.onclick = e => {
    e.stopPropagation();
    exportDropdown.classList.toggle('open');
  };
  document.addEventListener('click', e => {
    if (exportDropdown && !exportDropdown.contains(e.target)) exportDropdown.classList.remove('open');
  });
  exportDropdown.querySelectorAll('.dropdown-menu button, .dropdown-menu a').forEach(el => {
    el.addEventListener('click', () => exportDropdown.classList.remove('open'));
  });
}

function slotNumbers(value) {
  const match = String(value || '').match(/^(\d+)-(\d+)$/);
  if (!match) return [];
  const start = Number(match[1]);
  const end = Number(match[2]);
  const low = Math.min(start, end);
  const high = Math.max(start, end);
  return Array.from({ length: high - low + 1 }, (_, i) => high - i);
}

function slotRangeValue(start, end) {
  const low = Math.min(start, end);
  const high = Math.max(start, end);
  return `${low}-${high}`;
}

function slotStatusText(slot) {
  const labels = {
    free: '空闲',
    occupied: slot.server || '已占用',
    pending_occupied: '待审核占用',
    pending_release: '待审核释放',
    pending_implementation: '待实施占用',
  };
  return labels[slot.status] || '空闲';
}

function isSlotSelectable(slot) {
  return slot.selectable !== false && slot.status !== 'occupied' && slot.status !== 'pending_occupied' && slot.status !== 'pending_implementation';
}

function slotSelectableForRequest(slot, requestId) {
  return isSlotSelectable(slot) || (requestId && slot.request_id === requestId);
}

function slotInfoText(slot) {
  if (!slot || !slot.server) return slot && slot.status !== 'free' ? slotStatusText(slot) : '';
  const label = slot.status === 'occupied' ? '已占用' : slotStatusText(slot);
  return `${label}：${slot.server}`;
}

async function loadCabinetOptions(row) {
  const pendingBase = state.pendingEditing?._pending_operation === '修改' ? stripPendingFields(state.pendingEditing) : null;
  const exclude = state.entity === 'servers' ? (state.editing?.['序列号'] || pendingBase?.['序列号'] || '') : '';
  const suffix = exclude ? `?exclude_server=${encodeURIComponent(exclude)}` : '';
  state.cabinetOptions = await api(`/api/cabinet-options${suffix}`);
}

function isKeyReadonlyInForm(field) {
  const editingExisting = state.editing || state.pendingEditing?._pending_operation === '修改';
  return editingExisting && state.entity !== 'customers' && state.meta.key.includes(field);
}

function renderServerForm(row) {
  const special = new Set(['所在机房', '所属机柜', '所属机位']);
  const normal = state.meta.headers.filter(h => !special.has(h));
  const rooms = state.cabinetOptions.rooms || [];
  const initialRoom = rooms.find(x => x.name === row['所在机房'])?.name || rooms[0]?.name || '';
  const cabinets = rooms.find(x => x.name === initialRoom)?.cabinets || [];
  const initialCabinet = cabinets.find(x => x.name === row['所属机柜'])?.name || cabinets[0]?.name || '';
  state.slotPick = { start: null, end: null };
  const selected = slotNumbers(row['所属机位']);
  if (selected.length) {
    state.slotPick.start = Math.max(...selected);
    state.slotPick.end = Math.min(...selected);
  }
  $('#formFields').innerHTML = normal.map(h => {
    const readonly = isKeyReadonlyInForm(h) ? ' readonly' : '';
    if (h === '客户id' || h === '客户名称') {
      return `<label>${esc(h)}${h === '客户id' ? ' *' : ''}${customerInput(h, row[h] || '', readonly)}</label>`;
    }
    if (h === '负责人') {
      return `<label>${esc(h)}<span class="owner-field"><input name="负责人" value="${esc(row['负责人'] || '')}"><button class="mini sync-owner" type="button" id="syncOwnerBtn" title="按当前客户从客户表同步负责人">自动同步负责人</button></span></label>`;
    }
    return `<label>${esc(h)}${state.meta.key.includes(h) ? ' *' : ''}<input name="${esc(h)}" value="${esc(row[h] || '')}"${readonly}></label>`;
  }).join('') + `<div class="server-location">
    <label>所在机房 *<select id="serverRoomSelect" name="所在机房"></select></label>
    <label>所属机柜 *<select id="serverCabinetSelect" name="所属机柜"></select></label>
    <label class="slot-value">所属机位 *<input id="serverSlotInput" name="所属机位" value="${esc(row['所属机位'] || '')}" readonly></label>
    <div class="slot-picker" id="serverSlotPicker"></div>
  </div>`;
  const roomSelect = $('#serverRoomSelect');
  const cabinetSelect = $('#serverCabinetSelect');
  roomSelect.innerHTML = rooms.map(room => `<option value="${esc(room.name)}"${room.name === initialRoom ? ' selected' : ''}>${esc(room.name)}</option>`).join('');
  function renderCabinetSelect(selectedName = '') {
    const room = rooms.find(x => x.name === roomSelect.value);
    const list = room?.cabinets || [];
    const selectedNameOrFirst = list.find(x => x.name === selectedName)?.name || list[0]?.name || '';
    cabinetSelect.innerHTML = list.map(cabinet => `<option value="${esc(cabinet.name)}"${cabinet.name === selectedNameOrFirst ? ' selected' : ''}>${esc(cabinet.name)}</option>`).join('');
    renderSlotPicker();
  }
  function renderSlotPicker() {
    const room = rooms.find(x => x.name === roomSelect.value);
    const cabinet = room?.cabinets.find(x => x.name === cabinetSelect.value);
    const start = state.slotPick.start;
    const end = state.slotPick.end ?? state.slotPick.start;
    const selectedLow = start && end ? Math.min(start, end) : null;
    const selectedHigh = start && end ? Math.max(start, end) : null;
    $('#serverSlotPicker').innerHTML = cabinet ? cabinet.slots.map(slot => {
      const num = Number(slot.slot);
      const selectedClass = selectedLow !== null && num >= selectedLow && num <= selectedHigh ? ' selected' : '';
      const locked = isSlotSelectable(slot) ? '' : ' disabled';
      const info = slotInfoText(slot);
      return `<button class="slot-choice${selectedClass}${locked}" type="button" data-slot="${esc(slot.slot)}" data-slot-info="${esc(info)}" aria-disabled="${locked ? 'true' : 'false'}"><b>${esc(slot.slot)}</b><span>${esc(slotStatusText(slot))}</span></button>`;
    }).join('') : '<div class="empty">请先维护机柜信息</div>';
    $$('#serverSlotPicker .slot-choice').forEach(btn => {
      if (btn.classList.contains('disabled')) {
        bindSlotInfoTooltip(btn, btn.dataset.slotInfo);
        return;
      }
      btn.onclick = () => chooseSlot(Number(btn.dataset.slot));
    });
  }
  function chooseSlot(slot) {
    const room = rooms.find(x => x.name === roomSelect.value);
    const cabinet = room?.cabinets.find(x => x.name === cabinetSelect.value);
    if (!cabinet) return;
    if (!state.slotPick.start || state.slotPick.end) {
      state.slotPick = { start: slot, end: null };
      $('#serverSlotInput').value = slotRangeValue(slot, slot);
      renderSlotPicker();
      return;
    }
    const low = Math.min(state.slotPick.start, slot);
    const high = Math.max(state.slotPick.start, slot);
    const busy = cabinet.slots.find(item => {
      const num = Number(item.slot);
      return num >= low && num <= high && !isSlotSelectable(item);
    });
    if (busy) {
      toast(`所选范围包含${slotStatusText(busy)}机位 ${busy.slot}`, true);
      return;
    }
    state.slotPick.end = slot;
    $('#serverSlotInput').value = slotRangeValue(state.slotPick.start, state.slotPick.end);
    renderSlotPicker();
  }
  roomSelect.onchange = () => {
    state.slotPick = { start: null, end: null };
    $('#serverSlotInput').value = '';
    renderCabinetSelect();
  };
  cabinetSelect.onchange = () => {
    state.slotPick = { start: null, end: null };
    $('#serverSlotInput').value = '';
    renderSlotPicker();
  };
  renderCabinetSelect(initialCabinet);
  bindCustomerInputs($('#formFields'));
}

async function openModal(mode, row = {}) {
  if (mode !== 'pending') state.pendingEditing = null;
  state.editing = mode === 'edit' ? row : null;
  const titles = { edit: '修改', add: '新增', pending: '编辑待审批' };
  $('#modalTitle').textContent = (titles[mode] || '新增') + state.meta.label;
  $('#modalEyebrow').textContent = mode === 'pending' ? 'PENDING CHANGE' : mode === 'edit' ? 'EDIT RECORD' : 'NEW RECORD';
  if (state.entity === 'servers') {
    try {
      await loadCustomerOptions();
      await loadCabinetOptions(row);
      renderServerForm(row);
    } catch (e) {
      toast(e.message, true);
      return;
    }
  } else {
    $('#formFields').innerHTML = state.meta.headers.map(h => {
      const readonly = isKeyReadonlyInForm(h) ? ' readonly' : '';
      return `<label>${esc(h)}${state.meta.key.includes(h) ? ' *' : ''}<input name="${esc(h)}" value="${esc(row[h] || '')}"${readonly}></label>`;
    }).join('');
  }
  $('#modal').classList.add('open');
}

window.openEdit = s => openModal('edit', JSON.parse(decodeURIComponent(s)));
$('#addBtn').onclick = () => openModal('add');

function closeModal() {
  $('#modal').classList.remove('open');
  state.pendingEditing = null;
}

$('#closeModal').onclick = closeModal;
$('#cancelModal').onclick = closeModal;

$('#recordForm').onsubmit = async e => {
  e.preventDefault();
  const row = Object.fromEntries(new FormData(e.target));
  try {
    const pending = state.pendingEditing;
    const url = pending ? `/api/changes/${pending._pending_change_id}/update-pending` : state.editing ? `/api/${state.entity}/update` : `/api/${state.entity}/add`;
    const body = pending ? { new_data: row } : state.editing ? { old: state.editing, new: row } : row;
    const d = await api(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    toast(d.message);
    closeModal();
    await loadEntity();
    if (APP_ROLE === 'admin') refreshBadge();
  } catch (err) {
    toast(err.message, true);
  }
};

window.removeRow = async s => {
  const row = JSON.parse(decodeURIComponent(s));
  if (!confirm('确定提交删除申请？审核通过后才会从正式 Excel 删除。')) return;
  try {
    const d = await api(`/api/${state.entity}/delete`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(row) });
    toast(d.message);
    await loadEntity();
    if (APP_ROLE === 'admin') refreshBadge();
  } catch (e) {
    toast(e.message, true);
  }
};

$('#bulkDeleteBtn').onclick = async () => {
  const rows = selectedRows();
  if (!rows.length) return;
  if (!confirm(`确定提交 ${rows.length} 条删除申请？审核通过后才会从正式 Excel 删除。`)) return;
  try {
    const d = await api(`/api/${state.entity}/delete-batch`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ records: rows }) });
    toast(d.message);
    await loadEntity();
    if (APP_ROLE === 'admin') refreshBadge();
  } catch (e) {
    toast(e.message, true);
  }
};

$('#importInput').onchange = async e => {
  const f = e.target.files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append('file', f);
  const mode = $('#importModeSelect')?.value || 'full';
  fd.append('mode', mode);
  try {
    const d = await api(`/api/${state.entity}/import`, { method: 'POST', body: fd });
    toast(d.message);
    if (APP_ROLE === 'admin') refreshBadge();
  } catch (err) {
    if (err.data && Array.isArray(err.data.details) && err.data.details.length) {
      alert(`${err.message}\n\n${formatImportErrorDetails(err.data.details)}`);
    }
    toast(err.message, true);
  } finally {
    e.target.value = '';
  }
};

$$('.filter').forEach(b => b.onclick = () => {
  if (isRackReviewMode() && b.dataset.status !== 'pending') return;
  state.status = b.dataset.status;
  updateReviewControls();
  loadChanges();
});

function renderReviewFilters() {
  const container = $('#reviewFilterRows');
  if (!container) return;
  const rows = state.reviewFilters.length ? state.reviewFilters : [{ field: RACK_REVIEW_FILTER_FIELDS[0], pattern: '' }];
  container.innerHTML = rows.map((filter, index) => `<div class="filter-row review-filter-row" data-index="${index}"><select>${RACK_REVIEW_FILTER_FIELDS.map(field => `<option value="${esc(field)}"${field === filter.field ? ' selected' : ''}>${esc(field)}</option>`).join('')}</select><input value="${esc(filter.pattern || '')}" placeholder="输入筛选内容"><button class="mini delete" type="button" onclick="removeReviewFilter(${index})">删除</button></div>`).join('');
}

function collectReviewFilters() {
  return $$('#reviewFilterRows .review-filter-row').map(row => {
    const field = row.querySelector('select')?.value || '';
    const pattern = row.querySelector('input')?.value.trim() || '';
    return { field, pattern };
  }).filter(item => item.field && item.pattern);
}

window.removeReviewFilter = index => {
  state.reviewFilters = collectReviewFilters();
  state.reviewFilters.splice(index, 1);
  renderReviewFilters();
  if (isRackReviewMode() && state.status === 'pending') renderRackReviewList();
};

function rackReviewSearchValue(item, field) {
  const data = item.request_data || {};
  const stageData = item.stage_data || {};
  return String(stageData[field] ?? data[field] ?? item[field] ?? '');
}

function reviewFilteredRackRequests() {
  const filters = collectReviewFilters();
  state.reviewFilters = filters;
  if (!filters.length) return state.rackRequests;
  return state.rackRequests.filter(item => filters.every(filter => rackReviewSearchValue(item, filter.field).includes(filter.pattern)));
}

function renderRackReviewList() {
  const rows = reviewFilteredRackRequests();
  renderReviewBulkBar();
  $('#reviewList').innerHTML = rows.length ? rows.map(rackReviewCard).join('') : '<div class="card empty">当前没有记录</div>';
  hydrateRackApprovalForms();
  bindRackReviewExpand();
  $$('.rack-select').forEach(input => input.onchange = updateChangeSelection);
  updateChangeSelection();
  if ($('#pendingBadge') && state.status === 'pending') $('#pendingBadge').textContent = rows.length;
}

async function loadChanges() {
  updateReviewControls();
  if (isRackReviewMode()) {
    return loadRackReviewChanges();
  }
  if (state.status === 'versions') {
    const d = await api('/api/versions');
    state.changes = [];
    renderReviewBulkBar();
    $('#reviewList').innerHTML = d.versions.length ? d.versions.map(versionCard).join('') : '<div class="card empty">尚未生成版本</div>';
    return;
  }
  const d = await api(`/api/changes?status=${state.status}`);
  state.changes = d.changes;
  renderReviewBulkBar();
  $('#reviewList').innerHTML = d.changes.length ? d.changes.map(changeCard).join('') : '<div class="card empty">当前没有记录</div>';
  $$('.change-select').forEach(input => input.onchange = updateChangeSelection);
  updateChangeSelection();
  if (state.status === 'pending') $('#pendingBadge').textContent = d.changes.length;
}

async function loadRackReviewChanges() {
  const d = state.reviewMode === 'allocation'
    ? await api(`/api/rack-requests?status=${state.status}&stage=allocation`)
    : await api(`/api/rack-requests?status=${state.status}&stage=implementation`);
  state.rackRequests = d.requests;
  await loadCustomerOptions();
  await loadRackApprovalOptions();
  renderRackReviewList();
}

async function loadRackApprovalOptions() {
  const needsOptions = state.status === 'pending' && state.rackRequests.some(item => item.request_type === '上架');
  state.rackApprovalOptions = needsOptions ? await api('/api/cabinet-options') : { rooms: [] };
}

function ensureBulkExportButton() {
  let btn = $('#bulkExportBtn');
  if (btn) return btn;
  const actions = $('.bulk-bar .bulk-actions');
  if (!actions) return null;
  btn = document.createElement('button');
  btn.id = 'bulkExportBtn';
  btn.className = 'btn ghost';
  btn.type = 'button';
  btn.disabled = true;
  btn.textContent = '导出 xlsx';
  btn.onclick = exportRackRequests;
  actions.appendChild(btn);
  return btn;
}

function renderReviewBulkBar() {
  const bar = $('#reviewBulkBar');
  if (!bar) return;
  bar.style.display = state.status === 'pending' ? 'flex' : 'none';
  const rackMode = isRackReviewMode();
  const rejectBtn = $('#bulkRejectBtn');
  const approveBtn = $('#bulkApproveBtn');
  const exportBtn = ensureBulkExportButton();
  if (rejectBtn) rejectBtn.style.display = rackMode ? 'none' : '';
  if (approveBtn) approveBtn.style.display = rackMode ? 'none' : '';
  if (exportBtn) exportBtn.style.display = rackMode ? '' : 'none';
  updateChangeSelection();
}

function pretty(o) {
  if (!o) return '<span class="muted">无</span>';
  return Object.entries(o).map(([k, v]) => `<b>${esc(k)}</b>：${esc(v) || '-'}`).join('<br>');
}

function rackApprovalFormById(id) {
  return document.querySelector(`.rack-approval-form[data-rack-approval-id="${id}"]`);
}

function collectRackApprovalData(id) {
  const form = rackApprovalFormById(id);
  return form ? Object.fromEntries([...form.querySelectorAll('input,select')].map(input => [input.name, input.value.trim()])) : {};
}

function hydrateRackApprovalForms() {
  $$('.rack-approval-form').forEach(form => {
    const id = form.dataset.rackApprovalId;
    form.onsubmit = event => event.preventDefault();
    const cabinet = form.querySelector('[data-rack-cabinet]');
    const slotValue = form.querySelector('[data-rack-slot-input]')?.value || '';
    const selectedSlots = slotNumbers(slotValue);
    state.rackSlotPick[id] = selectedSlots.length
      ? { start: Math.max(...selectedSlots), end: Math.min(...selectedSlots) }
      : { start: null, end: null };
    if (cabinet) cabinet.onchange = () => {
      state.rackSlotPick[id] = { start: null, end: null };
      const slotInput = form.querySelector('[data-rack-slot-input]');
      if (slotInput) slotInput.value = '';
      renderRackSlotPicker(id);
    };
    bindCustomerInputs(form);
    renderRackSlotPicker(id);
  });
  $$('[data-rack-approval-id] [data-rack-action]').forEach(button => {
    const id = button.closest('[data-rack-approval-id]')?.dataset.rackApprovalId;
    if (id) button.onclick = () => reviewRackRequest(id, button.dataset.rackAction);
  });
  $$('[data-rack-approval-id] [data-rack-auto-ip]').forEach(button => {
    const id = button.closest('[data-rack-approval-id]')?.dataset.rackApprovalId;
    if (id) button.onclick = () => autoAssignRackIps(id);
  });
}

function renderRackSlotPicker(id) {
  const form = rackApprovalFormById(id);
  if (!form) return;
  const item = state.rackRequests.find(row => row.id === id);
  const cabinetName = form.querySelector('[data-rack-cabinet]')?.value || '';
  const cabinet = roomCabinetsForRackRequest(item || {}).find(row => row.name === cabinetName);
  const picker = form.querySelector('[data-rack-slot-picker]');
  const pick = state.rackSlotPick[id] || { start: null, end: null };
  const end = pick.end ?? pick.start;
  const selectedLow = pick.start && end ? Math.min(pick.start, end) : null;
  const selectedHigh = pick.start && end ? Math.max(pick.start, end) : null;
  if (!picker) return;
  picker.innerHTML = cabinet ? cabinet.slots.map(slot => {
    const num = Number(slot.slot);
    // 具体实施阶段：本请求已分配的机位视为可选（绿色），允许保持或调整
    const view = slot.request_id === id ? { ...slot, status: 'free', server: '', selectable: true } : slot;
    const selected = selectedLow !== null && num >= selectedLow && num <= selectedHigh ? ' selected' : '';
    const locked = slotSelectableForRequest(slot, id) ? '' : ' disabled';
    const info = slotInfoText(view);
    return `<button class="slot-choice${selected}${locked}" type="button" data-rack-slot="${esc(slot.slot)}" data-slot-info="${esc(info)}" aria-disabled="${locked ? 'true' : 'false'}"><b>${esc(slot.slot)}</b><span>${esc(slotStatusText(view))}</span></button>`;
  }).join('') : '<div class="empty">该机房暂无可分配机柜</div>';
  picker.querySelectorAll('[data-rack-slot]').forEach(btn => {
    if (btn.classList.contains('disabled')) {
      bindSlotInfoTooltip(btn, btn.dataset.slotInfo);
      return;
    }
    btn.onclick = () => chooseRackSlot(id, Number(btn.dataset.rackSlot));
  });
}

function chooseRackSlot(id, slot) {
  const form = rackApprovalFormById(id);
  const item = state.rackRequests.find(row => row.id === id);
  const cabinetName = form?.querySelector('[data-rack-cabinet]')?.value || '';
  const cabinet = roomCabinetsForRackRequest(item || {}).find(row => row.name === cabinetName);
  if (!form || !cabinet) return;
  const pick = state.rackSlotPick[id] || { start: null, end: null };
  if (!pick.start || pick.end) {
    state.rackSlotPick[id] = { start: slot, end: null };
    form.querySelector('[data-rack-slot-input]').value = slotRangeValue(slot, slot);
    renderRackSlotPicker(id);
    return;
  }
  const low = Math.min(pick.start, slot);
  const high = Math.max(pick.start, slot);
  const busy = cabinet.slots.find(item => {
    const num = Number(item.slot);
    return num >= low && num <= high && !slotSelectableForRequest(item, id);
  });
  if (busy) {
    toast(`所选范围包含${slotStatusText(busy)}机位 ${busy.slot}`, true);
    return;
  }
  state.rackSlotPick[id] = { start: pick.start, end: slot };
  form.querySelector('[data-rack-slot-input]').value = slotRangeValue(pick.start, slot);
  renderRackSlotPicker(id);
}

async function autoAssignRackIps(id) {
  const form = rackApprovalFormById(id);
  if (!form) return;
  const body = {
    request_id: id,
    '所在机房': form.elements['所在机房']?.value || '',
    '所属机柜': form.elements['所属机柜']?.value || '',
  };
  try {
    const data = await api('/api/rack-requests/auto-ip', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    Object.entries(data.ips || {}).forEach(([field, value]) => {
      if (form.elements[field]) form.elements[field].value = value;
    });
    toast('自动分配 IP 已完成');
  } catch (e) {
    toast(e.message, true);
  }
}

function reviewEditor(c) {
  if (c.operation === '删除') return '<span class="muted">无</span>';
  if (c.status !== 'pending') return pretty(c.new_data);
  return `<div class="review-edit" data-change-id="${esc(c.id)}">${c.meta.headers.map(h => {
    const readonly = c.entity !== 'customers' && c.meta.key.includes(h) ? ' readonly' : '';
    return `<label>${esc(h)}${readonly ? ' *' : ''}<input name="${esc(h)}" value="${esc((c.new_data || {})[h] || '')}"${readonly}></label>`;
  }).join('')}</div>`;
}

function collectReviewData(id) {
  const form = document.querySelector(`.review-edit[data-change-id="${id}"]`);
  return form ? Object.fromEntries([...form.querySelectorAll('input')].map(input => [input.name, input.value])) : undefined;
}

function selectedChangeIds() {
  return $$('.change-select:checked').map(input => input.dataset.id).filter(Boolean);
}

function selectedRackIds() {
  return $$('.rack-select:checked').map(input => input.dataset.id).filter(Boolean);
}

function activeSelectionInputs() {
  return isRackReviewMode() ? $$('.rack-select') : $$('.change-select');
}

function updateChangeSelection() {
  const inputs = activeSelectionInputs();
  const selected = inputs.filter(input => input.checked).length;
  const total = inputs.length;
  const selectAll = $("#selectAllChanges");
  if (selectAll) {
    selectAll.checked = total > 0 && selected === total;
    selectAll.indeterminate = selected > 0 && selected < total;
  }
  const count = $('#selectedChangeCount');
  if (count) count.textContent = `已选 ${selected} 条`;
  ['#bulkRejectBtn', '#bulkApproveBtn', '#bulkExportBtn'].forEach(selector => {
    const btn = $(selector);
    if (btn) btn.disabled = selected === 0;
  });
}

function changeCard(c) {
  const actions = c.status === 'pending' ? `<div class="review-actions"><button class="btn danger" onclick="review('${c.id}','reject')">驳回</button><button class="btn primary" onclick="review('${c.id}','approve')">通过并合并</button></div>` : '';
  const selector = c.status === 'pending' ? `<label class="change-picker"><input class="change-select" type="checkbox" data-id="${esc(c.id)}"> 选择</label>` : '';
  return `<article class="review-card">${selector}<div class="review-top"><span class="tag ${c.operation === '删除' ? 'delete' : ''}">${esc(c.operation)}</span><strong>${esc(c.entity_label)} · ${esc(c.record_key)}</strong><span class="review-time">${esc(c.submitter)} · ${esc(c.submitted_at)}</span></div><div class="diff"><div class="diff-box"><b>当前正式数据</b>${pretty(c.current_data)}</div><div class="diff-arrow">→</div><div class="diff-box"><b>审批后数据</b>${reviewEditor(c)}</div></div>${c.reject_reason ? `<p class="muted">驳回原因：${esc(c.reject_reason)}</p>` : ''}${actions}</article>`;
}

function versionCard(v) {
  return `<article class="review-card"><div class="review-top"><span class="tag">${esc(v.version_code)}</span><strong>${esc(v.entity_label)} · ${esc(v.operation)} · ${esc(v.record_key)}</strong><span class="review-time">${esc(v.reviewer)} · ${esc(v.created_at)}</span></div><p class="muted">对应审核申请：${esc(v.change_id)}。该版本完整保存了当时的三份正式 Excel。</p><div class="review-actions"><a class="btn ghost" href="/api/versions/${v.id}/cabinets/download">下载机柜表</a><a class="btn ghost" href="/api/versions/${v.id}/customers/download">下载客户表</a><a class="btn primary" href="/api/versions/${v.id}/servers/download">下载服务器表</a></div></article>`;
}

function serverDraftFromRackRequest(item) {
  const data = item.request_data || {};
  const stageData = item.stage_data || {};
  return {
    ...data,
    ...stageData,
    '客户id': stageData['客户id'] || data['客户id'] || '',
    '客户名称': stageData['客户名称'] || data['客户名称'] || '',
    '所在机房': stageData['所在机房'] || data['所在机房'] || '',
    '所属机柜': stageData['所属机柜'] || '',
    '所属机位': stageData['所属机位'] || '',
  };
}

function roomCabinetsForRackRequest(item) {
  const roomName = item.stage_data?.['所在机房'] || item['所在机房'] || item.request_data?.['所在机房'] || '';
  const room = (state.rackApprovalOptions.rooms || []).find(x => x.name === roomName);
  return room?.cabinets || [];
}

function rackApprovalForm(item) {
  if (item.status !== 'pending' || item.request_type !== '上架') return '';
  const draft = serverDraftFromRackRequest(item);
  const textFields = SERVER_APPROVAL_HEADERS.filter(h => !['所在机房', '所属机柜', '所属机位'].includes(h));
  const cabinets = roomCabinetsForRackRequest(item);
  const cabinetOptions = cabinets.map(cabinet => `<option value="${esc(cabinet.name)}"${cabinet.name === draft['所属机柜'] ? ' selected' : ''}>${esc(cabinet.name)}</option>`).join('');
  const fields = textFields.map(field => {
    const readonly = !isAdmin() && ['序列号', '客户id', '客户名称'].includes(field) ? ' readonly' : '';
    if (field === '客户id' || field === '客户名称') {
      return `<label>${esc(field)}${customerInput(field, draft[field] || '', readonly)}</label>`;
    }
    return `<label>${esc(field)}<input name="${esc(field)}" value="${esc(draft[field] || '')}"${readonly}></label>`;
  }).join('');
  const approveText = state.reviewMode === 'allocation' ? '完成服务器机位IP分配' : '完成具体实施';
  const autoIpButton = state.reviewMode === 'allocation' ? '<button class="btn ghost" type="button" data-rack-auto-ip>自动分配 IP</button>' : '';
  const roomReadonly = !isAdmin() ? ' readonly' : '';
  const slotReadonly = !isAdmin() ? ' readonly' : '';
  return `<form class="rack-approval-form form-grid" data-rack-approval-id="${esc(item.id)}">
    ${fields}
    <label>所在机房<input name="所在机房" value="${esc(draft['所在机房'])}"${roomReadonly}></label>
    <label>所属机柜 *<select name="所属机柜" data-rack-cabinet>${cabinetOptions}</select></label>
    <label>所属机位 *<input name="所属机位" value="${esc(draft['所属机位'])}" data-rack-slot-input${slotReadonly}></label>
    <div class="slot-picker" data-rack-slot-picker></div>
    <div class="review-actions wide">${autoIpButton}<button class="btn danger" type="button" data-rack-action="reject">驳回</button><button class="btn primary" type="button" data-rack-action="approve">${approveText}</button></div>
  </form>`;
}

function rackRequestDetails(item) {
  const fields = ['上下架需求', '防火墙白名单', '是否重装系统', '初始密码', '备注'];
  const data = item.request_data || {};
  const details = Object.fromEntries(fields.map(field => [field, data[field] || '']).filter(([, value]) => String(value).trim()));
  return pretty(details);
}

function rackReviewCard(item) {
  const actions = item.status === 'pending' && item.request_type !== '上架' ? `<div class="review-actions" data-rack-approval-id="${esc(item.id)}"><button class="btn danger" type="button" data-rack-action="reject">驳回</button><button class="btn primary" type="button" data-rack-action="approve">完成具体实施</button></div>` : '';
  const picker = item.status === 'pending' ? `<label class="change-picker"><input class="rack-select" type="checkbox" data-id="${esc(item.id)}"> 选择</label>` : '';
  const customer = item['客户名称'] || item['客户id'] || '';
  const expanded = state.rackReviewExpanded.includes(item.id);
  const head = `<div class="review-top rack-request-head" data-rack-expand-id="${esc(item.id)}" title="点击展开/收起"><span class="tag">${esc(item.request_type)}</span><strong>${esc(item['序列号'])} · ${esc(item['所在机房'] || '未指定机房')}${customer ? ' · ' + esc(customer) : ''}</strong><span class="rack-caret">${expanded ? '▾' : '▸'}</span></div>`;
  const body = `<div class="rack-request-body"${expanded ? '' : ' hidden'}><div class="diff compact"><div class="diff-box"><b>原请求补充信息</b>${rackRequestDetails(item)}</div></div>${rackApprovalForm(item)}${item.reject_reason ? `<p class="muted">驳回原因：${esc(item.reject_reason)}</p>` : ''}${actions}</div>`;
  return `<article class="review-card rack-request-card">${picker}${head}${body}</article>`;
}

function rackSubmitterCard(item) {
  const encoded = encodeURIComponent(JSON.stringify(item.request_data || {}));
  const submitText = item.status === 'rejected' ? '修改后重新提交' : '修改';
  const actions = item.status === 'pending'
    ? `<div class="review-actions"><button class="btn ghost" onclick="editRackRequest('${item.id}','${encoded}')">${submitText}</button><button class="btn danger" onclick="withdrawRackRequest('${item.id}')">撤回</button></div>`
    : item.status === 'rejected'
      ? `<div class="review-actions"><button class="btn primary" onclick="editRackRequest('${item.id}','${encoded}')">${submitText}</button></div>`
      : '';
  const customer = item['客户名称'] || item['客户id'] || '';
  const expanded = state.rackRequestExpanded.includes(item.id);
  const head = `<div class="review-top rack-request-head" data-rack-expand-id="${esc(item.id)}" title="点击展开/收起"><span class="tag">${esc(item.request_type)}</span><strong>${esc(item['序列号'])} · ${esc(item['所在机房'] || '未指定机房')}${customer ? ' · ' + esc(customer) : ''}</strong><span class="review-time">${esc(item.submitted_at)}</span><span class="rack-caret">${expanded ? '▾' : '▸'}</span></div>`;
  const body = `<div class="rack-request-body"${expanded ? '' : ' hidden'}><div class="diff"><div class="diff-box"><b>请求内容</b>${pretty(item.request_data)}</div></div>${item.reject_reason ? `<p class="muted">驳回原因：${esc(item.reject_reason)}</p>` : ''}${actions}</div>`;
  return `<article class="review-card rack-request-card">${head}${body}</article>`;
}

function bindExpandToggles(selector, listKey) {
  $$(selector).forEach(head => {
    head.onclick = () => {
      const id = head.dataset.rackExpandId;
      const body = head.parentElement.querySelector('.rack-request-body');
      const caret = head.querySelector('.rack-caret');
      body.hidden = !body.hidden;
      caret.textContent = body.hidden ? '▸' : '▾';
      if (body.hidden) state[listKey] = state[listKey].filter(x => x !== id);
      else if (!state[listKey].includes(id)) state[listKey].push(id);
    };
  });
}

function bindRackRequestExpand() {
  bindExpandToggles('#rackRequestList .rack-request-head', 'rackRequestExpanded');
}

function bindRackReviewExpand() {
  bindExpandToggles('#reviewList .rack-request-head', 'rackReviewExpanded');
}

function renderRackRequestFilters() {
  const container = $('#rackRequestFilterRows');
  if (!container) return;
  const rows = state.rackRequestFilters.length ? state.rackRequestFilters : [{ field: RACK_REVIEW_FILTER_FIELDS[0], pattern: '' }];
  container.innerHTML = rows.map((filter, index) => `<div class="filter-row rack-request-filter-row" data-index="${index}"><select>${RACK_REVIEW_FILTER_FIELDS.map(field => `<option value="${esc(field)}"${field === filter.field ? ' selected' : ''}>${esc(field)}</option>`).join('')}</select><input value="${esc(filter.pattern || '')}" placeholder="输入筛选内容"><button class="mini delete" type="button" onclick="removeRackRequestFilter(${index})">删除</button></div>`).join('');
}

function collectRackRequestFilters() {
  return $$('#rackRequestFilterRows .rack-request-filter-row').map(row => {
    const field = row.querySelector('select')?.value || '';
    const pattern = row.querySelector('input')?.value.trim() || '';
    return { field, pattern };
  }).filter(item => item.field && item.pattern);
}

window.removeRackRequestFilter = index => {
  state.rackRequestFilters = collectRackRequestFilters();
  state.rackRequestFilters.splice(index, 1);
  renderRackRequestFilters();
  renderRackRequestList();
};

function rackRequestSearchValue(item, field) {
  const data = item.request_data || {};
  return String(data[field] ?? item[field] ?? '');
}

function filteredRackRequests() {
  const filters = collectRackRequestFilters();
  state.rackRequestFilters = filters;
  if (!filters.length) return state.rackRequests;
  return state.rackRequests.filter(item => filters.every(filter => rackRequestSearchValue(item, filter.field).includes(filter.pattern)));
}

function rackRequestEmptyText() {
  if (state.rackRequestStatus === 'rejected') return '当前没有已驳回上下架请求';
  if (state.rackRequestStatus === 'approved') return '当前没有已通过上下架请求';
  return '当前没有待审核上下架请求';
}

function renderRackRequestList() {
  const rows = filteredRackRequests();
  $('#rackRequestList').innerHTML = rows.length ? rows.map(rackSubmitterCard).join('') : `<div class="card empty">${rackRequestEmptyText()}</div>`;
  bindRackRequestExpand();
}

function updateRackRequestStatusTabs() {
  $$('[data-rack-request-status]').forEach(button => {
    button.classList.toggle('active', button.dataset.rackRequestStatus === state.rackRequestStatus);
  });
  const rejected = state.rackRequestStatus === 'rejected';
  const approved = state.rackRequestStatus === 'approved';
  const title = $('#rackRequestListTitle');
  const hint = $('#rackRequestListHint');
  if (title) title.textContent = approved ? '我的已通过请求' : rejected ? '我的已驳回请求' : '我的待审核请求';
  if (hint) hint.textContent = approved ? '仅展示当前用户提交且已通过的上下架请求' : rejected ? '修改后可重新提交，重新提交后会再次进入审核流程' : '仅展示当前用户提交且尚未进入审批流的请求';
  const searchPanel = $('#rackRequestSearchPanel');
  if (searchPanel) $('#rackRequestSearchPanel').style.display = 'flex';
  renderRackRequestFilters();
}

async function loadRackRequests() {
  if (!$('#rackRequestList')) return;
  await loadCustomerOptions();
  updateRackRequestStatusTabs();
  const d = await api(`/api/rack-requests?status=${state.rackRequestStatus}&scope=submitted`);
  state.rackRequests = d.requests;
  renderRackRequestList();
}

async function autofillRackOffFields() {
  const form = $('#rackRequestForm');
  if (!form || form.elements['是否上架']?.value !== '否') return;
  const serial = form.elements['序列号']?.value.trim();
  if (!serial) return;
  try {
    const data = await api('/api/servers');
    const server = (data.records || []).find(row => row['序列号'] === serial);
    if (!server) return;
    let field = form.elements['客户id'];
    if (field) field.value = server['客户id'] || '';
    field = form.elements['客户名称'];
    if (field) field.value = server['客户名称'] || '';
    field = form.elements['所在机房'];
    if (field) field.value = server['所在机房'] || '';
  } catch (e) {
    toast(e.message, true);
  }
}

async function submitRackRequest(event) {
  event.preventDefault();
  try {
    const row = Object.fromEntries(new FormData(event.target));
    const url = state.rackEditing ? `/api/rack-requests/${state.rackEditing}/update` : '/api/rack-requests';
    const body = state.rackEditing ? { request_data: row } : row;
    const d = await api(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    toast(d.message);
    resetRackRequestForm();
    state.rackRequestStatus = 'pending';
    state.rackRequestFilters = [];
    await loadRackRequests();
  } catch (e) {
    toast(e.message, true);
  }
}

async function importRackRequests(event) {
  const input = event.target;
  const file = input.files?.[0];
  if (!file) return;
  try {
    const body = new FormData();
    body.append('file', file);
    const d = await api('/api/rack-requests/import', { method: 'POST', body });
    toast(d.message);
    resetRackRequestForm();
    await loadRackRequests();
  } catch (e) {
    if (e.data && Array.isArray(e.data.details) && e.data.details.length) {
      alert(`${e.message}\n\n${formatImportErrorDetails(e.data.details)}`);
    }
    toast(e.message, true);
  } finally {
    input.value = '';
  }
}

function resetRackRequestForm() {
  const form = $('#rackRequestForm');
  if (form) form.reset();
  state.rackEditing = null;
  const submit = $('#rackRequestSubmitBtn');
  const cancel = $('#rackRequestCancelEditBtn');
  if (submit) submit.textContent = '提交上下架请求';
  if (cancel) cancel.hidden = true;
}

function editRackRequest(id, encoded) {
  const data = JSON.parse(decodeURIComponent(encoded));
  const form = $('#rackRequestForm');
  if (!form) return;
  state.rackEditing = id;
  Object.entries(data).forEach(([key, value]) => {
    const field = form.elements[key];
    if (field) field.value = value;
  });
  $('#rackRequestSubmitBtn').textContent = state.rackRequestStatus === 'rejected' ? '重新提交上下架请求' : '保存上下架请求';
  $('#rackRequestCancelEditBtn').hidden = false;
  bindCustomerInputs(form);
  form.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function withdrawRackRequest(id) {
  if (!confirm('确认撤回这条上下架请求？撤回后需要重新提交。')) return;
  try {
    const d = await api(`/api/rack-requests/${id}/withdraw`, { method: 'POST' });
    toast(d.message);
    if (state.rackEditing === id) resetRackRequestForm();
    await loadRackRequests();
  } catch (e) {
    toast(e.message, true);
  }
}

async function reviewRackRequest(id, action) {
  let reason = '';
  const body = { action };
  if (action === 'reject') {
    reason = prompt('请输入驳回原因：') || '';
    if (!reason) return;
    body.reason = reason;
  }
  const item = state.rackRequests.find(row => row.id === id);
  if (action === 'approve' && item?.request_type === '上架') {
    body.server_data = collectRackApprovalData(id);
  }
  try {
    const d = await api(`/api/rack-requests/${id}/review`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    toast(d.message);
    await loadChanges();
  } catch (e) {
    toast(e.message, true);
  }
}

async function loadSettings() {
  if (!$('#settingsForm')) return;
  try {
    const d = await api('/api/settings');
    const s = d.settings;
    $('#backupKeepCount').value = s.backup_keep_count;
    $('#backupCommand').value = s.backup_command || '';
    $('#auditKeepCount').value = s.audit_keep_count;
    $('#notifyEnabled').checked = Number(s.notify_enabled || 0) === 1;
    $('#notifyWebhookUrl').value = s.notify_webhook_url || '';
    $('#notifyMessageType').value = s.notify_message_type || 'markdown';
  } catch (e) {
    toast(e.message, true);
  }
}

async function saveSettings(event) {
  event.preventDefault();
  try {
    const body = Object.fromEntries(new FormData(event.target));
    body.notify_enabled = $('#notifyEnabled').checked ? '1' : '0';
    const d = await api('/api/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    toast(d.message);
  } catch (e) {
    toast(e.message, true);
  }
}

async function runBackup() {
  try {
    const d = await api('/api/backups/run', { method: 'POST' });
    toast(d.message);
  } catch (e) {
    toast(e.message, true);
  }
}

async function resetFlowState() {
  if (!confirm('确认重置流程状态？这会清空操作审核、上下架请求和版本记录，但不会删除正式 Excel、设置和系统用户。')) return;
  try {
    const d = await api('/api/flow-state/reset', { method: 'POST' });
    toast(d.message);
    if (APP_ROLE === 'admin') refreshBadge();
    if (state.reviewMode) loadChanges();
  } catch (e) {
    toast(e.message, true);
  }
}

async function loadSystemUsers() {
  if (!$('#systemUsersBody')) return;
  try {
    const d = await api('/api/system-users');
    state.systemUsers = d.users;
    renderSystemUsers();
  } catch (e) {
    toast(e.message, true);
  }
}

function renderSystemUsers() {
  $('#systemUsersBody').innerHTML = state.systemUsers.map((user, index) => `<tr data-index="${index}"><td><input name="用户id" value="${esc(user['用户id'] || '')}"></td><td><input name="密码" value="${esc(user['密码'] || '')}"></td><td><select name="身份">${['管理人员','机房人员','业务人员'].map(role => `<option${role === user['身份'] ? ' selected' : ''}>${role}</option>`).join('')}</select></td><td><input name="业务单位" value="${esc(user['业务单位'] || '')}"></td><td><input name="所属机房" value="${esc(user['所属机房'] || '')}"></td><td><input type="hidden" name="api_token" value="${esc(user['api_token'] || '')}"><button class="mini delete" type="button" onclick="removeSystemUser(${index})">删除</button></td></tr>`).join('');
}

function collectSystemUsers() {
  return $$('#systemUsersBody tr').map(row => Object.fromEntries([...row.querySelectorAll('input,select')].map(input => [input.name, input.value.trim()])));
}

window.removeSystemUser = index => {
  state.systemUsers.splice(index, 1);
  renderSystemUsers();
};

async function saveSystemUsers() {
  try {
    const d = await api('/api/system-users', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ users: collectSystemUsers() }) });
    state.systemUsers = d.users;
    renderSystemUsers();
    toast(d.message);
  } catch (e) {
    toast(e.message, true);
  }
}

window.review = async (id, action) => {
  let reason = '';
  if (action === 'reject') {
    reason = prompt('请输入驳回原因：') || '';
    if (!reason) return;
  }
  if (action === 'approve' && !confirm('确认审核通过并写入正式 Excel？')) return;
  try {
    const body = { action, reason };
    const newData = action === 'approve' ? collectReviewData(id) : undefined;
    if (newData) body.new_data = newData;
    const d = await api(`/api/changes/${id}/review`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    toast(d.message);
    loadChanges();
    refreshBadge();
  } catch (e) {
    toast(e.message, true);
  }
};

if ($("#selectAllChanges")) {
  $("#selectAllChanges").onchange = e => {
    activeSelectionInputs().forEach(input => input.checked = e.target.checked);
    updateChangeSelection();
  };
}
if ($('#addReviewFilterBtn')) $('#addReviewFilterBtn').onclick = () => {
  state.reviewFilters = collectReviewFilters();
  state.reviewFilters.push({ field: RACK_REVIEW_FILTER_FIELDS[0], pattern: '' });
  renderReviewFilters();
};
if ($('#applyReviewFilterBtn')) $('#applyReviewFilterBtn').onclick = () => {
  if (isRackReviewMode() && state.status === 'pending') renderRackReviewList();
};
if ($('#clearReviewFilterBtn')) $('#clearReviewFilterBtn').onclick = () => {
  state.reviewFilters = [];
  renderReviewFilters();
  if (isRackReviewMode() && state.status === 'pending') renderRackReviewList();
};

async function bulkReview(action) {
  const ids = selectedChangeIds();
  if (!ids.length) return;
  let reason = '';
  if (action === 'reject') {
    reason = prompt('请输入批量驳回原因：') || '';
    if (!reason) return;
  }
  if (action === 'approve' && !confirm(`确认通过 ${ids.length} 条申请并写入正式 Excel？`)) return;
  try {
    const body = { ids, action, reason };
    if (action === 'approve') {
      const newData = {};
      ids.forEach(id => {
        const value = collectReviewData(id);
        if (value) newData[id] = value;
      });
      if (Object.keys(newData).length) body.new_data = newData;
    }
    const d = await api('/api/changes/review-batch', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    toast(d.message);
    loadChanges();
    refreshBadge();
  } catch (e) {
    toast(e.message, true);
  }
}

if ($('#bulkRejectBtn')) $('#bulkRejectBtn').onclick = () => bulkReview('reject');
if ($('#bulkApproveBtn')) $('#bulkApproveBtn').onclick = () => bulkReview('approve');

async function exportRackRequests() {
  const ids = selectedRackIds();
  const body = { ids, status: state.status, stage: state.reviewMode, scope: 'review' };
  try {
    const r = await fetch('/api/rack-requests/export', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!r.ok) {
      let data = {};
      try { data = await r.json(); } catch {}
      throw new Error(data.error || '导出失败');
    }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `上下架请求-${new Date().toISOString().slice(0, 19).replace(/[-:T]/g, '')}.xlsx`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    toast(ids.length ? `已导出 ${ids.length} 条` : '已导出当前全部记录');
  } catch (e) {
    toast(e.message, true);
  }
}

if ($('#bulkExportBtn')) $('#bulkExportBtn').onclick = exportRackRequests;

async function toggleTokenPop() {
  const pop = $('#tokenPop');
  const body = $('#tokenBody');
  if (!pop || !body) return;
  if (!pop.hidden) { pop.hidden = true; return; }
  body.innerHTML = '<p class="token-tip">读取中…</p>';
  pop.hidden = false;
  let token = '';
  try {
    const r = await fetch('/api/my-token');
    if (r.ok) {
      const d = await r.json();
      token = String(d.api_token || '').trim();
    }
  } catch (e) {
    token = '';
  }
  if (token) {
    body.innerHTML = `<code>${esc(token)}</code><div class="token-actions"><button class="btn ghost" type="button" id="tokenCopy">复制</button></div>`;
    const copy = $('#tokenCopy');
    if (copy) copy.onclick = async () => {
      try {
        await navigator.clipboard.writeText(token);
        toast('Token 已复制到剪贴板');
      } catch (e) {
        toast('复制失败，请手动选中复制', true);
      }
    };
  } else {
    body.innerHTML = '<p class="token-tip">暂无token,联系管理员获取</p>';
  }
}
if ($('#tokenBtn')) $('#tokenBtn').onclick = toggleTokenPop;
if ($('#tokenClose')) $('#tokenClose').onclick = () => { const p = $('#tokenPop'); if (p) p.hidden = true; };

async function refreshBadge() {
  if (APP_ROLE !== 'admin') return;
  const d = await api('/api/changes?status=pending');
  $('#pendingBadge').textContent = d.changes.length;
}

async function loadStats() {
  state.stats = await api('/api/statistics');
  renderStats();
}

function renderStats() {
  const d = state.stats;
  $('#statCards').innerHTML = [['机柜数量', d.summary.cabinet_count], ['服务器数量', d.summary.server_count], ['客户数量', d.summary.client_count]].map(x => `<div class="stat-card"><small>${x[0]}</small><b>${x[1]}</b></div>`).join('');
  const q = $('#statsSearch').value.trim().toLowerCase();
  const cabinets = d.cabinets.filter(c => JSON.stringify(c).toLowerCase().includes(q));
  $('#cabinetGrid').innerHTML = cabinets.length ? cabinets.map(c => {
    const slots = c.slots.map(s => {
      const status = slotStatusText(s);
      const pending = s.pending_server ? `<div class="server"><b>${esc(s.pending_server)}</b><div>${esc(status)}</div></div>` : '';
      const servers = s.servers.map(v => `<div class="server"><b>${esc(v['序列号'])} · ${esc(v['品牌'])} ${esc(v['型号'])}</b><div>占用机位：${esc(v['所属机位'])} · ${esc(v['管理ip'] || '无管理IP')}</div><div>客户：${esc(v['客户名称'])}</div><div>${esc(v['用途分类'])} · ${esc(v['用途'])}</div></div>`).join('');
      return `<div class="slot ${esc(s.status || 'free')}"><div class="slot-title"><b>第 ${esc(s.slot)} 行</b><span>${esc(status)}</span></div>${servers}${pending}</div>`;
    }).join('');
    return `<article class="cabinet"><div class="cabinet-head"><b>${esc(c.name || '未分配机柜')}</b><small>${esc(c.room || '未指定机房')} · ${esc(c.rack_range || c.slots.length + ' 行')}</small></div><div class="slots">${slots}</div></article>`;
  }).join('') : '<div class="card empty">没有匹配的机柜</div>';
}

const ss = $('#statsSearch');
if (ss) ss.oninput = () => state.stats && renderStats();
if ($('#rackRequestForm')) {
  const form = $('#rackRequestForm');
  loadCustomerOptions().then(() => bindCustomerInputs(form)).catch(e => toast(e.message, true));
  form.onsubmit = submitRackRequest;
  form.elements['序列号'].addEventListener('change', autofillRackOffFields);
  form.elements['是否上架'].addEventListener('change', autofillRackOffFields);
}
if ($('#rackRequestImportInput')) $('#rackRequestImportInput').onchange = importRackRequests;
if ($('#rackRequestCancelEditBtn')) $('#rackRequestCancelEditBtn').onclick = resetRackRequestForm;
$$('[data-rack-request-status]').forEach(button => {
  button.onclick = async () => {
    state.rackRequestStatus = button.dataset.rackRequestStatus;
    state.rackRequestFilters = [];
    state.rackRequestExpanded = [];
    resetRackRequestForm();
    await loadRackRequests();
  };
});
if ($('#addRackRequestFilterBtn')) $('#addRackRequestFilterBtn').onclick = () => {
  state.rackRequestFilters = collectRackRequestFilters();
  state.rackRequestFilters.push({ field: RACK_REVIEW_FILTER_FIELDS[0], pattern: '' });
  renderRackRequestFilters();
};
if ($('#applyRackRequestFilterBtn')) $('#applyRackRequestFilterBtn').onclick = () => renderRackRequestList();
if ($('#clearRackRequestFilterBtn')) $('#clearRackRequestFilterBtn').onclick = () => {
  state.rackRequestFilters = [];
  renderRackRequestFilters();
  renderRackRequestList();
};
if ($('#settingsForm')) $('#settingsForm').onsubmit = saveSettings;
if ($('#manualBackupBtn')) $('#manualBackupBtn').onclick = runBackup;
if ($('#resetFlowStateBtn')) $('#resetFlowStateBtn').onclick = resetFlowState;
if ($('#addSystemUserBtn')) $('#addSystemUserBtn').onclick = () => {
  state.systemUsers.push({ '用户id': '', '密码': '', '身份': '业务人员', '业务单位': '', '所属机房': '' });
  renderSystemUsers();
};
if ($('#saveSystemUsersBtn')) $('#saveSystemUsersBtn').onclick = saveSystemUsers;
applyCapabilityVisibility();
loadEntity().catch(e => toast(e.message, true));
if (APP_ROLE === 'admin') refreshBadge();
