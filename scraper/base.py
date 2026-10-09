"""数据源抽象 —— 所有采集源的统一形状。

只定义"一批爆料"长什么样、源要提供哪些方法；清洗往后由
`scraper/ingest.py` 统一实现，Mock 与 SMZDM 走同一条管道。

分层降级实际只有两层（JSON 层未被证实存在，已删，见 docs/可行性评估.md §16.1）：
    html     httpx + 正则解析 —— 唯一被真实数据验证过的
    browser  Playwright 渲染后交给 html 层 —— 结构就位，默认且必须关闭
关闭理由：页面是服务端渲染，httpx 就够；且 Windows 上 uvicorn --reload 的
worker 落在 SelectorEventLoop（不支持子进程），Playwright 必崩——
它只能跑在采集独立进程里（scraper/worker.py）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

# --- 数据形状 ---


@dataclass(frozen=True, slots=True)
class RawDeal:
    """数据源解析出来的**原始**爆料 —— 一个字都还没洗。

    price_raw / brand 都是源站原文，清洗是下一层（ingest）的事；
    frozen=True 防止下游意外改掉原始值（"原始值被改过"最难排查）。
    """

    source: str              # 数据血缘：mock / smzdm
    source_id: str           # 源站 id。去重的业务主键，必须稳定
    title: str

    # 价格原文。允许 None：真实页面确实有卡片没写价格（实测 100 条里 2 条）；
    # None = 页面上没有这个字段，和"字段是空串"（解析器可能出错）是两回事。
    price_raw: str | None = None

    # 源站的发布时间，决定价格点落在趋势图的哪个位置。
    #   SMZDM 用卡片上的 timesort（unix 秒），是真实数据能画出价格序列的唯一依据。
    recorded_at: datetime | None = None

    url: str | None = None
    image_url: str | None = None
    brand: str | None = None
    platform: str | None = None
    category: str | None = None
    # 我方采集通道，由**数据源自己**声明。取值必须落在 db/models.py 声明的
    # 族里（mock / jingxuan / fenlei）：它决定 price_history.recorded_at 的语义，
    # 前端坐标轴的标签也按它查表。不能填源站自己的 'channel' 标记（那是文章类型）。
    channel: str | None = None

    # 销量。允许 None 且 None 是常态：真实电商的销量不在网页端公开
    #   （SMZDM 恒为 None，Mock 给值）。放在 RawDeal 上而不是写死进管道，
    #   是为了让"哪些源提供销量"成为数据，而不是管道里的分支。
    sales: int | None = None

    # 店铺名。同样是"有的源有、有的源没有"（SMZDM 没有店铺概念，只有平台）；
    #   给了名字的源，管道会负责 resolve-or-create 出 shops 行。
    shop_name: str | None = None


@dataclass(frozen=True, slots=True)
class CrawlReport:
    """一轮采集的结果统计。

    数字必须来自**数据库的真实反馈**：`new_prices` 是 record_prices() 的
    返回值（RETURNING 的行数），不是 len(points)——后者把已存在被跳过的
    也算进去，界面上会永远显示"本轮新增 100 条"。
    """

    fetched: int = 0            # 解析出多少条爆料
    products_new: int = 0       # 新建了多少个商品
    products_merged: int = 0    # 归并到已有商品多少条
    products_marked: int = 0    # 灰色地带：新建并标记 suspected_duplicate_of
    products_refreshed: int = 0 # 库里已经有主，只刷新不新建
    prices_new: int = 0         # 真正新增的价格点（数据库反馈）
    pages_ok: int = 0
    pages_blocked: int = 0
    # 这两个也必须在【报告】里，不能只留在 FetchStats：否则"5 页里坏了 1 页"
    # 的任务会显示 success、消息里一字不提，解析器慢慢腐烂而没人知道。
    pages_ugc: int = 0
    pages_unparsed: int = 0

    def as_message(self) -> str:
        """给 crawl_tasks.message 用的一句话摘要（前端直接显示）。"""
        if not self.fetched:
            return "没有解析到任何爆料（页面结构可能变了，或全部被拦截）"
        parts = [f"解析 {self.fetched} 条", f"新商品 {self.products_new}"]
        if self.products_merged:
            parts.append(f"归并 {self.products_merged}")
        if self.products_marked:
            parts.append(f"待核对 {self.products_marked}")
        # ★ 刷新数单独报，不要并进"新商品"：第二轮采集的正确输出是
        # "新商品 0，刷新 100"，并进去会显示"新商品 100"——报告在说谎。
        if self.products_refreshed:
            parts.append(f"刷新 {self.products_refreshed}")
        parts.append(f"新增价格点 {self.prices_new}")
        # ★ 三种"没产出数据"必须分开报：被拦 → 别重试；URL 错 → 改配置；
        # 结构变 → 改解析器。合成一句人会跑去研究 WAF。★ 前缀是给前端看的。
        if self.pages_blocked:
            parts.append(f"★ {self.pages_blocked} 页被拦截")
        if self.pages_ugc:
            parts.append(f"★ {self.pages_ugc} 页是分类页/UGC（URL 填错了？）")
        if self.pages_unparsed:
            parts.append(f"★ {self.pages_unparsed} 页解析出 0 条（页面结构可能变了）")
        return "，".join(parts)


# --- 异常：分类型，是为了让调用方能分别处理 ---


class SourceError(RuntimeError):
    """采集源相关的错误基类。"""


class FetchBlocked(SourceError):
    """被 WAF / 反爬拦截。

    ★ 单独一个类型：网络超时应该重试，被拦截**不该重试**——重试只会更快被封。
    实测 SMZDM 约 50~100 次请求后返回腾讯 TCaptcha 挑战页，之后直接 403。
    """


class NetworkDisabled(SourceError):
    """这个源被策略禁止联网（不是被对方拦截）。

    ★ 必须和 FetchBlocked 分开：两者都会"停止采集"，但原因和处置相反——
      对方在挡我们 → 别重试；我们自己关着开关 → 打开 --allow-network 就行。
      混成一个类型，失败消息会把"忘了加参数"说成"被拦截"，把人带去研究 WAF。
    """


class BrowserUnavailable(SourceError):
    """需要浏览器层，但浏览器没装 / 不允许用。"""


# --- 分层降级 ---


@dataclass(frozen=True, slots=True)
class Layer:
    """降级链里的一层。

    ★ `parse` 是**纯函数**（str → list[RawDeal]，不碰网络、不碰数据库）：
      测试某层解析能力就退化成"喂一段 HTML 给它"，不需要联网也能进 CI。
    """

    name: str
    parse: Callable[[str], list[RawDeal]]
    # 这一层是否需要真实浏览器（需要则会受"默认关闭"的约束）
    needs_browser: bool = False


def try_layers(
    html: str,
    layers: tuple[Layer, ...],
    *,
    allow_browser: bool,
) -> tuple[str, list[RawDeal]]:
    """按顺序试每一层，返回**第一个解析出非空结果**的那一层及其产出。

    ★ 判据是"解析出非空"而不是"没抛异常"：结构变了时后者会把空结果一路传
      下去，表现为"抓取成功但一条数据都没有"。返回层名让调用方知道数据来自哪层。
    """
    tried: list[str] = []
    for layer in layers:
        if layer.needs_browser and not allow_browser:
            continue
        tried.append(layer.name)
        deals = layer.parse(html)
        if deals:
            return layer.name, deals
    return "", []


# --- 数据源协议 ---


@runtime_checkable
class DataSource(Protocol):
    """采集源的形状。

    ★ 用 Protocol 而不是 ABC：只要"长得像"就行，测试里的假实现不该被迫
      继承一个它们并不需要的基类（app/analysis/glm_client.py 同理）。
    """

    name: str

    def fetch(self, *, pages: int) -> tuple[str, list[RawDeal], "FetchStats"]:
        """抓取并解析，返回 (层名, 爆料列表, 统计)。

        统计里带"几页成功几页被拦截"——"被拦截"必须能被看见，不能被静默吞掉。
        """
        ...


@dataclass(slots=True)
class FetchStats:
    """一次 fetch 的过程统计（可变，由 fetch 内部累加）。

    ★ 和 CrawlReport 分开：这个统计**抓取阶段**，CrawlReport 统计**入库阶段**——
      合成一个的话，"抓到了但一条都没入库"和"根本没抓到"在数字上会长得一样。
    """

    pages_ok: int = 0
    # 三个都是"这一页没产出数据"但处置完全不同（别重试 / 改 URL / 改解析器），
    # 所以分开计数——合成一个会把人带去研究 WAF。
    pages_blocked: int = 0   # WAF 拦了（对方在挡我们）
    pages_ugc: int = 0       # 分类页/UGC（我们自己选择不采）
    pages_unparsed: int = 0  # 页面正常但解析出 0 条（页面结构变了）
    layer: str = ""
