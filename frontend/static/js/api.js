/* ============================================================================
 * API 封装 + 全局状态
 *
 * 两个页面（index.html / products.html）共用这一份。
 * ========================================================================== */

/** 当前选中的数据源。
 *
 *  ★ 为什么要"记住"它：演示双数据源的时候，人会反复在
 *    「首页看总览 → 点进商品列表 → 返回首页」之间来回走。
 *    如果每次跳页都重置回 all，这个来回就会变成"每页都要重新点一次"，
 *    演示节奏全被打断——而"两个数据源切换对比"恰恰是这个项目的核心看点。
 *
 *  存 localStorage 而不是 URL 参数，是为了让 URL 干净；
 *  同时也读一次 URL，这样 `?source=smzdm` 这种分享链接仍然能用。
 */
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
  // ★ 同步写回 URL，但不触发跳转。
  //   这样"复制地址栏发给别人"能带上当前数据源，
  //   而页面不重新加载——重新加载会让已经画好的图表全部闪一下。
  const url = new URL(location.href);
  if (id === 'all') url.searchParams.delete('source');
  else url.searchParams.set('source', id);
  history.replaceState(null, '', url);
}

/** 带 source 参数的 GET。
 *
 *  ★ 所有统计接口都走这里，**没有例外**。
 *    这样"忘了带 source"这件事在代码层面就不可能发生——
 *    因为调用方根本没有机会手写 URL。
 *    （后端那边也是同一个思路：过滤只写在 apply_source 一处。）
 */
async function apiGet(path, params = {}) {
  const url = new URL(path, location.origin);
  url.searchParams.set('source', currentSource());
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v);
  }

  const resp = await fetch(url);
  if (!resp.ok) {
    // 把后端的 detail 带出来。FastAPI 的 422 是结构化数组，
    // 直接 alert 一个 [object Object] 对排查毫无帮助。
    let detail = '';
    try {
      const body = await resp.json();
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    } catch { /* 响应不是 JSON（比如静态文件的 404 页面），忽略 */ }
    throw new Error(`${resp.status} ${detail || resp.statusText}`);
  }
  return resp.json();
}

/* ---------------------------------------------------------------------------
 * 数字格式化
 * ------------------------------------------------------------------------ */

function fmtInt(n) {
  if (n === null || n === undefined) return '—';
  return n.toLocaleString('zh-CN');
}

function fmtPrice(n) {
  if (n === null || n === undefined) return '—';
  return '¥' + Number(n).toFixed(2);
}

/** 占比：0.1234 → "12.3%"。
 *
 *  ★ 入参是 0~1 的小数，接口就是这么返回的（见 stats.py 里 ShopShareItem 的注释）。
 *    这里乘 100 是**唯一**一次乘法——接口那边刻意不乘，
 *    因为两边都乘一次就会得到 8700% 这种荒唐数字。
 */
function fmtPct(x) {
  if (x === null || x === undefined) return '—';
  return (x * 100).toFixed(1) + '%';
}

/** 后端给的是不带时区的 UTC 字符串（"2026-06-30 10:28:10.043727"）。
 *
 *  ★ 两种做法都错，必须选一种并说明：
 *      ① 当成 UTC 再转本地 → 需要手动补 "Z"，稍不注意就转错时区
 *      ② 原样截断显示      → 显示的是 UTC 时间，而用户以为是本地时间
 *    本项目选 ②，并且**在界面上标出 (UTC)**。
 *    理由是数据全是 Mock/采集来的，用户不靠它做决策，
 *    而显示 UTC 是"诚实的近似"；转错了时区才是"自信地给出错误答案"。
 */
function fmtTime(s) {
  if (!s) return '—';
  return s.slice(0, 16).replace('T', ' ') + ' UTC';
}
