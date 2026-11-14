/**
 * 「下载 MCP」按钮（jsdom，提取 app.js 真实代码执行）：
 * 1. 账号菜单里是「下载 MCP」而不是「下载 Skill」，且不再有 skill 入口
 * 2. 点击后请求的是 /api/mcp/download（不是 /api/skill/download）
 * 3. 下载文件名是 server_management_mcp.zip，成功提示带版本号
 * 4. 后端报错时把 error 文案透传给用户，不静默失败
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
  <div id="keyMenu" hidden>
    <button id="keyMenuToken" type="button">🔑 获取 Token</button>
    <button id="keyMenuMcp" type="button">⬇ 下载 MCP</button>
  </div>
</body>`;

const dom = new JSDOM(html, { url: 'http://localhost/', runScripts: 'dangerously' });
const { window } = dom;
const $ = s => window.document.querySelector(s);

const fetchCalls = [];
let responseFor = () => ({
  ok: true,
  headers: { get: name => (name === 'X-MCP-Version' ? 'abcdef123456' : null) },
  blob: () => Promise.resolve(new window.Blob(['zip-bytes'], { type: 'application/zip' })),
  json: () => Promise.resolve({}),
});
window.fetch = (url, opt) => {
  fetchCalls.push({ url, method: (opt && opt.method) || 'GET' });
  return Promise.resolve(responseFor(url));
};

const downloads = [];
window.URL.createObjectURL = () => 'blob:fake';
window.URL.revokeObjectURL = () => {};
const realClick = window.HTMLAnchorElement.prototype.click;
window.HTMLAnchorElement.prototype.click = function () {
  if (this.download) { downloads.push({ download: this.download, href: this.href }); return; }
  return realClick.apply(this, arguments);
};

const toasts = [];
const code = [
  'const $ = s => window.document.querySelector(s);',
  'const toast = (msg, bad) => window.__toasts.push({ msg, bad: !!bad });',
  extractBlock('function closeKeyMenu()'),
  extractBlock('async function downloadMcpPackage()'),
  extractLine("if ($('#keyMenuMcp'))"),
].join('\n');
window.__toasts = toasts;
window.eval(code);

const tick = (ms = 30) => new Promise(r => setTimeout(r, ms));

async function main() {
  await tick();

  // ===== T1: 模板与绑定 =====
  check('T1 模板有「下载 MCP」按钮', template.includes('id="keyMenuMcp"') && template.includes('下载 MCP'));
  check('T1 模板不再有「下载 Skill」按钮', !template.includes('keyMenuSkill') && !template.includes('下载 Skill'));
  check('T1 菜单项文案正确', ($('#keyMenuMcp') || {}).textContent === '⬇ 下载 MCP',
    ($('#keyMenuMcp') || {}).textContent);

  // ===== T2: 点击 → 走 MCP 端点 =====
  $('#keyMenuMcp').onclick();
  await tick();
  check('T2 请求 /api/mcp/download', fetchCalls.some(c => c.url === '/api/mcp/download'),
    fetchCalls.map(c => c.url).join(' | '));
  check('T2 不再请求 skill 包', !fetchCalls.some(c => String(c.url).includes('/api/skill/download')),
    fetchCalls.map(c => c.url).join(' | '));
  check('T2 菜单已收起', $('#keyMenu').hidden);

  // ===== T3: 下载文件名与版本提示 =====
  check('T3 下载文件名是 mcp 包', downloads.length === 1 && downloads[0].download === 'server_management_mcp.zip',
    JSON.stringify(downloads));
  const okToast = toasts[toasts.length - 1] || {};
  check('T3 成功提示带版本号', String(okToast.msg || '').includes('abcdef123456') && okToast.bad === false,
    JSON.stringify(okToast));

  // ===== T4: 后端报错要透传 =====
  responseFor = () => ({
    ok: false,
    headers: { get: () => null },
    json: () => Promise.resolve({ error: 'MCP 服务目录不存在：/x/mcp_server' }),
    blob: () => Promise.resolve(new window.Blob([])),
  });
  downloads.length = 0;
  $('#keyMenuMcp').onclick();
  await tick();
  const badToast = toasts[toasts.length - 1] || {};
  check('T4 错误文案透传给用户', String(badToast.msg || '').includes('MCP 服务目录不存在') && badToast.bad === true,
    JSON.stringify(badToast));
  check('T4 失败时不触发下载', downloads.length === 0, JSON.stringify(downloads));

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
