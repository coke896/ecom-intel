"""SMZDM（什么值得买）好价采集源。

================================================================================
抓什么：/jingxuan/ 的「好价爆料」，不是商品库
================================================================================
必须先说清楚这件事，因为整个归并设计都建立在它上面：

    SMZDM 上每条爆料（deal）是【一次价格事件】，不是【一个商品】。
    "今日必买：京觅 猕猴桃 17.5 元" 说的是"某商品在某时刻卖这个价"。

所以 products 表和 SMZDM 的对应关系是"多个爆料 → 一个商品"，
中间的转换由 scraper/dedup.py 的归并逻辑负责。

================================================================================
★ 不抓什么：分类页（fenlei/*）—— 实测之后剔除的
================================================================================
计划里原本写的是「/jingxuan/ + 分类好价页」。真去看了页面之后发现分类页
（fenlei/shujuxian、fenlei/shumapeijian 等）**不是好价页，是 UGC 社区页**：

  · 卡片结构是 `shaiwu-card-container`（晒物），链接指向 post.smzdm.com/talk/
  · 正文是用户写的使用感受（"做工质感：尼龙绳做工，相比原装线的橡胶材质…"）
  · **没有 articleid、没有 timesort、没有价格** —— 三样支撑数据模型的字段全缺

两个独立的理由把它排除掉：

  1. **数据模型对不上**：没有 timesort 就没有价格时间点，
     没有价格就没法算 price_history。抓回来只能存成一条"帖子"，
     而本项目没有这个实体。
  2. **★ 合规红线**：晒物卡片里带 `nickname`（用户昵称），
     属于个人信息。项目从一开始就定了"只取商品公开字段、不收集个人信息"，
     所以这里不是"顺便也能抓"，是**明确不抓**。

去掉之后品类覆盖确实变窄了（只剩 /jingxuan/ 全站好价），
这个取舍记在 docs/可行性评估.md 第十六节。

================================================================================
合规约束（写死在代码里，不是口头承诺）
================================================================================
- robots.txt：只访问它允许的路径
- 请求间隔 2~3 秒，且【页数有上限】
- 只取商品公开字段（标题/价格/品牌/平台/类目），
  **解析器里没有一个正则碰用户相关字段**
- 不绕验证码。实测 SMZDM 在约 50~100 次请求后返回腾讯 TCaptcha 挑战页，
  之后直接 403 —— 遇到就抛 FetchBlocked 并停下，不重试
- 仅供学习研究

================================================================================
★ 默认不联网
================================================================================
`SmzdmSource()` 默认 `allow_network=False`，此时 fetch() 直接抛异常，
而不是"反正没人调用所以无所谓"。

理由：这个项目的演示路径（往开发库灌真实数据）走的是
**离线 fixture 重放**，不是实时抓取。让"联网"变成一个必须显式打开的动作，
就不会出现"某次跑测试顺手打了一下对方站点"这种事——
而那种事在 WAF 已经封过一轮之后，代价可能是整条采集线再也验证不了。
"""

from __future__ import annotations

import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from scraper.base import (
    BrowserUnavailable,
    DataSource,
    FetchBlocked,
    FetchStats,
    Layer,
    NetworkDisabled,
    RawDeal,
    try_layers,
)
from scraper.cleaner import parse_timestamp

# ===========================================================================
# 配置
# ===========================================================================

BASE = "https://www.smzdm.com/jingxuan/"

# ★ 请求间隔（秒）。真实浏览器访问时也会停顿，这个节奏不算异常。
#   取随机而不是固定 2.5 秒：固定间隔本身就是脚本特征。
DELAY_RANGE = (2.0, 3.0)

# ★ 单次采集的页数上限。
#   20 条/页 × 10 页 = 200 条，够归并演示，也不给对方站点添麻烦。
MAX_PAGES = 10

