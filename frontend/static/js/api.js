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

/** 带 source 参数的请求。
 *
 *  ★ 所有后端接口都走这里，**没有例外**。
 *    这样"忘了带 source"这件事在代码层面就不可能发生——
 *    因为调用方根本没有机会手写 URL。
 *    （后端那边也是同一个思路：过滤只写在 apply_source 一处。）
 *
 *  ★ 为什么 GET 和 POST 共用同一个函数、只差一个 method：
 *    "source 注入"和"错误翻译"这两件事跟 HTTP 方法毫无关系。
 *    分开写成两个函数的话，将来给错误处理加一条规则
 *    （比如 503 要显示成"服务未配置"而不是"出错了"），
 *    就必须记得改两处——而漏改的那一处不会报错，
 *    只会让某一类请求的错误提示和别人不一样。
 */
async function apiRequest(method, path, params = {}, body = undefined) {
  const url = new URL(path, location.origin);
  url.searchParams.set('source', currentSource());
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v);
  }

  const init = { method };
  if (body !== undefined) {
    // ★ body 必须是显式传的，不能"有 params 就顺手当 body 发"。
    //   这两种东西语义完全不同：params 是过滤条件（GET 也能有），
    //   body 是要写入的数据。混在一起的话，某个只想加个过滤参数的调用
    //   会变成"把过滤条件当数据写进库"——而它看起来完全正常。
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(body);
  }

  const resp = await fetch(url, init);
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

function apiGet(path, params = {}) {
  return apiRequest('GET', path, params);
}

/** POST。
 *
 *  ★ 生成报告必须用 POST，这不是风格问题：
 *    它会写一行报告、消耗 token。用 GET 的话，浏览器预取、代理缓存、
 *    爬虫都可能触发一次**付费调用**——而且在用户没点任何按钮的时候。
 *    HTTP 语义在这里是有实际后果的：GET 约定"安全、可重复"，而它两条都不满足。
 *
 *  ★ 第三个参数 body 是给"要写数据"的接口用的（建告警规则）。
 *    和前两个参数分开，理由见 apiRequest 里那段注释。
 */
function apiPost(path, params = {}, body = undefined) {
  return apiRequest('POST', path, params, body);
}

/** PATCH：改一条已存在的记录（告警规则的启停和阈值）。
 *
 *  ★ 为什么不复用 POST：后端用 PATCH 表达"只改传进来的字段"——
 *    空 PATCH 和不改任何东西的 PATCH 都不会重置规则的状态机状态。
 *    前端用 POST 去调同一个路径会拿到 405，那个报错信息不会告诉你
 *    "语义不匹配"，只会说方法不允许。
 */
function apiPatch(path, body) {
  return apiRequest('PATCH', path, {}, body);
}

/** DELETE：删一条记录。★ 后端会回一个 JSON（删掉了什么、附带一句提醒）。 */
function apiDelete(path) {
  return apiRequest('DELETE', path);
}

/** 图片地址 → 走后端代理的地址。
 *
 *  ★ 这是**唯一**一个不走 apiRequest 的后端接口，两个理由：
 *
 *  ① 取图的是**浏览器的 <img> 加载器**，不是我们的 JS。
 *     什么 fetch / 什么 XHR 都插不进去——所以它走不了 apiRequest，
 *     而不是"我们决定让它特殊"。
 *  ② 它也因此**拿不到 `?source=`**。这不是遗漏：
 *     取字节的动作和"这个平台的商品是什么样"无关，
 *     同一个商品在 all / mock / smzdm 三个视图下的缩略图是同一张。
 *     （后端那边也是同一个说法，见 app/api/images.py 顶部。）
 *
 *  ★ 用 encodeURIComponent 而不是拼字符串：图片地址里带 `&`（图床的
 *    URL 经常有 query），不编码的话第一个 `&` 之后的内容会被当成
 *    `/api/proxy-image` 自己的参数——表现是"某些图能显示、某些不能"，
 *    而它们的区别只是 URL 里有没有 `&`，很容易往图床那边想。
 *
 *  顺带一提，编码之后返回值里只剩 URL 安全字符，
 *  所以它可以被直接插进 HTML 属性，不需要再转义一次。
 */
function proxyImageUrl(url) {
  if (!url) return '';
  return '/api/proxy-image?url=' + encodeURIComponent(url);
}

/** 图片加载失败时的兜底。
 *
 *  ★ 用 `function` 声明而不是 `const imgFallback = ...`：
 *    这个函数是给 `onerror="imgFallback(this)"` 这个**内联属性**调的，
 *    而内联属性里的名字是从 window 上找的。
 *    顶层 `function` 声明会挂到 window 上，顶层 `const` **不会**
 *    （它在脚本的词法作用域里）——写成 const 的话，
 *    图片一加载失败就是一个 ReferenceError，
 *    而表现的只是"占位符没出来"，看不到报错来源。
 *
 *  ★ 为什么必须有它：代理失败是会发生的（断网、图床挂了、超过大小上限），
 *    而 <img> 失败时浏览器画的是那个**破图图标**——
 *    它看起来像"这个网站坏了"，而不是"这张图没取到"。
 *    换成一块空白占位要好得多，因为"没有图"本来就是这一屏的常态
 *    （Mock 数据一条图都没有）。
 */
function imgFallback(img) {
  const box = document.createElement('span');
  box.className = 'thumb thumb-empty';
  // ★ 兜底时不能再触发兜底。replaceWith 之后原来的 <img> 已经不在文档里，
  //   不会再触发 onerror；但这一点值得写下来，
  //   因为"错误处理自己报错"是这类代码最常见的死循环。
  img.replaceWith(box);
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
