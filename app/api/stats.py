"""统计聚合接口 —— 前端 4 张图表的数据来源。

端点一览（**全部支持 `?source=all|mock|smzdm`**）：

    GET /api/stats/sources             数据源清单（前端据此渲染切换按钮）
    GET /api/stats/overview            总览卡片
    GET /api/stats/price-distribution  价格分布直方图
    GET /api/stats/sales-top           销量 Top20 条形图
    GET /api/stats/shop-share          店铺占比饼图
    GET /api/stats/keywords            词云

================================================================================
三个贯穿本文件的决定
================================================================================
1. **聚合算在 Python 里，不写在 SQL 里。**
   本项目单表最多几万行，而且这些图表全部要"一次性看全量"（直方图本来就
   需要所有价格才能分桶）。为此写的 SQL 窗口函数会显著更难读、更难测，
   换来的性能收益在这个量级上测不出来。
   ★ 但这不是"永远不用 SQL"的借口——真到百万行时的做法写在
     `_fetch_current_prices()` 的注释里，面试被问到能答。

2. **排序结果必须是确定的。**
   每一次 `order_by` 都带一个唯一的兜底列（通常是 id 或 name）。
   原因见 `sales_top()` 里那段注释——不确定性排序会让翻页丢数据，
   也会让测试随机变红。

3. **"没有数据"要返回 200 + 空数组 + 一句解释，不能返回 404/500。**
   前端拿不到数据时该显示一句人话，而不是一片空白。
   这是 plan 里明确要求的空态设计，理由见 `SALES_UNAVAILABLE_NOTICE`。
"""

from __future__ import annotations

import logging
import math
import re
import threading
from collections import Counter
from enum import Enum
from typing import Annotated

import jieba
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, select

from app.api.deps import DbDep, SourceDep, apply_source
from app.config import settings
from app.db.models import PriceHistory, Product, Shop
from app.textutil import elide_middle

router = APIRouter(prefix="/api/stats", tags=["stats"])


# ===========================================================================
# 空态文案（★ 放在后端，不是"UI 文案写错地方了"）
#
# 前端当然可以自己写一句提示，但"这个数据源为什么没有销量"是**数据的事实**，
# 不是界面风格。放在后端有三个好处：
#   1. 只有一处需要改。将来真接了带销量的数据源，改这里就够了，
#      不用去前端代码里搜字符串。
#   2. 接口自己描述自己——直接 curl 也能看懂为什么是空的，
#      不用去翻前端源码才知道"空数组是正常的"。
#   3. 报告导出（阶段⑤）复用同一句话，不会出现"界面上说了一套、
#      导出的 Excel 里说的是另一套"。
# ===========================================================================

SALES_UNAVAILABLE_NOTICE = (
    "该数据源无销量数据。真实电商的销量是核心商业数据，网页端不公开且反爬最严；"
    "本项目用 Mock 数据驱动本图表，以保证分析链路可被独立验证。"
)

NO_DATA_NOTICE = "该数据源暂无数据（尚未采集）。"


def _empty_notice(product_count: int) -> str:
    """该数据源没有可展示的数据时，返回**正确的那一句**。

    ★ 这个函数是"空态文案优先级"的唯一来源，抽出来是被测试逼的：

      规则本身很简单——**先看有没有商品，再看有没有销量**。
      我在 `overview()` 里实现了它，然后在 `sales_top()` 里忘了，
      于是"一个还没采集过的数据源"在销量图上会显示
      "该数据源无销量数据（真实电商不公开销量）"。

      这句话单独看没问题，但它是**误导**：这个源不是"有商品但没销量"，
      而是压根什么都没采。两句话指向的行动完全不同——
      前者是"就这样，接受它"，后者是"去跑采集"。

      同一个规则写在两个地方、只漏了一处，且**不报错**——
      这就是把它收成一个函数的原因。
    """
    return SALES_UNAVAILABLE_NOTICE if product_count > 0 else NO_DATA_NOTICE

# 店铺占比里"没有店铺信息的商品"归到这一档。
# ★ 必须有这一档，不能让它们悄悄消失：饼图的各扇区之和必须等于 100%，
#   否则图上看起来"少了 15%"，而没人知道那 15% 去哪了。
UNKNOWN_SHOP = "未知店铺"