# ★ 必须带 UA。不带 UA 的请求一眼就是脚本，而且实测空 UA 会直接吃 403。
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# ---------------------------------------------------------------------------
# ★ 如何识别"被拦截"：一条踩过的坑
# ---------------------------------------------------------------------------
# spike 阶段的注释写的是「带 probe.js 就是被 WAF 拦了」。**这句话是错的**，
# 而且错得很隐蔽——它当时之所以没造成误判，是因为代码里真正起作用的是
# 另一条判据（响应体小于 5000 字节）。
#
# 拿真实页面 fixture 一比就露馅了：
#
#     jingxuan_p1.html（正常页，346KB）  含 probev3.js  ← 有！
#     probe_lanya.html（拦截图，185B）   含 probev3.js  ← 也有
#
# `probev3.js` 是**注入到每一个响应里的 WAF 信标**，正常页面同样带着它。
# 拿它当"被拦截"的判据，会把所有正常页面判成拦截——
# 也就是**全站抓取直接失效**，而且表现为"一页都没抓到"。
#
# 真正区分两者的是体积和跳转：
#
#     正常页  300KB+，有 <ul id="feed-main-list">
#     拦截图  185B，<meta http-equiv="refresh" ... url=.../404>
#
# 这条能成立，靠的是 tests/fixtures/ 里那两个真实文件——
# **如果没有把真实页面存下来，这个 bug 只会在下一次真联网抓取时才暴露**，
# 而那时候 WAF 已经封过一轮了。
BLOCK_MIN_BYTES = 5000
RE_BLOCK_REDIRECT = re.compile(r'http-equiv="refresh"[^>]*url=[^>]*404', re.I)

# UGC 页（分类页）的标记。
# ★ 本项目**明确不采集**这类页，理由见模块顶部：
#   ① 它是晒物帖子，没有 articleid / timesort / 价格，喂不进数据模型
#   ② 卡片里带用户昵称，属于个人信息，与"不收集个人信息"的合规约束冲突
#
#   所以这里不是"顺便也能解析"，是**认出来就拒绝**。
#   写成一条可执行的判据，比写在文档里更靠得住：
#   文档拦不住"我改天把 URL 换成分类页试试"。
RE_UGC_MARKER = re.compile(r"shaiwu-card-container|post\.smzdm\.com/talk/")

# ===========================================================================
# 解析：主体 feed
# ===========================================================================
#
# ★ 这里记一个踩过的坑，它是"页面能打开"和"能正确解析"是两件事的证据：
#
#   页面顶部有个「热门排行榜」小组件，卡片是
#   <div class="feed-hot-list z-clearfix J_feed_za">，元数据藏在
#   tabItemClick(位置参数...) 里。最初我照着它写解析器，**一条都抓不到**。
#
#   真正的主体内容在更下面：<ul id="feed-main-list"> 里的
#   <li class="J_feed_za feed-row-wide">。两者结构完全不同：
#
#     排行榜卡片：无 timesort，articleid 是裸数字，用 tabItemClick(位置参数)
#     主体 feed ：有 timesort（价格时间序列全靠它），articleid 带 "3_" 前缀，
#                 用 sensors.track({命名键}) —— 命名键比位置参数好解析得多
#
#   这两个正则只匹配主体 feed，所以排行榜卡片天然被排除在外。

CARD_RE = re.compile(
    r'<li class="J_feed_za feed-row-wide"(?P<attrs>[^>]*)>(?P<body>.*?)</li>',
    re.S,
)

RE_TIMESORT = re.compile(r'timesort="(\d+)"')
RE_ARTICLEID = re.compile(r'articleid="([^"]+)"')
RE_URL = re.compile(r'href="(https://www\.smzdm\.com/p/\d+/)"')

# 标题在 <h5 class="feed-block-title"> 里的 <a> 标签内，前后有大量空白和换行
RE_TITLE = re.compile(r'<h5 class="feed-block-title">\s*<a[^>]*>\s*(.*?)\s*</a>', re.S)

# ★ 价格标签的属性跨多行（target / href / onclick 各占一行），
#   所以 [^>]* 必须配 re.S，否则匹配不到。
RE_PRICE = re.compile(r'<a class="z-highlight"[^>]*>\s*(.*?)\s*</a>', re.S)

RE_IMG = re.compile(r'src="(https://y\.zdmimg\.com/[^"]+)"')

# sensors.track 的 payload 是 JS 对象字面量（键不带引号），不是合法 JSON，
# 所以不能用 json.loads。好在结构是平坦的，逐键正则提取即可。
RE_BRAND = re.compile(r"'brand':'(.*?)'")
RE_MALL = re.compile(r"'mall_name':'(.*?)'")
RE_CATE = re.compile(r"'cate_level1':'(.*?)'")
RE_CHANNEL = re.compile(r"'channel':'(.*?)'")

RE_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
RE_WS = re.compile(r"\s+")


def _text(raw: str | None) -> str | None:
    """把标签内容清成纯文本：去 HTML 注释、去多余空白、去首尾空格。

    ★ 返回 None 而不是空串，是为了让「字段缺失」和「字段为空」在数据层可区分——
      空串会污染唯一约束和指纹计算，None 不会。
      （这条约定是 app/analysis/fingerprint.py 的同一套思路。）
    """
    if raw is None:
        return None
    raw = RE_HTML_COMMENT.sub("", raw)
    raw = RE_WS.sub(" ", raw).strip()
    return raw or None


