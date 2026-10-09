"""统计聚合接口 —— 前端 4 张图表的数据来源。

端点：sources（★ **不接受 ?source=**——它返回的就是"有哪些 source"，再按
source 过滤它没有意义）/ overview / price-distribution / sales-top /
shop-share / keywords，除 `/sources` 外都支持 `?source=all|mock|smzdm`。

三个贯穿本文件的决定：
1. **聚合算在 Python 里，不写在 SQL 里。** 单表最多几万行，而这些图表都要
   "一次性看全量"；到百万行时的做法写在 `_fetch_current_prices()` 的注释里。
2. **排序结果必须确定**：每次 `order_by` 都带一个唯一兜底列，否则翻页丢数据，
   测试也会随机变红（理由见 `sales_top()`）。
3. **"没有数据"返回 200 + 空数组 + 一句解释**，不返回 404/500。
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, select

from app.aggregate import bucket_prices, median, quantile
from app.api.deps import DbDep, SourceDep
from app.db.models import PriceHistory, Product, Shop
from app.keywords import rank_keywords
from app.source_scope import apply_source
from app.textutil import elide_middle

router = APIRouter(prefix="/api/stats", tags=["stats"])


# ---- 空态文案（★ 放在后端，不是"UI 文案写错地方了"）--------------------------
# "这个数据源为什么没有销量"是**数据的事实**，不是界面风格：放后端只有一处需要
# 改；接口自己描述自己（curl 也看得懂）；导出报告也复用同一句话。

SALES_UNAVAILABLE_NOTICE = (
    "该数据源无销量数据。真实电商的销量是核心商业数据，网页端不公开且反爬最严；"
    "本项目用 Mock 数据驱动本图表，以保证分析链路可被独立验证。"
)

NO_DATA_NOTICE = "该数据源暂无数据（尚未采集）。"


def _empty_notice(product_count: int) -> str:
    """该数据源没有可展示的数据时，返回**正确的那一句**。

    ★ 这是"空态文案优先级"的唯一来源。规则简单——**先看有没有商品，再看有没有
      销量**——但它曾在 `sales_top()` 漏掉，于是还没采集过的源显示"无销量数据"：
      那句话把人指向"接受它"而不是"去跑采集"，且不报错。
    """
    return SALES_UNAVAILABLE_NOTICE if product_count > 0 else NO_DATA_NOTICE


def _price_notice(price_missing: int, charted: int) -> str | None:
    """价格分布图那句话的**唯一来源**（三种状态，只有一处拼字符串）。

    ★ 抽成函数而不是在端点里拼：三种状态指向的**动作完全不同**——没有数据
      （尚未采集）→ 去跑采集；有商品但一个价格都没有 → 查价格解析器；有几个
      漏的 → 至少知道百分比的分母是几。
    ★ `charted` 和 `price_missing` 都出现在文案里：读者要能自己算出分母到底是几。
    """
    if charted == 0:
        if price_missing == 0:
            return NO_DATA_NOTICE
        # ★ 防的是"把'解析不出价格'说成'尚未采集'"：3 个商品全无价格时接口原本
        #   返回的正是"暂无数据"，而同一屏卡片写着"商品数 3"。触发路径不是假设：
        #   smzdm 的价格来自列表页 z-highlight，解析器一旦不匹配，价格全变 None。
        return (
            f"该数据源的 {price_missing} 个商品全部没有价格，画不出分布图。"
            f"这不是「尚未采集」——请检查价格解析器，或确认该数据源是否公开价格。"
        )
    if price_missing:
        return (
            f"另有 {price_missing} 个商品没有价格，无法归入任何价格区间，未计入本图；"
            f"图中的百分比以 {charted} 个有价格的商品为分母。"
        )
    return None

# 店铺占比里"没有店铺信息的商品"归到这一档。
# ★ 必须有这一档，不能让它们悄悄消失：饼图各扇区之和必须等于 100%，否则图上
#   看起来"少了 15%"，而没人知道那 15% 去哪了。
UNKNOWN_SHOP = "未知店铺"


# ---- 响应模型 ------------------------------------------------------------------
# 写 Pydantic 模型而不是直接 `return {...}`：/docs 上会显示真实的字段结构；字段
# 一旦被误删/改名会立刻 500（ValidationError），而不是前端静默拿到 undefined。


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

    ★ min/max 取的是**当前价**的极值，不是历史极值（历史极值在
      /api/products/{id}/price-history 里）。
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

    # 时间范围用字符串而不是 datetime，避免前端拿到 naive 时间后又当成 UTC 转一遍。
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
    # ★ 明确告诉调用方这个直方图统计的是什么价："当前价"（每个商品一个点）和
    #   "历史价"（每个商品 90 个点）画出来形状完全不同。
    basis: str = "current_price_per_product"
    # ★ `total` 是**能进桶的商品数**，不是这个数据源的商品总数——有商品缺价格时
    #   两者不相等（overview 的 product_count 才是总数），而前端拿它当分母，
    #   读的人自然以为是"占全部商品"。
    total: int
    # ★ 有商品、但没有价格的商品数（= 进不了任何柱子的那些）。名字和 overview 的
    #   `price_missing` 一致——同一个事实在两个接口上必须同名。
    price_missing: int = 0
    bucket_step: float
    buckets: list[PriceBucket]
    # ★ 测试逼出来的字段：空数据源下 `buckets: []` 返回 200，但没有一句话解释为
    #   什么是空的。五个统计接口里其他四个都有 notice，唯独这个漏了——而"漏一个"
    #   正是空态设计最容易出现的失败方式。
    notice: str | None = None


class SalesTopItem(BaseModel):
    id: int
    title: str
    # ★ 按显示宽度截断后的标题，供图表 y 轴直接用。截断规则应该只有后端一处
    #   （前端的 `t[:10]` 会正好踩到项目早期那个坑），见 app/textutil.py。
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
    """该店铺在本数据源下的商品数。★ **含无价商品**（口径见 ShopShareOut.total）。"""

    priced_count: int
    """其中有价格的商品数。

    ★ 这个字段存在的唯一理由是给 `avg_price` 一个**可见的分母**。
      `product_count` 是 100、`avg_price` 只基于其中 98 件，
      而界面上写着"商品数 100 ｜ 均价 ¥99"——读者会理所当然地
      把那个均价当成 100 件商品的均价。
      真实采集源今天就有这种情况（100 条里 2 条解析不出价格），
      而且这个比例**没有下限**。

      和 `/price-distribution` 返回 `price_missing` 是同一个动作：
      口径对不上的时候，把差额一并交给调用方，
      而不是让它从一个孤零零的 `total` 里去猜。
    """

    # 0~1 之间的小数，前端乘 100 显示。★ 不返回已经乘过 100 的数：
    # 一旦前端再乘一次就是 8700%，这种 bug 很常见。
    share: float
    avg_price: float | None
    """该店铺的均价。★ 只覆盖 `priced_count` 件，不是 `product_count` 件。"""


class ShopShareOut(BaseModel):
    source: str
    total: int
    """商品数。★ **含无价商品**——与 `/price-distribution` 的 `total`
    （只含**有价**商品）**不是同一个口径**，两者不能直接比。

    ★ 这里刻意跟直方图取不同的口径，理由在两张图的画法里：
      饼图的每一块是"这个店铺占多少商品"，无价商品**同样占地方**，
      排除掉会让扇区凭空变小、各扇区之和不再是全部商品；
      而直方图的每一根柱子是"多少商品落在这个价位"，
      无价商品**落不进任何一根柱子**，所以它只能在 total 之外，
      并靠 `price_missing` 把差额说出来。

    ★ 代价就是同一个字段名在两个接口里含义不同。所以两边都写明口径，
      并且都额外返回一个"差额"字段（这里是 `priced_count` 之和、
      那里是 `price_missing`），
      让"total 到底含不含无价商品"不靠读文档、靠读数据就能确认。
    """

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


# ---- 内部工具 --------------------------------------------------------------


def _fetch_current_prices(db, source) -> tuple[list[float], int]:
    """取回该数据源下所有商品的**当前价**，升序排好。返回 (价格列表, 空价数量)。

    ★ 口径是 `Product.price`（每商品一个价格），**不是** price_history 的全部
      价格点——后者是"过去 90 天所有成交记录"，会被促销期和商品数量严重加权。
      口径写在响应的 `basis` 字段里。
    ★ 到百万行时怎么改（面试会问）：分位数换 SQL 窗口函数
      （`PERCENT_RANK() OVER (ORDER BY price)`）或预聚合表；直方图直接
      `GROUP BY CAST(price/step AS INT)`。现在不做，因为这张表只有 1000 行，
      那些 SQL 只会让代码更难测。
    """
    stmt = apply_source(select(Product.price), source).where(Product.price.is_not(None))
    prices = sorted(float(p) for p in db.execute(stmt).scalars())

    missing = db.execute(
        apply_source(select(func.count(Product.id)), source).where(Product.price.is_(None))
    ).scalar_one()

    return prices, int(missing)


# ★ 分桶和分位数的实现在 app/aggregate.py，不在本文件：报告要喂给模型的价格
#   分布必须和本接口返回给前端的是**同一份计算**，否则图和结论可能对不上。


# ---- 端点 1：数据源清单 ----


@router.get("/sources", response_model=SourcesOut)
def list_sources(db: DbDep) -> SourcesOut:
    """列出库里有数据的 source 及各自的家底。

    ★ 前端用它渲染数据源切换按钮上的计数（"Mock（1000）"），让"两个数据源是
      隔离的"在界面上**可见**，而不是只能在 README 里读到。
    """
    # 三张小查询分别算，而不是一条 JOIN 全搞定。
    # ★ JOIN 写法更短，但 COUNT(*) 会被价格历史表放大（一个商品 90 个价格点会数
    #   出 90），要写 COUNT(DISTINCT p.id) 才对；三个简单查询只有几毫秒，却不需要
    #   读者去验证"DISTINCT 加对了没有"。
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


# ---- 端点 2：总览 ----


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

    # ★ 价格历史要 join 回 products 才能按 source 过滤——price_history 表自己没有
    #   source 列（冗余存一份会带来"两条数据不一致时信谁"的问题）。
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

    # 销量汇总。★ sales 全为 NULL 时聚合返回的是 NULL，所以必须用非空计数判断，
    #   不能靠 `total == 0` 判断——真的有一个商品销量是 0 时，两种情况返回值一样。
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
            median=int(median([float(v) for v in sales_values])),
            max=int(sales_max or 0),
        )

    price_summary = None
    if prices:
        price_summary = PriceSummary(
            min=round(prices[0], 2),
            p25=round(quantile(prices, 0.25), 2),
            median=round(median(prices), 2),
            p75=round(quantile(prices, 0.75), 2),
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
        # ★ 用 isoformat(sep=" ") 而不是 isoformat()：全库存的是
        #   "YYYY-MM-DD HH:MM:SS"，两种格式混用会让前端排序和字符串比较出错。
        first_recorded_at=first_at.isoformat(sep=" ") if first_at else None,
        last_recorded_at=last_at.isoformat(sep=" ") if last_at else None,
        last_crawled_at=last_crawled.isoformat(sep=" ") if last_crawled else None,
        notice=notice,
    )


# ---- 端点 3：价格分布 ----


@router.get("/price-distribution", response_model=PriceDistributionOut)
def price_distribution(
    db: DbDep,
    source: SourceDep,
    target_buckets: Annotated[int, Query(ge=5, le=40)] = 16,
) -> PriceDistributionOut:
    """价格分布直方图。

    ★ 没有价格的商品**进不了任何一根柱子**：既不在 `total` 里、也不在任何一个桶
      里（同文件的店铺占比就是这么处理的，见 UNKNOWN_SHOP）。各柱之和**是**等于
      total 的，图自身毫无破绽，出错的是读者对百分比的理解——前端拿 total 当分母
      （frontend/static/js/charts.js），"这个价位占 X%"被读成占全部商品。偏差
      今天不大（真实源 2.0%），但它**没有下限**，而 total 是对外公开的字段。
    """
    prices, price_missing = _fetch_current_prices(db, source)

    if not prices:
        return PriceDistributionOut(
            source=source.value,
            total=0,
            price_missing=price_missing,
            bucket_step=0,
            buckets=[],
            notice=_price_notice(price_missing, charted=0),
        )

    # ★ 分桶交给 app/aggregate.bucket_prices()，它返回的 dict 形状恰好就是
    #   PriceBucket 需要的四个字段——自己再写一遍会让界面和 AI 报告算出不同的桶。
    step, buckets = bucket_prices(prices, target=target_buckets)

    return PriceDistributionOut(
        source=source.value,
        total=len(prices),
        price_missing=price_missing,
        bucket_step=step,
        buckets=[PriceBucket(**b) for b in buckets],
        notice=_price_notice(price_missing, charted=len(prices)),
    )


# ---- 端点 4：销量排行 ----


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
        # ★ 兜底排序列 Product.id 不能省：没有它，两个销量相同的商品谁排前面是未
        #   定义的，后果两个且都不报错——分页时某商品在两页**各出现一次**、另一个
        #   **一次都不出现**；测试偶发变红、重跑又绿，然后被当成 flaky 忽略掉。
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
        # ★ 只在为空时才多查一次计数——"有销量"的常见路径一次额外查询都不用付。
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


# ---- 端点 5：店铺占比 ----


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
            # ★ 这个 count 是**下面加权平均的权重**，必须是"有价商品数"：`avg(price)`
            #   的分母是**非空**价格的个数，用 COUNT(Product.id) 会让缺价商品"以它没
            #   参与的那份均价"去加码；用对权重后，加权平均恰等于尾部有价商品的直接
            #   平均，可以拿一条 SQL 核对（test_api_smoke.py）。
            func.count(Product.price).label("priced_cnt"),
        )
        .select_from(Product)
        # ★ LEFT JOIN 而不是 JOIN：price 为空 / shop 为空的商品也必须被统计，否则
        #   各扇区之和不足 100%，而图上看不出少了东西（`coalesce` 给这些行一个名字）。
        .join(Shop, Product.shop_id == Shop.id, isouter=True)
        # ★ Shop.platform 必须写进 GROUP BY：SQLite 对裸列是宽容的，它**静默**从组
        #   里挑一个值给你，而 PostgreSQL 上这是硬错误——修的不是算错的数，是**可
        #   移植性**。不用 `group_by(shop_label, Shop.platform)`：那是按**别名**分组，
        #   会把两个 shop_id 不同的行（比如都叫 NULL 的）并成一组。
        .group_by(Product.shop_id, Shop.platform)
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
            priced_count=int(r.priced_cnt),
            share=round(int(r.cnt) / total, 4) if total else 0.0,
            avg_price=round(float(r.avg_price), 2) if r.avg_price is not None else None,
        )
        for r in head
    ]

    # ★ 折叠成"其他"而不是硬截断：饼图少一块是看不出来的，
    #   但百分比会被静默地重新归一化，于是每一块的占比都是错的。
    if tail:
        tail_count = sum(int(r.cnt) for r in tail)
        # 加权平均：不能用 avg(avg_price)（"平均的平均"会让小店和大店等权），
        # 权重是 priced_cnt 而**不是** cnt——见上面 select 里那段注释。
        weighted = [r for r in tail if r.avg_price is not None and r.priced_cnt]
        avg = None
        if weighted:
            wsum = sum(float(r.avg_price) * int(r.priced_cnt) for r in weighted)
            wcnt = sum(int(r.priced_cnt) for r in weighted)
            avg = round(wsum / wcnt, 2) if wcnt else None
        items.append(
            ShopShareItem(
                shop=f"其他（{others_count} 家）",
                platform=None,
                product_count=tail_count,
                priced_count=sum(int(r.priced_cnt) for r in tail),
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


# ---- 端点 6：词云 ----


# ★ 分词相关的全部实现（jieba 初始化、停用词表、分词规则、大小写还原）都在
#   app/keywords.py：AI 报告和词云必须是同一份分词，否则两边结果对不上且不报错。


@router.get("/keywords", response_model=KeywordsOut)
def keywords(
    db: DbDep,
    source: SourceDep,
    limit: Annotated[int, Query(ge=10, le=300)] = 120,
) -> KeywordsOut:
    """词云数据。

    ★ 分词在后端做，因为 jieba 是 Python 的：让浏览器干要么引几百 KB 的 JS 分词
      库（效果还差），要么把整份词典发给前端，两头都不划算。
    ★ 实测 1000 个标题约 **46 ms**（所有接口里最慢的一个），O(标题数)；到 1 万条
      时该做 (source, 商品数) 的短 TTL 缓存或预聚合。
    """
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

    # ★ 分词、排序、大小写还原全部在 app/keywords.py 一处定义；
    #   返回值是 [(显示形式, 归一化键, 次数)]，排序的确定性在那边保证。
    ranked = rank_keywords(titles, limit=limit)

    items = [KeywordItem(name=name, value=count) for name, _key, count in ranked]

    return KeywordsOut(
        source=source.value,
        total_titles=len(titles),
        items=items,
        notice=None if items else "分词后没有剩下任何关键词（标题可能全被停用词覆盖）。",
    )
