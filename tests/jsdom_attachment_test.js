/**
 * 服务器资料（附件）前端验证（jsdom，提取 app.js 真实代码执行）：
 * 1. attachmentPanelHtml：按序列号渲染资料列表（下载链接 / 大小 / 上传人 / 时间），
 *    可管理的条目才出现「重命名」「删除」，有上传权限才出现上传入口
 * 2. 无序列号 / 无资料时的空态提示
 * 3. attachmentButton：服务器表操作列按钮，带资料数量徽标并转义序列号
 * 4. attachmentSizeText：B / KB / MB 单位
 * 5. hydrateAttachmentPanels：重命名 / 删除 / 上传的真实交互会打到对应接口
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

const code = [
  extractLine('const $ = s => document.querySelector(s);'),
  extractLine('const $$ = s =>'),
  extractBlock('function esc(v)'),
  extractBlock('function attachmentSizeText(size)'),
  extractBlock('function canUploadAttachments()'),
  extractBlock('function attachmentPanelHtml(serial, requestId = \'\', reload = \'rackReview\')'),
  extractBlock('function attachmentButton(row)'),
  extractBlock('function hydrateAttachmentPanels(root = document)'),
].join('\n\n');

const dom = new JSDOM('<!DOCTYPE html><html><body><div id="host"></div></body></html>', { runScripts: 'outside-only' });

const state = { attachmentMap: {}, attachmentCounts: {} };

// 用真实函数体 + 桩函数（api/toast/refresh）构造执行环境
const build = dom.window.Function('state', 'APP_USER', 'APP_CAPABILITIES', `
  const calls = [];
  const toast = () => {};
  const confirm = () => true;
  let promptValue = '新名字.txt';
  const prompt = () => promptValue;
  const isAdmin = () => APP_USER['身份'] === '管理人员';
  const hasCapability = name => APP_CAPABILITIES.includes(name) || APP_CAPABILITIES.includes('manage_all');
  const api = async (url, opt) => { calls.push({ url, opt }); return { message: 'ok' }; };
  const refreshAttachmentView = async () => {};
  const uploadAttachmentFiles = async (serial, requestId, files, source) => {
    calls.push({ url: '/api/attachments', upload: { serial, requestId, count: files.length, source } });
    return { message: 'ok' };
  };
  ${code}
  return { calls, attachmentPanelHtml, attachmentButton, attachmentSizeText, hydrateAttachmentPanels };
`);

const adminApi = build(state, { '身份': '管理人员' }, ['manage_all']);
const $ = s => dom.window.document.querySelector(s);

// ===== T1: 资料面板渲染 =====
state.attachmentMap = {
  'SN-1': [
    { id: 'a1', serial_number: 'SN-1', filename: '验收单.pdf', size: 2048, uploader: 'admin', uploaded_at: '2026-10-30 10:00:00', can_manage: true },
    { id: 'a2', serial_number: 'SN-1', filename: '只读资料.txt', size: 9, uploader: 'biz1', uploaded_at: '2026-10-30 11:00:00', can_manage: false },
  ],
};
const html = adminApi.attachmentPanelHtml('SN-1', 'req1', 'rackReview');
$('#host').innerHTML = html;
const items = [...$('#host').querySelectorAll('.attachment-item')];
check('T1 渲染出 2 条资料', items.length === 2, items.length);
check('T1 下载链接指向 download 接口',
  $('#host').querySelector('.attachment-name')?.getAttribute('href') === '/api/attachments/a1/download',
  $('#host').querySelector('.attachment-name')?.getAttribute('href'));
check('T1 展示原始文件名', items[0].textContent.includes('验收单.pdf'));
check('T1 展示大小（KB）', items[0].textContent.includes('2 KB'), items[0].textContent);
check('T1 展示上传人与时间', items[0].textContent.includes('admin') && items[0].textContent.includes('2026-10-30 10:00:00'));
check('T1 有权限的条目有重命名按钮', !!items[0].querySelector('[data-attachment-rename]'));
check('T1 有权限的条目有删除按钮', !!items[0].querySelector('[data-attachment-delete]'));
check('T1 无权限的条目没有操作按钮',
  !items[1].querySelector('[data-attachment-rename]') && !items[1].querySelector('[data-attachment-delete]'));
check('T1 面板携带序列号 / 请求id / 刷新上下文', (() => {
  const block = $('#host').querySelector('[data-attachment-block]');
  return block.dataset.serial === 'SN-1' && block.dataset.requestId === 'req1' && block.dataset.reload === 'rackReview';
})());
check('T1 有上传权限时出现上传入口', !!$('#host').querySelector('[data-attachment-upload]'));

// 无上传权限（如无任何相关能力）→ 不出现上传入口、也不出现操作按钮
const guestApi = build(state, { '身份': '业务人员' }, []);
$('#host').innerHTML = guestApi.attachmentPanelHtml('SN-1');
check('T1 无权限用户看不到上传入口', !$('#host').querySelector('[data-attachment-upload]'));

// ===== T2: 空态 =====
$('#host').innerHTML = adminApi.attachmentPanelHtml('SN-NONE');
check('T2 无资料显示空态', $('#host').textContent.includes('暂无资料'), $('#host').textContent);
check('T2 空态仍是 0 个', $('#host').querySelector('.attachment-head').textContent.includes('0 个'));
$('#host').innerHTML = adminApi.attachmentPanelHtml('');
check('T2 无序列号给出提示', $('#host').textContent.includes('没有序列号'), $('#host').textContent);
check('T2 无序列号时不渲染上传入口', !$('#host').querySelector('[data-attachment-upload]'));

// ===== T3: 服务器表「资料」按钮 =====
state.attachmentCounts = { 'SN-1': 3 };
const btnHtml = adminApi.attachmentButton({ '序列号': 'SN-1' });
check('T3 有资料时显示数量', btnHtml.includes('资料 3'), btnHtml);
check('T3 无资料时只显示「资料」', adminApi.attachmentButton({ '序列号': 'SN-9' }).includes('>资料<'),
  adminApi.attachmentButton({ '序列号': 'SN-9' }));
check('T3 序列号被 URL 编码后传参',
  adminApi.attachmentButton({ '序列号': 'SN 1/2' }).includes(encodeURIComponent('SN 1/2')));
check('T3 无序列号不渲染按钮', adminApi.attachmentButton({ '序列号': '' }) === '');

// ===== T4: 大小单位 =====
check('T4 字节', adminApi.attachmentSizeText(512) === '512 B', adminApi.attachmentSizeText(512));
check('T4 KB 四舍五入', adminApi.attachmentSizeText(1536) === '2 KB', adminApi.attachmentSizeText(1536));
check('T4 MB 保留一位小数', adminApi.attachmentSizeText(5 * 1024 * 1024) === '5.0 MB', adminApi.attachmentSizeText(5 * 1024 * 1024));
check('T4 空值按 0 处理', adminApi.attachmentSizeText(undefined) === '0 B');

// ===== T5: 交互（重命名 / 删除 / 上传） =====
async function main() {
  const api = build(state, { '身份': '管理人员' }, ['manage_all']);
  $('#host').innerHTML = api.attachmentPanelHtml('SN-1', 'req1', 'rackReview');
  api.hydrateAttachmentPanels($('#host'));
  const renameBtn = $('#host').querySelector('[data-attachment-rename]');
  check('T5 重命名按钮已绑定点击事件', typeof renameBtn.onclick === 'function');
  renameBtn.onclick();
  await new Promise(r => setTimeout(r, 0));
  const renameCall = api.calls.find(c => c.url.includes('/rename'));
  check('T5 重命名打到 /rename 接口', !!renameCall, JSON.stringify(api.calls));
  check('T5 重命名提交新文件名', renameCall && JSON.parse(renameCall.opt.body).filename === '新名字.txt');

  api.calls.length = 0;
  $('#host').querySelector('[data-attachment-delete]').onclick();
  await new Promise(r => setTimeout(r, 0));
  check('T5 删除打到 /delete 接口', api.calls.some(c => c.url === '/api/attachments/a1/delete'), JSON.stringify(api.calls));

  api.calls.length = 0;
  const input = $('#host').querySelector('[data-attachment-upload]');
  Object.defineProperty(input, 'files', { value: [new dom.window.File(['a'], 'a.txt'), new dom.window.File(['b'], 'b.txt')] });
  input.onchange();
  await new Promise(r => setTimeout(r, 0));
  const up = api.calls.find(c => c.upload);
  check('T5 上传携带序列号与请求id', up && up.upload.serial === 'SN-1' && up.upload.requestId === 'req1',
    JSON.stringify(up));
  check('T5 一次上传多个文件', up && up.upload.count === 2, up && String(up.upload.count));
  check('T5 上传上下文透传（rackReview）', up && up.upload.source === 'rackReview');

  console.log('RESULT: ' + passed + ' passed, ' + failures.length + ' failed');
  if (failures.length) {
    console.log(failures.map(f => '  FAIL ' + f).join('\n'));
    process.exitCode = 1;
  }
}

main();
