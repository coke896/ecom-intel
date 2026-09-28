"""数据源抽象 —— 所有采集源的统一形状。

================================================================================
这个模块存在的理由
================================================================================
Mock 源和 SMZDM 源的差别，比"一个造假数据一个抓网页"要小得多：

    Mock：  内存里生成 → 得到一批爆料
    SMZDM： HTTP 请求   → 得到一批爆料

**从"得到一批爆料"往后，两者走的是【完全同一条路】**：
清洗 → 归并判定 → upsert 商品 → 写价格点。
如果让两个源各自实现这条后半段，就会出现"改归并策略要改两处"的老问题
（这个坑本项目已经踩过三次了，见 docs/可行性评估.md 第十五节）。

所以这个文件只做一件事：**定义"一批爆料"长什么样，以及源要提供哪些方法**。
后半段管道由 `scraper/ingest.py` 统一实现。

================================================================================
★ 分层降级：结构是真的，但我要说清楚哪几层真的在用
================================================================================
计划里写的是「JSON 接口 → HTML 解析 → Playwright」三层。

**实际落地时，JSON 那层被我删掉了，因为它从头到尾没有被证实存在。**
（见 docs/可行性评估.md 第十六节 16.1）
写一个"理论上应该有"的层塞进降级链，只会让代码看起来比它实际的能力强——
面试官问一句"JSON 接口的响应长什么样"就露馅了。

所以真正存在的是两层：

    第 1 层  html        httpx 拿页面 + 正则解析          ← 唯一被真实数据验证过的
    第 2 层  browser     用 Playwright 渲染后再交给第 1 层  ← 结构就位，默认关闭

★ 第 2 层为什么默认关闭，而且是**必须**关闭：

  SMZDM 的 /jingxuan/ 是服务端渲染的，httpx 就够（实测 100 条）。
  浏览器层唯一的作用是"万一哪天改成前端渲染了"。

  代价则很实在：`playwright install chromium` 要下 150MB 二进制。
  为一个用不上的降级层让每个 clone 项目的人都下 150MB，不划算。

  （Windows 上还有一条硬约束：uvicorn --reload 的 worker 跑在
    SelectorEventLoop 上，而它不支持子进程，Playwright 启动 Chromium 必崩。
    这条正是"采集层必须是独立进程"的原因之一，见 scraper/worker.py。）
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

# ===========================================================================
# 数据形状
# ===========================================================================


@dataclass(frozen=True, slots=True)
class RawDeal:
    """数据源解析出来的**原始**爆料 —— 一个字都还没洗。

    ★ 为什么叫 Raw 而不是直接叫 Deal：
      这个结构里的 `price_raw` 是 `"130元（需用券）"` 这样的原文，
      `brand` 是 `"SKECHERS/斯凯奇"` 这样的源站写法。
      **清洗是下一层（ingest）的事，不是解析层的事。**

      分开的好处很实际：解析层可以对着真实页面 fixture 断言"我抽出来的
      就是页面上写的那串字"，而不必在测试里同时关心"这串字洗出来是多少"。
      两个关注点混在一起时，一个测试挂了要同时怀疑解析器和清洗器。

    ★ frozen=True：这类对象会作为参数在管道里传来传去，
      做成不可变能防止下游意外改掉原始值——而"原始值被改过"
      在排查时是最难发现的一类问题（因为改了之后它依然是个合法值）。
    """

    source: str              # 数据血缘：mock / smzdm
    source_id: str           # 源站 id。去重的业务主键，必须稳定
    title: str

    # 价格原文。★ 允许 None：真实页面里确实有卡片没写价格（实测 100 条里 2 条）。
    #   None 表示"页面上没有这个字段"，和"字段是空串"是两回事——
    #   前者说明源站没提供，后者说明解析器可能出了问题。
    price_raw: str | None = None

    # 源站的发布时间。★ 它决定价格点落在趋势图的哪个位置。
    #   SMZDM 的卡片上有 timesort（unix 秒），是真实数据能画出价格序列的唯一依据。
    recorded_at: datetime | None = None

    url: str | None = None
    image_url: str | None = None
    brand: str | None = None
    platform: str | None = None
    category: str | None = None
    # ★ 我方采集通道，由**数据源自己**声明。取值必须落在 db/models.py
    #   声明的那一族里（mock / jingxuan / fenlei），因为这个值决定了
    #   price_history.recorded_at 的语义，前端坐标轴的标签也按它查表。
    #
    # ★ 这行注释是补上的。原先这个字段**一个字都没有**，而缺注释的代价
    #   很快就兑现了：smzdm 源往这里填了源站自己的 `'channel':'youhui'`
    #   标记（那是源站对文章的类型分类，和这个字段只是名字撞了），
    #   于是 100 条真实数据的 source_channel 全部落在声明取值族之外。
    #   "字段没有写清语义"和"字段被填错"之间，只隔了一个人。
    channel: str | None = None

    # ★ 销量。**允许 None，而且 None 是常态。**
    #   实测 SMZDM 好价页整页 HTML 里"已售"出现 0 次、"销量"出现 0 次
    #   （见 docs/可行性评估.md 发现 2）——真实电商的销量不在网页端公开。
    #   所以 Mock 源给值、SMZDM 源恒为 None。
    #
    #   ★ 这个字段放在 RawDeal 上而不是写死在 ingest 里，
    #     是为了让"哪些源提供销量"这件事成为**数据**而不是管道里的分支。
    #     写死的话，管道就变成"知道 SMZDM 没有销量"的代码了——
    #     而 base.py 的整个设计前提是管道不认识任何一个源的名字。
    sales: int | None = None

    # ★ 店铺名。同样是"有的源有、有的源没有"：
    #   SMZDM 的爆料没有店铺概念（只有平台：京东/天猫/淘宝），
    #   而 Mock 数据用它撑起"店铺占比饼图"。
    #   给了名字的源，管道会负责 resolve-or-create 出 shops 行。
    shop_name: str | None = None


@dataclass(frozen=True, slots=True)
class CrawlReport:
    """一轮采集的结果统计。

    ★ 这些数字必须来自**数据库的真实反馈**，不能是应用层数出来的：
      `new_prices` 是 `record_prices()` 的返回值（RETURNING 的行数），
      不是 `len(points)`——后者会把"已存在被跳过"的那些也算进去，
      于是界面上永远显示"本轮新增 100 条"，哪怕一条新的都没有。
    """

    fetched: int = 0            # 解析出多少条爆料
    products_new: int = 0       # 新建了多少个商品
    products_merged: int = 0    # 归并到已有商品多少条
    products_marked: int = 0    # 灰色地带：新建并标记 suspected_duplicate_of
    products_refreshed: int = 0 # ★ 这条爆料库里已经有主了，只刷新不新建
    prices_new: int = 0         # 真正新增的价格点（数据库反馈）
    pages_ok: int = 0
    pages_blocked: int = 0
    # ★ 下面两个也必须在【报告】里，不能只留在 FetchStats。
    #
    #   理由和 pages_blocked 一样（三种"没产出数据"的处置完全不同），
    #   而且更隐蔽：它们原先只在**一页都没抓到**的时候才被人看见
    #   （那条路径走 worker._describe_empty_fetch），
    #   而"5 页里坏了 1 页"恰恰是页面改版最常见的形态——
    #   那种情况下任务显示 success、消息里一个字都不提，
    #   于是解析器慢慢腐烂而没人知道。
    #
    #   计数器默默涨着、却没有任何一条路径把它送到人眼前，
    #   等于没计。这一条是本项目反复出现的同一类问题。
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
        # ★ 刷新数单独报，不要并进"新商品"。
        #   第二轮采集同一批页面时，正确的输出是
        #     "解析 100 条，新商品 0，刷新 100，新增价格点 0"
        #   这一行读起来就说明"增量采集真的在工作"。
        #   而如果把它算进新商品，第二轮会显示"新商品 100"——
        #   数据其实完全正确（upsert 更新了同一行），只是报告在说谎，
        #   而且是最容易被面试官一眼抓住的那种谎（"你连抓两次说新建了 200 个商品？"）。
        if self.products_refreshed:
            parts.append(f"刷新 {self.products_refreshed}")
        parts.append(f"新增价格点 {self.prices_new}")
        # ★ 三种"这一页没产出数据"都要报，而且必须分开报：
        #   被拦 → 别重试、别加频率；URL 填错 → 改配置；结构变了 → 改解析器。
        #   合成一句"有页面没抓到"的话，人会跑去研究 WAF。
        #   前缀的 ★ 是给前端看的：这条消息里带 ★ 就说明有需要处理的事。
        if self.pages_blocked:
            parts.append(f"★ {self.pages_blocked} 页被拦截")
        if self.pages_ugc:
            parts.append(f"★ {self.pages_ugc} 页是分类页/UGC（URL 填错了？）")
        if self.pages_unparsed:
            parts.append(f"★ {self.pages_unparsed} 页解析出 0 条（页面结构可能变了）")
        return "，".join(parts)


# ===========================================================================
# 异常：★ 分类型，是为了让调用方能【分别处理】
# ===========================================================================


class SourceError(RuntimeError):
    """采集源相关的错误基类。"""


class FetchBlocked(SourceError):
    """被 WAF / 反爬拦截。

    ★ 单独一个类型，因为它该触发的动作和别的错误不同：
      网络超时应该重试，被拦截【不应该重试】——
      重试只会让对方更确定这是脚本，也让自己更快被封。

      实测：SMZDM 在约 50~100 次请求后返回腾讯 TCaptcha 挑战页，
      之后直接 403。这个类型就是那次实测的产物。
    """


class NetworkDisabled(SourceError):
    """这个源被策略禁止联网（不是被对方拦截）。

    ★ 为什么它必须和 `FetchBlocked` 分开——两者都会被"采集停止"，
      但**原因和处置完全相反**：

        FetchBlocked     对方在挡我们      → 别重试、别加频率、今天别采了
        NetworkDisabled  我们自己关着开关  → 打开开关就行（--allow-network）

      用一个类型表示两件事，失败任务的 message 就只能是笼统的
      "采集停止"。而那个 message 会把人带去研究 WAF，
      真相却是他忘了加一个命令行参数。

      ★ 这个类是被一次真实的输出逼出来的：往队列里发一个 source=smzdm 的任务，
        然后用默认参数跑 worker，任务失败信息写着
        "被拦截，已按约定停止（不重试）"——而当时根本没发出任何网络请求。
        错的不是程序，是这句话把"不允许"说成了"被拒绝"。
    """


class BrowserUnavailable(SourceError):
    """需要浏览器层，但浏览器没装 / 不允许用。"""


# ===========================================================================
# 分层降级
# ===========================================================================


@dataclass(frozen=True, slots=True)
class Layer:
    """降级链里的一层。

    ★ `parse` 是**纯函数**（str → list[RawDeal]，不碰网络、不碰数据库）。
      这样"测试某一层的解析能力"就退化成"喂一段 HTML 给它"，
      不需要起服务器、不需要联网、也就能在 CI 里跑。
      采集代码最容易写成一坨"请求+解析+入库"的混合物，
      那样测试只能靠真联网——而真联网的测试既慢又不稳定，
      最后一定会被跳过，于是解析逻辑就没有测试了。
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

    ★ 判据是"解析出非空结果"，不是"这一层没抛异常"。
      因为降级链要处理的典型情况恰恰是"请求成功了、页面也拿到了，
      但页面结构变了导致解析出 0 条"——这时不抛异常的层会把
      空结果一路传下去，最后表现为"抓取成功但一条数据都没有"。
      （spike 阶段真的这么踩过：只看排行榜小组件的结构写解析器，
        页面能打开但一条都抓不到。见 spike/fetch_jingxuan.py 顶部注释。）

    ★ 返回层名是为了让调用方知道**数据是哪一层给的**。
      否则"今天是 HTML 层给的，明天变成浏览器层给的"这件事无人知晓，
      而两层的解析结果可能有细微差异。
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


