/* API 封装 + 全局状态（四个页面共用这一份）。 */

/** 当前选中的数据源。记住它是为了演示时不反复重选（存 localStorage、URL 保持干净），
 *  同时读一次 URL，这样 ?source=smzdm 分享链接仍然能用。 */
const SOURCE_KEY = 'ecom-intel.source';
const SOURCES = [
  { id: 'all', label: '全部' },
  { id: 'mock', label: 'Mock' },
  { id: 'smzdm', label: '真实采集' },
];

function currentSource() {
  const fromUrl = new URLSearchParams(location.search).get('source');
  if (fromUrl && SOURCES.some((s) => s.id === fromUrl)) return fromUrl;
  const saved = localStorage.getItem(SOURCE_KEY);
  if (saved && SOURCES.some((s) => s.id === saved)) return saved;
  return 'all';
}

function setSource(id) {
  localStorage.setItem(SOURCE_KEY, id);
  // 同步写回 URL 但不触发跳转：分享地址能带上数据源，页面也不重新加载（重载会让图表闪一下）。
  const url = new URL(location.href);
  if (id === 'all') url.searchParams.delete('source');
  else url.searchParams.set('source', id);
  history.replaceState(null, '', url);
}

/** 数据源按钮上该显示的数字；拿不到返回 undefined（= 不显示数字）。
 *  「全部」的计数在这里求和（/api/stats/sources 按实际存在的 source 逐条返回，不给 all 项），
 *  规则只存在这一处；一个源都没有时返回 undefined 而不是 0（0 会被渲染成"空数据源"）。 */
function sourceCount(id, counts) {
  const known = Object.values(counts || {});
  if (!known.length) return undefined;
  if (id !== 'all') return counts[id];
  return known.reduce((a, b) => a + b, 0);
}

/** 带 source 参数的请求，所有后端接口都走这里——调用方没有机会手写 URL，
 *  也就不可能忘记带 source；GET/POST 共用，因为 source 注入和错误翻译与 HTTP 方法无关。 */
async function apiRequest(method, path, params = {}, body = undefined) {
  const url = new URL(path, location.origin);
  url.searchParams.set('source', currentSource());
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v);
  }

  const init = { method };
  if (body !== undefined) {
    // body 必须显式传，不能"有 params 就顺手当 body 发"：
    // params 是过滤条件、body 是要写入的数据，混在一起会让"只想加个过滤参数"变成写库。
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(body);
  }

  const resp = await fetch(url, init);
  if (!resp.ok) {
    // 把后端的 detail 带出来：FastAPI 的 422 是结构化数组，直接 alert 一个 [object Object] 对排查毫无帮助。
    let detail = '';
    try {
      const body = await resp.json();
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    } catch { /* 响应不是 JSON（比如静态文件的 404 页面），忽略 */ }
    throw new Error(`${resp.status} ${detail || resp.statusText}`);
  }
  return resp.json();
}

function apiGet(path, params = {}) {
  return apiRequest('GET', path, params);
}

/** POST。生成报告必须用 POST：它会写一行报告、消耗 token——
 *  GET 的"安全、可重复"约定两条都不满足，浏览器预取/代理缓存/爬虫都可能触发一次付费调用。
 *  第三个参数 body 给"要写数据"的接口用（建告警规则）。 */
function apiPost(path, params = {}, body = undefined) {
  return apiRequest('POST', path, params, body);
}

/** PATCH：改一条已存在的记录。后端用 PATCH 表达"只改传进来的字段"；
 *  用 POST 会拿到 405，报错不会告诉你语义不匹配。 */
function apiPatch(path, body) {
  return apiRequest('PATCH', path, {}, body);
}

/** DELETE：删一条记录。后端会回一个 JSON（删掉了什么、附带一句提醒）。 */
function apiDelete(path) {
  return apiRequest('DELETE', path);
}

/** 图片地址 → 走后端代理的地址。这里是唯一不走 apiRequest 的后端接口：
 *  <img> 加载器插不进 fetch，因此也拿不到 ?source=（取字节与平台无关，缩略图三视图相同）。
 *  用 encodeURIComponent：图床 URL 常带 &，不编码会被当成 /api/proxy-image 自己的参数。 */
function proxyImageUrl(url) {
  if (!url) return '';
  return '/api/proxy-image?url=' + encodeURIComponent(url);
}

/** 图片加载失败时的兜底：换成空白占位（破图图标看起来像"网站坏了"；无图是常态）。
 *  用 function 声明而不是 const：它由内联 onerror="imgFallback(this)" 从 window 上找，
 *  顶层 const 不会挂到 window，会变成 ReferenceError 而只表现为占位符没出来。 */
function imgFallback(img) {
  const box = document.createElement('span');
  box.className = 'thumb thumb-empty';
  // 兜底时不能再触发兜底（replaceWith 后 <img> 已不在文档里，不会再触发 onerror）。
  img.replaceWith(box);
}

/* 数字格式化 */

function fmtInt(n) {
  if (n === null || n === undefined) return '—';
  return n.toLocaleString('zh-CN');
}

function fmtPrice(n) {
  if (n === null || n === undefined) return '—';
  return '¥' + Number(n).toFixed(2);
}

/** 0.1234 → "12.3%"。接口返回 0~1 的小数，这里乘 100 是唯一一次乘法
 *  （接口那边刻意不乘，两边都乘会得到 8700% 这种荒唐数字）。 */
function fmtPct(x) {
  if (x === null || x === undefined) return '—';
  return (x * 100).toFixed(1) + '%';
}

/** 后端给的是不带时区的 UTC 字符串；本项目原样截断显示并标出 (UTC)。
 *  转本地时区稍不注意就转错——显示 UTC 是诚实的近似，转错才是自信地给出错误答案。 */
function fmtTime(s) {
  if (!s) return '—';
  return s.slice(0, 16).replace('T', ' ') + ' UTC';
}

/* 转义。ECharts 的 tooltip formatter 返回字符串、由 echarts 自己赋给 innerHTML——
   那条路不在我们的 DOM 拼接路径上，不受"不用 innerHTML"纪律的约束，但要自己负责转义。
   放在 api.js：三个页面都引这个文件，避免出现第二份拷贝。 */

// 必须走 textContent 而不是手写 replace 链：手写的那种总会静默漏字符。
function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

/** 属性值用。和 escapeHtml 的差别只在引号——textContent 那条路生成的是文本节点，不处理引号。 */
function escapeAttr(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/"/g, '&quot;')
    .replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