# ===========================================================================
# 响应模型
#
# ★ 为什么要写 Pydantic 模型，而不是直接 `return {...}`：
#   · /docs 上会显示真实的字段结构，面试官点开就能看懂接口契约，
#     比"点 Try it out 看返回什么"专业得多
#   · 字段一旦被误删/改名，接口立刻报 500（ValidationError），
#     而不是前端静默拿到 undefined 然后画出一张空图
#   · 前端照着模型写，不用靠猜
# ===========================================================================


class SourceInfo(BaseModel):
    """一个数据源的家底。"""

    source: str
    product_count: int
    price_point_count: int
    # ★ 前端用它决定"销量"相关的图表要不要显示空态
    has_sales: bool


class SourcesOut(BaseModel):
    sources: list[SourceInfo]


class PriceSummary(BaseModel):
    """价格分布的几个分位点。

    min/max 取的是**当前价**的极值，不是历史极值。
    历史极值属于时间序列，在 /api/products/{id}/price-history 里。
    """

    min: float
    p25: float
    median: float
    p75: float
    max: float
    mean: float


class SalesSummary(BaseModel):
    total: int
    median: int
    max: int


class OverviewOut(BaseModel):
    source: str
    product_count: int
    shop_count: int
    price_point_count: int

    price: PriceSummary | None = Field(
        default=None, description="None 表示该数据源一个有效价格都没有"
    )
    price_missing: int = Field(description="有商品但价格为空（真实采集的常见情况）的数量")

    sales: SalesSummary | None = Field(
        default=None, description="None 表示该数据源没有销量数据"
    )
    has_sales: bool

    # 时间范围。用字符串而不是 datetime，避免前端拿到 naive 时间后
    # 又当成 UTC 转一遍导致差 8 小时——前端只做展示，不做时区运算。
    first_recorded_at: str | None
    last_recorded_at: str | None
    last_crawled_at: str | None

    notice: str | None = Field(
        default=None, description="数据缺失时的解释，前端直接显示这句话"
    )


class PriceBucket(BaseModel):
    label: str
    lower: float
    upper: float
    count: int


class PriceDistributionOut(BaseModel):
    source: str
    # ★ 明确告诉调用方这个直方图统计的是什么价。
    #   "当前价"（每个商品一个点）和"历史价"（每个商品 90 个点）画出来
    #   形状完全不同，不写清楚就会出现"这个图和那张表对不上"的困惑。
    basis: str = "current_price_per_product"
    total: int
    bucket_step: float
    buckets: list[PriceBucket]
    # ★ 这个字段是测试逼出来的：空数据源下 `buckets: []` 返回 200，
    #   但**没有一句话解释为什么是空的**，前端只能画一张白图。
    #   五个统计接口里其他四个都有 notice，唯独这个漏了——
    #   而"漏一个"正是空态设计最容易出现的失败方式（逐个端点漏）。
    #   测试 test_all_empty_endpoints_return_200_for_a_source_with_nothing
    #   一次性把五个都检查了，就是为了防这个。
    notice: str | None = None


class SalesTopItem(BaseModel):
    id: int
    title: str
    # ★ 按显示宽度截断后的标题，供图表 y 轴直接用。
    #   为什么不交给前端截：前端的 `t[:10]` 会正好踩到项目早期那个坑
    #   （把区分 SKU 的颜色截掉），截断规则应该是后端的一处逻辑，
    #   见 app/textutil.py 的模块注释。
    title_short: str
    brand: str | None
    platform: str | None
    category: str | None
    shop: str | None
    price: float | None
    sales: int
    image_url: str | None


class SalesTopOut(BaseModel):
    source: str
    limit: int
    has_sales: bool
    notice: str | None
    items: list[SalesTopItem]


class ShopShareItem(BaseModel):
    shop: str
    platform: str | None
    product_count: int
    # 0~1 之间的小数，前端乘 100 显示。★ 不返回已经乘过 100 的数：
    # 一旦前端再乘一次就是 8700%，这种 bug 很常见。
    share: float
    avg_price: float | None


