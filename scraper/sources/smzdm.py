"""SMZDM（什么值得买）好价采集源。

抓什么：/jingxuan/ 的「好价爆料」不是商品库——每条爆料是【一次价格事件】，
"多个爆料 → 一个商品"的归并由 scraper/dedup.py 负责。

★ 不抓分类页（fenlei/*）：实测是 UGC 晒物页——没有 articleid / timesort / 价格
（喂不进数据模型），且卡片带用户昵称，与"不收集个人信息"的合规约束冲突。
这是明确不抓，不是"顺便也能抓"；取舍记在 docs/可行性评估.md 第十六节。

合规约束（写死在代码里）：只访问 robots 允许的路径；请求间隔 2~3 秒且页数有上限
（MAX_PAGES=10）；解析器不碰任何用户相关字段；**不绕验证码**——实测约 50~100 次
请求后吃到腾讯 TCaptcha 挑战页、随后 403，遇到就抛 FetchBlocked 停下，不重试。

★ 默认不联网：allow_network=False 时 fetch() 直接抛异常，而不是静默返回空——
联网必须显式打开，演示走离线 fixture 重放。
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

# --- 配置 ---

BASE = "https://www.smzdm.com/jingxuan/"

# ★ 我方采集通道，必须落在 db/models.py 声明的取值族（mock / jingxuan / fenlei）里：
# **price_history.recorded_at 的语义跟着它走**（'jingxuan' = 爆料发布时间，不是抓取
# 时间），前端坐标轴标签也按它查表。★ 不能填源站自己的 `'channel':'youhui'`——
# 那是文章的类型分类、只是名字撞了；填错的后果：真实数据 100/100 落在取值族外、
# 坐标轴退化成"记录时间"、弹窗把内部标记当业务值显示。
CHANNEL = "jingxuan"

# ★ 请求间隔（秒）。取随机而不是固定 2.5 秒——固定间隔本身就是脚本特征；
# 真实浏览器访问时也有停顿，这个节奏不算异常。
DELAY_RANGE = (2.0, 3.0)

# ★ 单次采集页数上限：20 条/页 × 10 页 = 200 条，够归并演示，也不给对方添麻烦。
MAX_PAGES = 10

# ★ 必须带 UA：不带 UA 的请求一眼是脚本，实测空 UA 直接吃 403。
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# --- ★ 识别"被拦截"：判据是体积和跳转，不是 probev3.js ---
# probev3.js 是注入到每个响应里的 WAF 信标，正常页也有（346KB 的正常页同样含它）——
# 拿它当判据会把所有正常页面判成拦截、全站抓取直接失效。真正的区别：正常页 300KB+
# 且有 <ul id="feed-main-list">，拦截图 185B 且带 meta refresh → /404。
# 这条能成立靠 tests/fixtures/ 里的真实页面文件——没有它们，这个 bug 只会在下次
# 真联网抓取时才暴露（那时 WAF 已经封过一轮了）。
BLOCK_MIN_BYTES = 5000
RE_BLOCK_REDIRECT = re.compile(r'http-equiv="refresh"[^>]*url=[^>]*404', re.I)

# UGC 页（分类页）的标记：本项目**明确不采集**（喂不进数据模型 + 卡片带用户昵称，
# 与合规约束冲突）——认出来就拒绝。写成可执行判据而不是文档，
# 因为"我改天把 URL 换成分类页试试"是文档拦不住的。
RE_UGC_MARKER = re.compile(r"shaiwu-card-container|post\.smzdm\.com/talk/")

# --- 解析：主体 feed ---
# ★ 页面顶部还有个「热门排行榜」小组件（feed-hot-list / tabItemClick 位置参数）——
# 照着它写解析器**一条都抓不到**。真正的主体在 ul#feed-main-list 的 li.feed-row-wide：
# 有 timesort（价格时间序列全靠它）、articleid 带 "3_" 前缀、用命名键的 sensors.track。
# 下面的正则只匹配主体 feed，排行榜卡片天然被排除在外。

CARD_RE = re.compile(
    r'<li class="J_feed_za feed-row-wide"(?P<attrs>[^>]*)>(?P<body>.*?)</li>',
    re.S,
)

RE_TIMESORT = re.compile(r'timesort="(\d+)"')
RE_ARTICLEID = re.compile(r'articleid="([^"]+)"')
RE_URL = re.compile(r'href="(https://www\.smzdm\.com/p/\d+/)"')

# 标题在 <h5 class="feed-block-title"> 里的 <a> 标签内，前后有大量空白和换行
RE_TITLE = re.compile(r'<h5 class="feed-block-title">\s*<a[^>]*>\s*(.*?)\s*</a>', re.S)

# ★ 价格标签的属性跨多行（target / href / onclick 各占一行），[^>]* 必须配 re.S。
RE_PRICE = re.compile(r'<a class="z-highlight"[^>]*>\s*(.*?)\s*</a>', re.S)

RE_IMG = re.compile(r'src="(https://y\.zdmimg\.com/[^"]+)"')

# sensors.track 的 payload 是 JS 对象字面量（键不带引号），不是合法 JSON，
# 不能用 json.loads——好在结构平坦，逐键正则提取即可。
RE_BRAND = re.compile(r"'brand':'(.*?)'")
RE_MALL = re.compile(r"'mall_name':'(.*?)'")
RE_CATE = re.compile(r"'cate_level1':'(.*?)'")
# ★ 这里曾有一条 RE_CHANNEL（抓源站的 `'channel':'youhui'`），已删除——它抓到的值
# 曾被写进 source_channel，而那是两个不同字段（见上面 CHANNEL 段）；需要时在 git
# 历史里能找到。源站的文章类型本项目并不使用，留着只会给"顺手填进去"留机会。

RE_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
RE_WS = re.compile(r"\s+")


def _text(raw: str | None) -> str | None:
    """把标签内容清成纯文本：去 HTML 注释、压缩空白、去首尾空格。

    ★ 返回 None 而不是空串：让「字段缺失」和「字段为空」可区分——空串会污染唯一约束和指纹。
    """
    if raw is None:
        return None
    raw = RE_HTML_COMMENT.sub("", raw)
    raw = RE_WS.sub(" ", raw).strip()
    return raw or None


def parse_feed(html: str) -> list[RawDeal]:
    """从一页 feed HTML 里抽出所有爆料。**纯函数**，不碰网络也不碰数据库。

    ★ 纯函数所以测试可以直接喂真实页面 fixture、断言"抽出来的就是页面上写的那串字"——
    采集代码最容易写成"请求+解析+入库"的混合物，那样解析逻辑只能靠联网测，
    而联网测试又慢又不稳、最后一定被跳过。
    """
    deals: list[RawDeal] = []

    for m in CARD_RE.finditer(html):
        attrs, body = m.group("attrs"), m.group("body")

        aid_m = RE_ARTICLEID.search(attrs)
        url_m = RE_URL.search(body)

        # 没有 articleid 的卡片直接跳过（实测每页 1 张）：不满足"每条爆料有唯一
        # id"的前提，硬塞进去会让去重逻辑出错。
        if not (aid_m and url_m):
            continue

        # articleid 形如 "3_183089533"：前缀 "3_" 是所有卡片的固定标记，真 id 是
        # 后半段（URL 和 sensors payload 里用裸数字）——剥掉才能保证同一商品 id 一致。
        source_id = aid_m.group(1).split("_", 1)[-1]

        title_m = RE_TITLE.search(body)
        if not title_m or not _text(title_m.group(1)):
            # 没有标题 = 没有归并比对的输入，留着只会污染统计。
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
                # ★ 填的是**我方通道**，不是源站的 channel 标记，理由见上面 CHANNEL 那段。
                channel=CHANNEL,
            )
        )

    return deals


# --- 分层降级链 ---


def looks_blocked(html: str) -> bool:
    """判断响应是不是 WAF 拦截图。

    ★ 判据是体积和跳转，**不看 probev3.js**（正常页也有它，拿它当判据会让全站
    抓取失效）。单独抽成函数是为了能被测试直接调用——"被拦截要能认出来"必须有
    测试守着，否则 WAF 换个拦法，空结果会被当成"这个页面确实没有爆料"。
    """
    if len(html) < BLOCK_MIN_BYTES:
        return True
    return bool(RE_BLOCK_REDIRECT.search(html))


def looks_like_ugc(html: str) -> bool:
    """判断响应是不是分类页 / UGC 晒物页（本项目不采集）。

    ★ 单独一个判据而不是并进 looks_blocked：处理方式完全不同——被拦截要停下报警
    （对方在挡我们），UGC 要明确拒绝（是我们自己选择不采）；合并后日志只会写
    "被拦截"，而真相是"抓错页面了"，排查方向完全相反。
    """
    return bool(RE_UGC_MARKER.search(html))


# ★ 降级链。顺序即优先级。
# ★ 这里【没有】"JSON 接口"那一层（计划里写过）：调研时没找到可用的 JSON 接口，
# 写一个没被验证过的层只会让代码看起来比实际能力强——不写没验证的东西更重要。
LAYERS: tuple[Layer, ...] = (
    Layer(name="html", parse=parse_feed),
    # 浏览器层：渲染后的 HTML 交给同一个 parse_feed。★ needs_browser=True，
    # 默认（allow_browser=False）下 try_layers 会直接跳过——一次都不会被执行。
    Layer(name="browser", parse=parse_feed, needs_browser=True),
)


# --- 采集源 ---


class SmzdmSource(DataSource):
    """SMZDM 采集源。

    ★ 默认 `allow_network=False`：fetch() 直接抛异常，而不是静默返回空——静默返回空
    最坏，会让"没有联网"看起来像"抓到了 0 条"，两者排查方向完全相反。
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
            # ★ 这里抛 NetworkDisabled 而**不是** FetchBlocked：两者都会让采集停下，
            # 但一个是我们自己关着开关、一个是对方在挡我们——混用一个类型会把
            # "忘了加 --allow-network"说成"被 WAF 拦了"（见 base.py 的注释）。
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
            # ★ 上限是硬约束：不设的话，"多抓几页"是个太容易做出的决定。
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

                # ★ 顺序同 parse_files：先认 UGC，再认拦截。分类页体积很大、不会
                # 触发体积判据；两者都命中时"URL 填错了"是更准确的解释。
                if looks_like_ugc(resp.text):
                    stats.pages_ugc += 1
                    stats.layer = layer_name
                    break

                if resp.status_code != 200 or looks_blocked(resp.text):
                    # ★ 被拦截就【立刻停止】，不重试、不换 IP、不绕验证码——
                    # 重试只会让对方更确定这是脚本，也让自己更快被封。
                    stats.pages_blocked += 1
                    stats.layer = layer_name
                    if not all_deals:
                        # 一条都没抓到就是彻底失败，抛出去让任务变 failed，而不是
                        # "成功但 0 条"——后者会被当成"页面本来就没内容"。
                        raise FetchBlocked(
                            f"第 {page} 页被拦截（HTTP {resp.status_code}，"
                            f"{len(resp.text)} 字节）。已停止本次采集。"
                        )
                    break

                name, deals = try_layers(
                    resp.text, LAYERS, allow_browser=self.allow_browser
                )
                if not deals:
                    # 页面正常、没被拦、不是 UGC，却一条都解析不出来：★ 和"被拦截"
                    # 分开计数——这个信号意味着**解析器该改版了**，不是 WAF 的问题。
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

        ★ 这是本项目【实际使用】的采集路径：页面是从 smzdm.com 真实抓下来的，
        重放得到真实数据——与"伪造 source='smzdm' 的数据"有本质区别（区别在于
        数据是不是从真站点来的，docs/可行性评估.md 14.6 / 15.7）；且 CI 和别人的
        机器上都能跑，不需要联网、不会被 WAF 拦。
        """
        stats = FetchStats()
        all_deals: list[RawDeal] = []
        layer_name = ""

        for path in paths:
            html = path.read_text(encoding="utf-8", errors="replace")

            # ★ 顺序有讲究：先认 UGC 再认拦截。分类页体积大（500KB+）不触发体积
            # 判据；若哪天 WAF 开始拦分类页两者同真，那时"抓错页面了"更准确。
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
                # 页面拿到了、没被拦、不是 UGC，却解析不出 = 页面结构变了。必须和
                # "被拦截"分开报，否则改版时日志写"被拦截"、人跑去研究 WAF。
                stats.pages_unparsed += 1

        stats.layer = layer_name
        return layer_name, all_deals, stats


def dedupe_deals(deals: list[RawDeal]) -> list[RawDeal]:
    """按 source_id 去重，保留首次出现的那条。

    ★ 多页之间会重叠：实测 140 条原始结果里只有 100 条唯一（jingxuan.html 和
    jingxuan_p1.html 是同一页）。不去重数据也不会错（唯一约束挡住），但
    products_merged 统计会翻倍——"归并 40 条"实际是 0 条，统计错比数据错更难发现。
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

    ★ 故意留成一个抛异常的函数而不是写一段跑不了的 Playwright 代码：页面是
    服务端渲染，httpx 足够（实测 100 条全字段解析成功），这一层从未被需要；更要紧
    的是它【不能】跑在 API 进程里（Windows 上 uvicorn --reload 落到
    SelectorEventLoop，不支持子进程），真要用只能进 scraper/worker.py 独立进程。
    """
    raise BrowserUnavailable(
        "浏览器降级层尚未实现。本项目页面是服务端渲染，httpx 已足够"
        "（实测 100 条真实爆料全字段解析成功），因此没有安装 Playwright。"
        "真需要时：pip install playwright && playwright install chromium，"
        "然后只允许在 scraper/worker.py 独立进程里使用。"
    )