# ===========================================================================
# 数据源协议
# ===========================================================================


@runtime_checkable
class DataSource(Protocol):
    """采集源的形状。

    ★ 用 Protocol 而不是 ABC：Mock 源根本不需要继承任何东西，
      它只要"长得像"就行。这跟 `app/analysis/glm_client.py` 里
      `AnalysisClient` 的做法是同一个理由——
      测试里的假实现不该被迫继承一个它们并不需要的基类。
    """

    name: str

    def fetch(self, *, pages: int) -> tuple[str, list[RawDeal], "FetchStats"]:
        """抓取并解析。

        返回 (层名, 爆料列表, 统计)。
        统计里带"几页成功几页被拦截"——这个信息前端要显示，
        而且"被拦截"必须能被看见，不能被静默吞掉。
        """
        ...


@dataclass(slots=True)
class FetchStats:
    """一次 fetch 的过程统计（可变，由 fetch 内部累加）。

    ★ 和 CrawlReport 分开：这个统计的是**抓取阶段**，
      CrawlReport 统计的是**入库阶段**。
      合成一个的话，"抓到了但一条都没入库"和"根本没抓到"
      在数字上会长得一样。
    """

    pages_ok: int = 0
    # ★ 下面三个都是"这一页没产出数据"，但【处理方式完全不同】，
    #   所以分开计数。合成一个的话，日志里只会写"被拦截"，
    #   而真相可能是"页面改版了"或"URL 填成了分类页"——
    #   排查方向完全相反，而人会被错误的那句话带去研究 WAF。
    pages_blocked: int = 0   # WAF 拦了（对方在挡我们）
    pages_ugc: int = 0       # 分类页/UGC（我们自己选择不采）
    pages_unparsed: int = 0  # 页面正常但解析出 0 条（页面结构变了）
    layer: str = ""