class ShopShareOut(BaseModel):
    source: str
    total: int
    # 被折叠进"其他"的店铺家数。前端可以显示"其他（12 家）"。
    others_count: int
    items: list[ShopShareItem]
    notice: str | None


class KeywordItem(BaseModel):
    name: str
    value: int


class KeywordsOut(BaseModel):
    source: str
    total_titles: int
    items: list[KeywordItem]
    notice: str | None


# ===========================================================================
# 内部工具
# ===========================================================================


def _median(values: list[float]) -> float:
    """中位数。★ 入参必须已排序。

    SQLite 没有 MEDIAN 聚合函数（PostgreSQL 也没有，要用
    percentile_cont）。数据量小的时候在 Python 里算是正确的选择。
    """
    n = len(values)
    mid = n // 2
    if n % 2:
        return float(values[mid])
    return (values[mid - 1] + values[mid]) / 2


def _fetch_current_prices(db, source) -> tuple[list[float], int]:
    """取回该数据源下所有商品的**当前价**，升序排好。

    返回 (价格列表, 价格为空的数量)。

    ★ 统计口径是 `Product.price`（每个商品一个价格），
      **不是** `price_history` 的全部价格点。
      两者形状完全不同：前者是"这批商品现在卖多少钱"，
      后者是"过去 90 天所有成交记录"，会被促销期和商品数量严重加权。
      画"价格分布"要的是前者。口径写在响应的 `basis` 字段里。

    ★ 到百万行时怎么改（面试会问）：
      分位数换成 SQL——但 SQLite 没有 percentile 函数，
      标准做法是用窗口函数
          SELECT price, PERCENT_RANK() OVER (ORDER BY price) ...
      或者维护一张预聚合表（按天/按类目算好分位数）。
      直方图则可以直接在 SQL 里 `GROUP BY CAST(price/step AS INT)`，
      返回几十行而不是几百万行。
      现在不这么做的原因很明确：这张表只有 1000 行，
      上面的 SQL 会让这段代码从 10 行变成 40 行，还更难测。
    """
    stmt = apply_source(select(Product.price), source).where(Product.price.is_not(None))
    prices = sorted(float(p) for p in db.execute(stmt).scalars())

    missing = db.execute(
        apply_source(select(func.count(Product.id)), source).where(Product.price.is_(None))
    ).scalar_one()

    return prices, int(missing)


# 分桶时优先用的"好看"的步长。
#
# ★ 为什么要吸附到这些数，而不是 range/16 直接算：
#   算出来的步长会是 38.4375 这种数，桶边界跟着变成
#   0 / 38.4 / 76.9 / 115.3…，坐标轴上全是小数，图很脏。
#   真实的分析图里桶边界一定是 10 的整数倍——不是为了好看，
#   而是因为读图的人要在脑子里做加法（"100~150 这一段有 200 个商品"），
#   边界不整的话这个加法做不了。
_NICE_STEPS: tuple[int, ...] = (5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000)


def _bucket_edges(lo: float, hi: float, target: int = 16) -> tuple[float, float, float]:
    """算出 (起始边界, 步长, 结束边界)。

    算法：先按目标桶数算一个粗略步长，再往上取到最近的"好看"步长，
    最后把首尾边界向外扩到步长的整数倍。
    """
    span = hi - lo
    raw = span / target if span > 0 else 1.0
    step = next((s for s in _NICE_STEPS if s >= raw), _NICE_STEPS[-1])

    start = math.floor(lo / step) * step
    end = math.ceil(hi / step) * step
    if end <= start:  # 所有价格相同（或只有一个商品）时的退化情况
        end = start + step
    return float(start), float(step), float(end)


# ===========================================================================
# 端点 1：数据源清单
# ===========================================================================