def parse_feed(html: str) -> list[RawDeal]:
    """从一页 feed HTML 里抽出所有爆料。**纯函数**，不碰网络也不碰数据库。

    ★ 它是纯函数，所以 tests/test_source_smzdm.py 可以直接喂真实页面 fixture，
      断言"抽出来的就是页面上写的那串字"——不需要联网、不需要起服务。
      采集代码最容易写成一坨"请求+解析+入库"的混合物，
      那样解析逻辑就只能靠真联网来测，而联网的测试既慢又不稳，
      最后一定被跳过，于是解析逻辑等于没有测试。
    """
    deals: list[RawDeal] = []

    for m in CARD_RE.finditer(html):
        attrs, body = m.group("attrs"), m.group("body")

        aid_m = RE_ARTICLEID.search(attrs)
        url_m = RE_URL.search(body)

        # 没有 articleid 的卡片直接跳过。实测每页有 1 张这样的卡片
        # （可能是广告位或特殊推荐位），它不满足"每条爆料有唯一 id"的前提，
        # 硬塞进去会让去重逻辑出错。
        if not (aid_m and url_m):
            continue

        # articleid 形如 "3_183089533"。前缀 "3_" 是所有卡片都带的固定标记，
        # 真正的文章 id 是后半段——URL 和 sensors payload 里用的都是裸数字。
        # 剥掉前缀才能保证同一个商品在不同位置出现时 id 一致。
        source_id = aid_m.group(1).split("_", 1)[-1]

        title_m = RE_TITLE.search(body)
        if not title_m or not _text(title_m.group(1)):
            # 没有标题 = 没有归并比对的输入。这种卡片留着只会污染统计。
            continue

        ts_m = RE_TIMESORT.search(attrs)
        price_m = RE_PRICE.search(body)
        img_m = RE_IMG.search(body)

        deals.append(
            RawDeal(
                source="smzdm",
                source_id=source_id,
                title=_text(title_m.group(1)) or "",
                price_raw=_text(price_m.group(1) if price_m else None),
                recorded_at=parse_timestamp(ts_m.group(1) if ts_m else None),
                url=url_m.group(1),
                image_url=img_m.group(1) if img_m else None,
                brand=_text(RE_BRAND.search(body).group(1)) if RE_BRAND.search(body) else None,
                platform=_text(RE_MALL.search(body).group(1)) if RE_MALL.search(body) else None,
                category=_text(RE_CATE.search(body).group(1)) if RE_CATE.search(body) else None,
                channel=_text(RE_CHANNEL.search(body).group(1)) if RE_CHANNEL.search(body) else None,
            )
        )

    return deals


# ===========================================================================
# 分层降级链
# ===========================================================================


def looks_blocked(html: str) -> bool:
    """判断响应是不是 WAF 拦截图。

    ★ 判据见上面那段注释：**看体积和跳转，不看 probev3.js**
      （那个标记正常页面也有，拿它当判据会让全站抓取直接失效）。

    ★ 单独抽成函数而不是写在 fetch 里，是为了能被测试直接调用——
      "被拦截时我们要能认出来"这件事必须有测试守着，
      否则 WAF 换个拦法，程序会把空结果当成"这个页面确实没有爆料"。
    """
    if len(html) < BLOCK_MIN_BYTES:
        return True
    return bool(RE_BLOCK_REDIRECT.search(html))


def looks_like_ugc(html: str) -> bool:
    """判断响应是不是分类页 / UGC 晒物页（本项目不采集）。

    ★ 单独一个判据而不是并进 looks_blocked：
      两者的处理方式完全不同——被拦截要停下并报警（对方在挡我们），
      UGC 页要明确拒绝并说明理由（是我们自己选择不采）。
      合成一个的话，日志里只会写"被拦截"，而真相是"抓错页面了"，
      排查方向完全相反。
    """
    return bool(RE_UGC_MARKER.search(html))


# ★ 降级链。顺序即优先级。
#
#   ★ 这里【没有】"JSON 接口"那一层，尽管计划里写了。
#     真去调研时没有找到可用的 JSON 接口，而写一个没被验证过的层
#     只会让代码看起来比实际能力强——面试官问一句"响应长什么样"就露馅。
#     不写没验证的东西，这条比"层数看起来多"重要。
LAYERS: tuple[Layer, ...] = (
    Layer(name="html", parse=parse_feed),
    # 浏览器层：把渲染后的 HTML 交给同一个 parse_feed。
    # ★ 注意 needs_browser=True，而 try_layers 在 allow_browser=False 时
    #   会直接跳过它——所以默认路径下这一层【一次都不会被执行】。
    Layer(name="browser", parse=parse_feed, needs_browser=True),
)