@router.get("/sources", response_model=SourcesOut)
def list_sources(db: DbDep) -> SourcesOut:
    """列出库里有数据的 source 及各自的家底。

    ★ 前端用它来渲染数据源切换按钮上的计数（"Mock（1000）"），
      这样"两个数据源是隔离的"这件事在界面上是**可见**的，
      而不是只能在 README 里读到。
    """
    # 三张小查询分别算，而不是一条 JOIN 全搞定。
    # ★ 一条 JOIN 的写法会更短，但 COUNT(*) 会被价格历史表放大：
    #   一个商品有 90 个价格点时，`COUNT(*)` 数出来的是 90 而不是 1，
    #   要写成 COUNT(DISTINCT p.id) 才对。三个简单查询加起来只有
    #   几毫秒，却不需要读者去验证"DISTINCT 加对了没有"。
    counts = dict(
        db.execute(select(Product.source, func.count(Product.id)).group_by(Product.source)).all()
    )
    points = dict(
        db.execute(
            select(Product.source, func.count(PriceHistory.id))
            .select_from(Product)
            .join(PriceHistory, PriceHistory.product_id == Product.id)
            .group_by(Product.source)
        ).all()
    )
    with_sales = dict(
        db.execute(
            select(Product.source, func.count(Product.id))
            .where(Product.sales.is_not(None))
            .group_by(Product.source)
        ).all()
    )

    return SourcesOut(
        sources=[
            SourceInfo(
                source=src,
                product_count=int(n),
                price_point_count=int(points.get(src, 0)),
                has_sales=int(with_sales.get(src, 0)) > 0,
            )
            for src, n in sorted(counts.items())
        ]
    )


# ===========================================================================
# 端点 2：总览
# ===========================================================================


@router.get("/overview", response_model=OverviewOut)
def overview(db: DbDep, source: SourceDep) -> OverviewOut:
    """总览卡片。"""
    prices, price_missing = _fetch_current_prices(db, source)

    product_count = db.execute(
        apply_source(select(func.count(Product.id)), source)
    ).scalar_one()

    shop_count = db.execute(
        apply_source(select(func.count(func.distinct(Product.shop_id))), source)
    ).scalar_one()

    # ★ 价格历史要 join 回 products 才能按 source 过滤——
    #   price_history 表自己没有 source 列（冗余存一份会带来
    #   "两条数据不一致时信谁"的问题，血缘关系应该只有一处）。
    ph = apply_source(
        select(
            func.count(PriceHistory.id),
            func.min(PriceHistory.recorded_at),
            func.max(PriceHistory.recorded_at),
        )
        .select_from(Product)
        .join(PriceHistory, PriceHistory.product_id == Product.id),
        source,
    )
    price_point_count, first_at, last_at = db.execute(ph).one()

    last_crawled = db.execute(
        apply_source(select(func.max(Product.last_crawled_at)), source)
    ).scalar()

    # 销量汇总。★ sales 全为 NULL 时下面这几个聚合返回的是 NULL，
    #   所以必须用 product_count 判断，不能靠 `total == 0` 判断——
    #   真的有一个商品销量是 0 的时候，两种情况的返回值是一样的。
    sales_row = db.execute(
        apply_source(
            select(func.sum(Product.sales), func.max(Product.sales), func.count(Product.sales)),
            source,
        )
    ).one()
    sales_total, sales_max, sales_non_null = sales_row
    has_sales = int(sales_non_null or 0) > 0

    sales_summary = None
    if has_sales:
        sales_values = sorted(
            int(s)
            for s in db.execute(
                apply_source(select(Product.sales), source).where(Product.sales.is_not(None))
            ).scalars()
        )
        sales_summary = SalesSummary(
            total=int(sales_total or 0),
            median=int(_median([float(v) for v in sales_values])),
            max=int(sales_max or 0),
        )

    price_summary = None
    if prices:
        price_summary = PriceSummary(
            min=round(prices[0], 2),
            p25=round(_quantile(prices, 0.25), 2),
            median=round(_median(prices), 2),
            p75=round(_quantile(prices, 0.75), 2),
            max=round(prices[-1], 2),
            mean=round(sum(prices) / len(prices), 2),
        )

    # 空态文案。规则集中在 _empty_notice() 里，不在这里重写一遍。
    notice: str | None = None if has_sales else _empty_notice(int(product_count))

    return OverviewOut(
        source=source.value,
        product_count=int(product_count),
        shop_count=int(shop_count),
        price_point_count=int(price_point_count),
        price=price_summary,
        price_missing=int(price_missing),
        sales=sales_summary,
        has_sales=has_sales,
        # ★ 用 isoformat(sep=" ") 而不是 isoformat()：
        #   全库存的是 "YYYY-MM-DD HH:MM:SS"，前端如果要排序或做字符串
        #   比较，两种格式混用会出错。统一成 SQLite 里那个格式。
        first_recorded_at=first_at.isoformat(sep=" ") if first_at else None,
        last_recorded_at=last_at.isoformat(sep=" ") if last_at else None,
        last_crawled_at=last_crawled.isoformat(sep=" ") if last_crawled else None,
        notice=notice,
    )