# ===========================================================================
# 采集源
# ===========================================================================


class SmzdmSource(DataSource):
    """SMZDM 采集源。

    ★ 默认 `allow_network=False`：fetch() 会直接抛异常，而不是静默返回空。
      静默返回空是最坏的选择——它让"没有联网"看起来像"抓到了 0 条"，
      而这两件事的排查方向完全相反。
    """

    name = "smzdm"

    def __init__(
        self,
        *,
        allow_network: bool = False,
        allow_browser: bool = False,
        delay_range: tuple[float, float] = DELAY_RANGE,
        timeout: float = 30.0,
    ) -> None:
        self.allow_network = allow_network
        self.allow_browser = allow_browser
        self.delay_range = delay_range
        self.timeout = timeout

    # -- 抓取 ---------------------------------------------------------------

    def fetch(self, *, pages: int = 1) -> tuple[str, list[RawDeal], FetchStats]:
        if not self.allow_network:
            # ★ 这里抛的是 NetworkDisabled 而**不是** FetchBlocked。
            #   两者都会让采集停下，但一个是我们自己关着开关，
            #   另一个是对方在挡我们——失败任务的提示语完全不同，
            #   混用一个类型就会把"忘了加 --allow-network"说成"被 WAF 拦了"。
            #   详见 base.py 里 NetworkDisabled 的注释。
            raise NetworkDisabled(
                "SmzdmSource 默认不联网。要用真网络采集，显式传 "
                "SmzdmSource(allow_network=True)，或在命令行加 --allow-network。\n"
                "★ 这条路刻意留成「必须手动打开」：SMZDM 有 WAF，"
                "实测约 50~100 次请求后会被腾讯 TCaptcha 拦住。\n"
                "  开发与演示建议走离线重放：python -m scraper.worker --replay"
            )
        if pages < 1:
            raise ValueError("pages 必须 >= 1")
        if pages > MAX_PAGES:
            # ★ 上限是硬约束。不设上限的话，"多抓几页"是个太容易做出的决定。
            raise ValueError(f"pages 上限是 {MAX_PAGES}（合规约束，见模块顶部）")

        import httpx  # 惰性导入：不联网时连 httpx 都不需要加载

        stats = FetchStats()
        all_deals: list[RawDeal] = []
        layer_name = ""

        with httpx.Client(headers=HEADERS, timeout=self.timeout, follow_redirects=True) as client:
            for page in range(1, pages + 1):
                # 第 1 页是 /jingxuan/，第 N 页是 /jingxuan/pN/（实测 p2/ 是 200）
                url = BASE if page == 1 else f"{BASE}p{page}/"
                resp = client.get(url)

                # ★ 顺序同 parse_files：先认 UGC，再认拦截。
                #   分类页体积很大，不会触发体积判据；两者都命中时，
                #   "URL 填错了"是更准确的解释。
                if looks_like_ugc(resp.text):
                    stats.pages_ugc += 1
                    stats.layer = layer_name
                    break

                if resp.status_code != 200 or looks_blocked(resp.text):
                    # ★ 被拦截就【立刻停止】，不重试、不换 IP、不绕验证码。
                    #   重试只会让对方更确定这是脚本，也让自己更快被封。
                    stats.pages_blocked += 1
                    stats.layer = layer_name
                    if not all_deals:
                        # 一条都没抓到就是彻底的失败，抛出去让任务状态变成 failed，
                        # 而不是"成功但 0 条"——后者会被当成"页面本来就没内容"。
                        raise FetchBlocked(
                            f"第 {page} 页被拦截（HTTP {resp.status_code}，"
                            f"{len(resp.text)} 字节）。已停止本次采集。"
                        )
                    break

                name, deals = try_layers(
                    resp.text, LAYERS, allow_browser=self.allow_browser
                )
                if not deals:
                    # 页面正常、没被拦、不是 UGC，却一条都解析不出来。
                    # ★ 和"被拦截"分开计数：这个信号意味着**解析器该改版了**，
                    #   不是 WAF 的问题。混在一起会把人带去研究 WAF。
                    stats.pages_unparsed += 1
                    break

                layer_name = name or layer_name
                stats.pages_ok += 1
                all_deals.extend(deals)

                if page < pages:
                    time.sleep(random.uniform(*self.delay_range))

        stats.layer = layer_name
        return layer_name, all_deals, stats

    # -- 离线重放 -----------------------------------------------------------

    @staticmethod
    def parse_files(paths: list[Path]) -> tuple[str, list[RawDeal], FetchStats]:
        """从本地 HTML 文件解析（离线重放）。

        ★ 这是本项目【实际使用】的采集路径，也是开发库能拿到真实 SMZDM 数据的原因：

          磁盘上那批页面是我从 smzdm.com 真实抓下来的（spike/_raw/），
          所以重放它们得到的是**真实数据**，不是造出来的。

          这和"伪造 source='smzdm' 的数据"有本质区别——
          区别在于数据是不是从真站点来的。这一点在
          docs/可行性评估.md 里反复强调过（14.6 / 15.7），
          因为"这数据是真的还是 Mock"正是面试会问的第一个问题。

        重放还有个实际好处：CI 和别人的机器上都能跑，不需要联网、不会被 WAF 拦。
        """
        stats = FetchStats()
        all_deals: list[RawDeal] = []
        layer_name = ""

        for path in paths:
            html = path.read_text(encoding="utf-8", errors="replace")

            # ★ 顺序有讲究：先认 UGC，再认拦截。
            #   分类页体积很大（500KB+），不会触发体积判据；
            #   但如果哪天 WAF 开始拦分类页，两者会同时为真，
            #   那时"抓错页面了"是更准确的解释（因为 URL 本来就填错了）。
            if looks_like_ugc(html):
                stats.pages_ugc += 1
                continue
            if looks_blocked(html):
                stats.pages_blocked += 1
                continue

            name, deals = try_layers(html, LAYERS, allow_browser=False)
            layer_name = name or layer_name
            if deals:
                stats.pages_ok += 1
                all_deals.extend(deals)
            else:
                # 页面拿到了、也没被拦、也不是 UGC，但一条都解析不出来
                # = 页面结构变了。这必须和"被拦截"分开报，
                # 否则下次 SMZDM 改版时，日志里写的是"被拦截"，
                # 而人会跑去研究 WAF，实际上该做的是改解析器。
                stats.pages_unparsed += 1

        stats.layer = layer_name
        return layer_name, all_deals, stats


def dedupe_deals(deals: list[RawDeal]) -> list[RawDeal]:
    """按 source_id 去重，保留首次出现的那条。

    ★ 为什么需要它：多页之间会重叠。
      实测抓下来的 7 个页面文件里，jingxuan.html 和 jingxuan_p1.html 是同一页
      （20 条完全相同），p2.html 和 jingxuan_p2.html 也是同一页。
      140 条原始结果里只有 100 条是唯一的。

      不去重的话，同一篇文章会被"归并"两次——而第二次的 price_history
      会因为 (product_id, source_ref) 唯一约束被跳过，所以**数据不会错**，
      但 products_merged 的统计会翻倍，界面上显示"归并 40 条"，
      而实际归并的是 0 条。统计错了比数据错了更难发现。
    """
    seen: set[str] = set()
    out: list[RawDeal] = []
    for d in deals:
        if d.source_id in seen:
            continue
        seen.add(d.source_id)
        out.append(d)
    return out


def browser_layer_deals(html: str) -> list[RawDeal]:
    """浏览器层的实际实现位置（当前未启用）。

    ★ 故意留成一个抛异常的函数，而不是 import playwright 写一段代码：

      1. 本项目页面是服务端渲染，httpx 就够（实测 100 条全字段解析成功），
         这一层从未被需要过。写一段跑不了的代码不是"预留"，是负债。
      2. 更要紧的是它【不能】跑在 API 进程里：Windows 上 uvicorn --reload 的
         worker 落在 SelectorEventLoop 上，而它不支持子进程，
         Playwright 启动 Chromium 正是靠子进程。所以真要用，
         它只能出现在 scraper/worker.py 这个独立进程里。

      留着这个函数是为了让"这一层存在"这件事在代码里可见，
      同时明确它是未实现的状态——而不是让人以为浏览器降级已经就绪。
    """
    raise BrowserUnavailable(
        "浏览器降级层尚未实现。本项目页面是服务端渲染，httpx 已足够"
        "（实测 100 条真实爆料全字段解析成功），因此没有安装 Playwright。"
        "真需要时：pip install playwright && playwright install chromium，"
        "然后只允许在 scraper/worker.py 独立进程里使用。"
    )