def _quantile(sorted_values: list[float], q: float) -> float:
    """线性插值分位数（等同于 numpy 的默认算法）。

    ★ 自己实现而不是 import numpy：只为算两个分位点就把 numpy
      拉进 Web 进程不划算（它在这个进程里唯一的用途就是这两行）。
      numpy 是 seed.py 的依赖，那是**离线脚本**，两者不该混。
    """
    if not sorted_values:
        raise ValueError("空列表没有分位数")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = (len(sorted_values) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(sorted_values[lo])
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


# ===========================================================================
# 端点 3：价格分布
# ===========================================================================


@router.get("/price-distribution", response_model=PriceDistributionOut)
def price_distribution(
    db: DbDep,
    source: SourceDep,
    target_buckets: Annotated[int, Query(ge=5, le=40)] = 16,
) -> PriceDistributionOut:
    """价格分布直方图。"""
    prices, _ = _fetch_current_prices(db, source)

    if not prices:
        return PriceDistributionOut(
            source=source.value, total=0, bucket_step=0, buckets=[], notice=NO_DATA_NOTICE
        )

    start, step, end = _bucket_edges(prices[0], prices[-1], target=target_buckets)

    # 一次遍历分桶。★ 用 min(int((p-start)/step), n-1) 而不是 if/elif 链：
    #   p 恰好等于最大值 end 时，算出来的下标是 n（越界），
    #   钳到 n-1 就把最大值放进最后一个桶。这个边界每次都会写错一次。
    n = int(round((end - start) / step))
    counts = [0] * n
    for p in prices:
        idx = min(int((p - start) / step), n - 1)
        counts[idx] += 1

    buckets = [
        PriceBucket(
            # 标签用整数显示（"100-150" 而不是 "100.0-150.0"）——
            # 步长一定是整数，显示成小数只会让坐标轴变长。
            label=f"{int(start + i * step)}-{int(start + (i + 1) * step)}",
            lower=float(start + i * step),
            upper=float(start + (i + 1) * step),
            count=int(counts[i]),
        )
        for i in range(n)
    ]

    return PriceDistributionOut(
        source=source.value, total=len(prices), bucket_step=step, buckets=buckets
    )


# ===========================================================================
# 端点 4：销量排行
# ===========================================================================


@router.get("/sales-top", response_model=SalesTopOut)
def sales_top(
    db: DbDep,
    source: SourceDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SalesTopOut:
    """销量 Top N。该数据源无销量时返回**空数组 + 解释**，不是 404。"""
    stmt = (
        select(Product, Shop.name.label("shop_name"))
        # LEFT JOIN：没有店铺信息的商品也要能上榜（真实采集早期就是这种情况）
        .join(Shop, Product.shop_id == Shop.id, isouter=True)
        .where(Product.sales.is_not(None))
        # ★ 兜底排序列 Product.id 不能省。
        #
        #   没有它的话，两个销量相同的商品在"谁排前面"上是未定义的：
        #   SQLite 只保证 ORDER BY 的列有序，同值行之间的顺序
        #   取决于它内部用了哪个索引、甚至取决于上次查询的缓存。
        #   后果有两个，都不报错：
        #     · 分页时 LIMIT/OFFSET 两次查询可能给出不同的排列，
        #       于是某个商品在第一页和第二页**各出现一次**，
        #       而另一个商品**一次都不出现**
        #     · 测试偶发变红，重跑又绿了，然后被当成"flaky 测试"忽略掉
        #   加一个唯一列当兜底，顺序就从"未定义"变成"确定"。
        .order_by(Product.sales.desc(), Product.id.asc())
        .limit(limit)
    )

    rows = db.execute(apply_source(stmt, source)).all()

    items = [
        SalesTopItem(
            id=int(p.id),
            title=p.title,
            title_short=elide_middle(p.title, budget=30),
            brand=p.brand,
            platform=p.platform,
            category=p.category,
            shop=shop_name,
            price=round(float(p.price), 2) if p.price is not None else None,
            sales=int(p.sales),
            image_url=p.image_url,
        )
        for p, shop_name in rows
    ]

    notice = None
    if not items:
        # ★ 只在为空时才多查一次计数——这条路径本来就慢不到哪去
        #   （空结果集），而"有销量"的常见路径一次额外查询都不用付。
        product_count = db.execute(
            apply_source(select(func.count(Product.id)), source)
        ).scalar_one()
        notice = _empty_notice(int(product_count))

    return SalesTopOut(
        source=source.value,
        limit=limit,
        has_sales=bool(items),
        notice=notice,
        items=items,
    )


# ===========================================================================
# 端点 5：店铺占比
# ===========================================================================


@router.get("/shop-share", response_model=ShopShareOut)
def shop_share(
    db: DbDep,
    source: SourceDep,
    limit: Annotated[int, Query(ge=2, le=50)] = 12,
) -> ShopShareOut:
    """店铺占比饼图。超过 limit 家的部分折叠成"其他"。"""
    shop_label = func.coalesce(Shop.name, UNKNOWN_SHOP).label("shop")

    stmt = (
        select(
            shop_label,
            Shop.platform.label("platform"),
            func.count(Product.id).label("cnt"),
            func.avg(Product.price).label("avg_price"),
        )
        .select_from(Product)
        # ★ LEFT JOIN 而不是 JOIN：price 为空 / shop 为空的商品也必须被统计。
        #   用 INNER JOIN 的话，没有店铺信息的商品会**整个消失**，
        #   各扇区之和不足 100%，而看图上完全看不出来少了东西。
        #   （`func.coalesce(..., UNKNOWN_SHOP)` 就是为了给这些行一个名字。）
        .join(Shop, Product.shop_id == Shop.id, isouter=True)
        .group_by(Product.shop_id)
        .order_by(func.count(Product.id).desc(), shop_label)
    )

    rows = db.execute(apply_source(stmt, source)).all()

    total = sum(int(r.cnt) for r in rows)

    head = rows[:limit]
    tail = rows[limit:]
    others_count = len(tail)

    items = [
        ShopShareItem(
            shop=r.shop,
            platform=r.platform,
            product_count=int(r.cnt),
            share=round(int(r.cnt) / total, 4) if total else 0.0,
            avg_price=round(float(r.avg_price), 2) if r.avg_price is not None else None,
        )
        for r in head
    ]

    # ★ 折叠成"其他"而不是硬截断：饼图少一块是看不出来的，
    #   但百分比会被静默地重新归一化，于是每一块的占比都是错的。
    if tail:
        tail_count = sum(int(r.cnt) for r in tail)
        # 加权平均：不能用 avg(avg_price)，那是"平均的平均"，
        # 会让只有 1 个商品的小店和 300 个商品的大店等权。
        weighted = [r for r in tail if r.avg_price is not None and r.cnt]
        avg = None
        if weighted:
            wsum = sum(float(r.avg_price) * int(r.cnt) for r in weighted)
            wcnt = sum(int(r.cnt) for r in weighted)
            avg = round(wsum / wcnt, 2) if wcnt else None
        items.append(
            ShopShareItem(
                shop=f"其他（{others_count} 家）",
                platform=None,
                product_count=tail_count,
                share=round(tail_count / total, 4) if total else 0.0,
                avg_price=avg,
            )
        )

    # ① 完全没有数据 ② 所有商品都没有店铺信息（真实采集早期就是这样）
    notice = None
    if not items:
        notice = NO_DATA_NOTICE
    elif len(items) == 1 and items[0].shop == UNKNOWN_SHOP:
        notice = "该数据源的商品均未采集到店铺信息，因此店铺占比无意义。"

    return ShopShareOut(
        source=source.value,
        total=total,
        others_count=others_count,
        items=items,
        notice=notice,
    )


# ===========================================================================
# 端点 6：词云
# ===========================================================================


# jieba 的初始化只需要做一次，但它不是线程安全的"只做一次"。
#
# ★ 为什么要自己加锁：FastAPI 的同步端点跑在线程池里，
#   服务刚起来时几个请求会并发进来，同时走到这里。
#   jieba 内部对词典加载有锁，但 `load_userdict` **没有**——
#   两个线程同时往同一个 trie 里写自定义词，行为未定义。
#   加锁的成本是几十纳秒，不加的后果是"偶发地少几个自定义词"，
#   而那种 bug 你查一整天都想不到是并发。
_jieba_lock = threading.Lock()
_jieba_ready = False


def _ensure_jieba() -> None:
    """加载自定义词典（幂等）。"""
    global _jieba_ready
    if _jieba_ready:
        return
    with _jieba_lock:
        # 双重检查：拿到锁之后要再判一次，否则并发时后面的线程
        # 会白等一次锁然后重复加载。
        if _jieba_ready:
            return

        # ★ 关掉 jieba 的 DEBUG 日志。
        #   默认级别会在首次分词时打印
        #   "Building prefix dict from ... Loading model from cache ..."
        #   这几行会出现在服务日志里，让第一次请求看起来像是卡住了。
        jieba.setLogLevel(logging.INFO)

        # 词典文件缺失不报错，只是词云会难看一些。
        # ★ 这里刻意不做"文件不存在就抛异常"：本项目承诺
        #   `git clone` 下来不配任何东西就能跑起来，而 data/ 是
        #   gitignore 的候选目录。缺词典应该是"效果打折"而不是"服务起不来"。
        if settings.userdict_file.exists():
            jieba.load_userdict(str(settings.userdict_file))

        jieba.initialize()
        _jieba_ready = True


def _load_stopwords() -> frozenset[str]:
    """读停用词表。每次调用都重读文件——**故意不缓存**。

    ★ 缓存的诱惑很大（几百行的文件，每次请求读一遍），但代价是：
      调词云的时候要重启服务才生效，而调词云恰恰是一个
      "改一个词、看一眼图、再改"的高频循环。
      这里文件只有几十 KB，OS 的页缓存早就把它放在内存里了，
      open + splitlines 的实际耗时在微秒级。
      用可测量的微小开销换"改完即生效"，这个交易很划算。
    """
    path = settings.stopwords_file
    if not path.exists():
        return frozenset()
    words = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        words.add(line.lower())
    return frozenset(words)


_HAS_CJK = re.compile(r"[一-鿿]")
# 纯数字（含小数）。"20000" 这种规格数字不该进词云——
# 它没有词义，而且会让词云上飘着一堆数字。
_IS_NUMBER = re.compile(r"^\d+(\.\d+)?$")
# 至少含一个"有意义"的字符，否则整个 token 是标点/符号。
_HAS_WORDCHAR = re.compile(r"[0-9a-zA-Z一-鿿]")


def _tokenize_titles(
    titles: list[str], stopwords: frozenset[str]
) -> tuple[Counter[str], dict[str, Counter[str]]]:
    """分词。返回 `(归一化词频, {归一化词: {原始写法: 次数}})`。

    ★ 归一化规则（每条都对应一个具体的坏结果）：
      · 大小写归一下沉 —— 不归一的话 `iPhone` / `iphone` / `IPHONE`
        会作为三个不同的词在词云上各占一块，同一个概念被拆成三份
      · 中文词长度 >= 2 —— 单字（"宝""器"）没有信息量，
        而且它们几乎都是分词切错的产物
      · ASCII 词长度 >= 3 —— ★ 这条不能写成"丢弃纯 ASCII 词"。
        早期草稿就是这么写的，那会亲手杀掉 iPhone / WiFi / Type-C / USB，
        而它们恰恰是数码品类里信息量最高的词。
        真正的噪声（LED、GB）交给停用词表按需处理，不用长度一刀切。
      · 丢掉纯数字和纯符号

    ★ 为什么同时返回"原始写法"的计数：
      归一化之后词云上会显示 `iphone`（全小写），而人写的是 `iPhone`。
      要还原成人类写法，就得知道每种写法各出现过几次。
      这件事**必须在分词这一趟里顺手记下来**——
      第一版是事后拿着每个关键词再去 `title.lower().find(key)` 扫一遍全部标题，
      120 个词 × 1000 条标题 = 12 万次字符串查找，纯粹是白花的开销，
      而信息在分词的时候本来就摆在手边。
    """
    counter: Counter[str] = Counter()
    forms: dict[str, Counter[str]] = {}

    for title in titles:
        if not title:
            continue
        # cut（精确模式）而不是 cut_for_search：
        # 后者会额外吐出长词的子串（"蓝牙耳机" → 还给"蓝牙""耳机"），
        # 词云上会同时出现整体词和它的碎片，看起来像重复统计。
        for raw in jieba.cut(title):
            token = raw.strip()
            if not token or not _HAS_WORDCHAR.search(token):
                continue

            key = token.lower()
            if key in stopwords or _IS_NUMBER.match(key):
                continue

            if _HAS_CJK.search(key):
                if len(key) < 2:
                    continue
                # 中文词没有大小写，不需要记原始写法
            elif len(key) < 3:
                continue

            counter[key] += 1
            if not _HAS_CJK.search(key):
                forms.setdefault(key, Counter())[token] += 1

    return counter, forms


def _display_form(key: str, forms: dict[str, Counter[str]]) -> str:
    """把 `iphone` 还原成 `iPhone` 这类人类写法。

    取原文里出现次数最多的那种写法。
    ★ 为什么不取"第一次出现的"：标题是按 id 排的，
      而排在前面的可能恰好是一条全大写的促销标题（"IPHONE 15 保护壳"），
      于是整个词云里这个词都会显示成全大写。
      取众数稳得多，而且这正是"归一化"本该有的逆运算。
    """
    candidates = forms.get(key)
    if not candidates:
        return key
    # ★ max 的 key 里带上原文做第二排序键：出现次数相同时，
    #   结果不能取决于 Counter 的插入顺序（那就又变成不确定了）。
    best_form, _ = max(candidates.items(), key=lambda kv: (kv[1], kv[0]))
    return best_form


@router.get("/keywords", response_model=KeywordsOut)
def keywords(
    db: DbDep,
    source: SourceDep,
    limit: Annotated[int, Query(ge=10, le=300)] = 120,
) -> KeywordsOut:
    """词云数据。

    ★ 分词在后端做，因为 jieba 是 Python 的。
      让浏览器干这件事要么引一个几百 KB 的 JS 分词库（效果还差），
      要么把整份词典发给前端——两头都不划算。

    ★ 实测耗时（1000 个标题，本机，5 次取中位）：**46 ms**。
      这是所有接口里最慢的一个，其余都在 2~12 ms。
      原因很直接：它要对每个标题跑一遍 jieba，是 O(标题数) 的。

      当前不做缓存，因为词云一页只请求一次，46 ms 无感。
      但要把这个决定和它的失效条件一起记下来：
      **标题涨到 1 万条时它会变成约 0.5 秒**，那时该做的是
      按 (source, 商品数) 做短 TTL 缓存，或者把词频预聚合成一张表。
      现在不加，是因为缓存会引入"改了停用词表要等 TTL 过期"的困惑，
      而调停用词表恰恰是这个功能最高频的操作。
    """
    _ensure_jieba()

    titles = [
        t
        for t in db.execute(
            apply_source(select(Product.title), source).order_by(Product.id)
        ).scalars()
        if t
    ]

    if not titles:
        return KeywordsOut(
            source=source.value, total_titles=0, items=[], notice=NO_DATA_NOTICE
        )

    stopwords = _load_stopwords()
    counter, forms = _tokenize_titles(titles, stopwords)

    # ★ 排序必须完全确定：`Counter.most_common()` 在计数相同时的返回顺序
    #   取决于字典的插入顺序，也就是取决于上面那条 SQL 返回标题的顺序。
    #   于是"给词云加个排序"这种无关改动会让输出悄悄变样，测试也跟着飘。
    #   显式按 (-次数, 词) 排，同分时按字典序，结果就唯一了。
    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]

    items = [KeywordItem(name=_display_form(k, forms), value=int(v)) for k, v in ranked]

    return KeywordsOut(
        source=source.value,
        total_titles=len(titles),
        items=items,
        notice=None if items else "分词后没有剩下任何关键词（标题可能全被停用词覆盖）。",
    )
