"""商品接口：分页列表 / 详情 / 价格历史。

    GET /api/products                     分页列表（支持排序、搜索、数据源过滤）
    GET /api/products/{id}                详情
    GET /api/products/{id}/price-history  价格时间序列（折线图数据）

本文件里三个"看起来啰嗦但必须写"的地方：
1. **排序字段走白名单映射，绝不拼字符串。** `?sort=` 是用户可控的，而且即使
   参数化了值，**列名也是没法参数化的**——正确做法只能是"用户给的字符串 →
   查表 → 得到真正的列对象"。
2. **每一种排序都必须带兜底列。** 理由写在 `list_products()` 里，它会造成翻页丢数据。
3. **价格历史要告诉前端 x 轴到底是什么时间。** `recorded_at` 的含义随
   source_channel 变化（爆料发布时间 / 采集时间 / 模拟时间），混着画是**不诚实**的。
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, nullslast, select

from app.api.deps import DbDep, SourceDep
from app.db.models import PriceHistory, Product, Shop
from app.source_scope import apply_source
from app.textutil import elide_middle

router = APIRouter(prefix="/api/products", tags=["products"])


class ProductSort(str, Enum):
    """可排序的字段。

    ★ 用 Enum 而不是裸字符串：FastAPI 自动校验（`?sort=drop_table` 直接 422），
      而白名单写在类型里，加字段必须改这里——不会漏在某个 if/elif 链的深处。
    """

    last_crawled_at = "last_crawled_at"
    price = "price"
    sales = "sales"
    created_at = "created_at"
    id = "id"


class SortOrder(str, Enum):
    asc = "asc"
    desc = "desc"


# 枚举成员 → 真正的 ORM 列。★ 这张表就是白名单本身。
_SORT_COLUMNS = {
    ProductSort.last_crawled_at: Product.last_crawled_at,
    ProductSort.price: Product.price,
    ProductSort.sales: Product.sales,
    ProductSort.created_at: Product.created_at,
    ProductSort.id: Product.id,
}


class ProductListItem(BaseModel):
    id: int
    source: str
    source_id: str
    source_channel: str
    title: str
    title_short: str
    brand: str | None
    platform: str | None
    category: str | None
    price: float | None
    price_max: float | None
    sales: int | None
    shop: str | None
    image_url: str | None
    url: str | None
    last_crawled_at: str
    # ★ 价格历史的数据点数：Mock 商品 90 个，真实采集的可能只有 1 个——并排可见，
    #   这也正是"事件归并"要解决的问题。
    price_point_count: int


class ProductPage(BaseModel):
    source: str
    page: int
    size: int
    total: int
    pages: int
    items: list[ProductListItem]


class ProductDetail(ProductListItem):
    title_norm: str
    created_at: str
    # 疑似重复指向谁（灰色地带的人工核对线索）
    suspected_duplicate_of: int | None
    # 价格历史的时间范围
    first_recorded_at: str | None
    last_recorded_at: str | None
    # 历史最低/最高价 —— ★ 刻意从 price_history 现算，而不是读 products.price_max：
    #   后者是"本次成交的价格区间上限"，和历史最高价是两回事（见 models.py）。
    historical_min: float | None
    historical_max: float | None


class PricePointOut(BaseModel):
    recorded_at: str
    price: float
    source_ref: str | None


class PriceHistoryOut(BaseModel):
    product_id: int
    title: str
    source: str
    source_channel: str
    total: int
    points: list[PricePointOut]
    first_recorded_at: str | None
    last_recorded_at: str | None
    min_price: float | None
    max_price: float | None

    # ★ 这两个字段是给前端"如实标注"用的。
    x_axis_label: str = Field(
        description="x 轴到底画的是什么时间。语义随 source_channel 变化，必须显示给用户。"
    )
    notice: str | None = Field(
        default=None,
        description=(
            "没有价格点、只有一个点（无法构成趋势）、或点数超过 limit 被截断时，"
            "这里给出解释。★ 前两种前端要知道，第三种更要——"
            "「图上没有这一段」和「数据里没有这一段」是完全不同的两件事。"
        ),
    )


# ---- 时间轴语义 ---------------------------------------------------------------
# ★ 这张表必须和 models.py 里 PriceHistory.recorded_at 的注释一致：不一致时接口
#   文档说"爆料时间"、图上标"采集时间"，而实际存的是采集时间。
_X_AXIS_LABELS = {
    "jingxuan": "爆料发布时间",
    "fenlei": "采集时间（类目页没有发布时间）",
    "mock": "模拟时间",
}


def _x_axis_label(source_channel: str) -> str:
    return _X_AXIS_LABELS.get(source_channel, "记录时间")


def _escape_like(term: str) -> str:
    """转义 LIKE 的通配符。

    ★ 必须做：用户搜"50% 折扣"很自然，而不转义时 `%` 被当成"匹配任意字符"，
      输入一个字符就返回**全表**——不报错，只是结果莫名其妙地多。
    ★ 反斜杠必须**第一个**替换：反过来会把先转好的 `\\%` 里的反斜杠再转一次。
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _point_count_subquery():
    """相关子查询：每个商品有多少个价格点。

    ★ 用相关子查询而不是 JOIN + GROUP BY：后者要为所有价格点产出行再聚合。
      `.correlate(Product)` 是关键——不写它，子查询会再 FROM 一次 products 表。
    """
    return (
        select(func.count(PriceHistory.id))
        .where(PriceHistory.product_id == Product.id)
        .correlate(Product)
        .scalar_subquery()
        .label("price_point_count")
    )


def _to_item(product: Product, shop_name: str | None, point_count: int) -> ProductListItem:
    return ProductListItem(
        id=int(product.id),
        source=product.source,
        source_id=product.source_id,
        source_channel=product.source_channel,
        title=product.title,
        # 表格里的标题列由 CSS 的 text-overflow 负责（能 hover 看全文），这里给的
        # 是"没有 CSS 时的退化长度"，预算比图表大得多。
        title_short=elide_middle(product.title, budget=60),
        brand=product.brand,
        platform=product.platform,
        category=product.category,
        price=round(float(product.price), 2) if product.price is not None else None,
        price_max=round(float(product.price_max), 2) if product.price_max is not None else None,
        sales=int(product.sales) if product.sales is not None else None,
        shop=shop_name,
        image_url=product.image_url,
        url=product.url,
        last_crawled_at=product.last_crawled_at.isoformat(sep=" "),
        price_point_count=int(point_count or 0),
    )


@router.get("", response_model=ProductPage)
def list_products(
    db: DbDep,
    source: SourceDep,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
    sort: ProductSort = ProductSort.last_crawled_at,
    order: SortOrder = SortOrder.desc,
    q: Annotated[str | None, Query(max_length=100)] = None,
    category: Annotated[str | None, Query(max_length=32)] = None,
) -> ProductPage:
    """商品分页列表。"""
    column = _SORT_COLUMNS[sort]

    # ★★ 兜底排序列 `Product.id` 是最容易被删、后果最严重的一行：没有它时排序是
    #    **部分有序**的（同值行顺序没有任何保证），而翻页是两次独立查询——顺序一
    #    变，就会有商品在第 1、2 页各出现一次、另一个一次都不出现。（Mock 同价
    #    很多，翻第二页就能看到，不是理论风险。）
    primary = column.asc() if order is SortOrder.asc else column.desc()
    order_by = [nullslast(primary), Product.id.asc()]

    stmt = (
        apply_source(select(Product, Shop.name.label("shop_name"), _point_count_subquery()), source)
        # LEFT JOIN：没有店铺信息的商品也要出现在列表里。
        .join(Shop, Product.shop_id == Shop.id, isouter=True)
        .order_by(*order_by)
        .limit(size)
        .offset((page - 1) * size)
    )

    if q:
        # ★ 只搜 title 不搜 title_norm：用户输入的是他看到的文字，拿它去匹配去噪
        #   后的内部表示会出现"明明看见却搜不到"。代价是走不了索引，但商品表千级，
        #   全表扫描无所谓。
        stmt = stmt.where(Product.title.like(f"%{_escape_like(q)}%", escape="\\"))
    if category:
        stmt = stmt.where(Product.category == category)

    rows = db.execute(stmt).all()

    # 计数必须和上面的 where 条件完全一致，否则会出现"总共 3 条，但翻到第 2 页
    # 是空的"这种诡异现象——所以用同样的过滤条件再拼一次 select。
    count_stmt = apply_source(select(func.count(Product.id)), source)
    if q:
        count_stmt = count_stmt.where(Product.title.like(f"%{_escape_like(q)}%", escape="\\"))
    if category:
        count_stmt = count_stmt.where(Product.category == category)
    total = int(db.execute(count_stmt).scalar_one())

    return ProductPage(
        source=source.value,
        page=page,
        size=size,
        total=total,
        pages=max(1, math.ceil(total / size)),
        items=[_to_item(p, shop_name, cnt) for p, shop_name, cnt in rows],
    )


@router.get("/{product_id}", response_model=ProductDetail)
def get_product(db: DbDep, product_id: int) -> ProductDetail:
    """商品详情。

    ★ 这里**不加** `?source=` 过滤：id 已全局唯一，再叠过滤只会让"从列表点进来"
      因为没带 source 而 404（列表/统计接口则必须过滤——那是在"看一批数据"）。
    """
    row = db.execute(
        select(Product, Shop.name.label("shop_name"), _point_count_subquery())
        .join(Shop, Product.shop_id == Shop.id, isouter=True)
        .where(Product.id == product_id)
    ).one_or_none()

    if row is None:
        raise HTTPException(status_code=404, detail=f"商品 {product_id} 不存在")

    product, shop_name, point_count = row

    agg = db.execute(
        select(
            func.min(PriceHistory.price),
            func.max(PriceHistory.price),
            func.min(PriceHistory.recorded_at),
            func.max(PriceHistory.recorded_at),
        ).where(PriceHistory.product_id == product_id)
    ).one()
    hist_min, hist_max, first_at, last_at = agg

    base = _to_item(product, shop_name, point_count)
    return ProductDetail(
        **base.model_dump(),
        title_norm=product.title_norm,
        created_at=product.created_at.isoformat(sep=" "),
        suspected_duplicate_of=product.suspected_duplicate_of,
        first_recorded_at=first_at.isoformat(sep=" ") if first_at else None,
        last_recorded_at=last_at.isoformat(sep=" ") if last_at else None,
        historical_min=round(float(hist_min), 2) if hist_min is not None else None,
        historical_max=round(float(hist_max), 2) if hist_max is not None else None,
    )


@router.get("/{product_id}/price-history", response_model=PriceHistoryOut)
def price_history(
    db: DbDep,
    product_id: int,
    limit: Annotated[int, Query(ge=1, le=2000)] = 400,
) -> PriceHistoryOut:
    """价格时间序列。

    ★ `limit` 默认 400 且有上限：Mock 每商品 90 个点，真实采集可以无限增长，而折线
      图画不了几千个点。
    ★ 截断时取**最新** N 个点，而 total / min / max / 首末时间都算在**全量**上。
    """
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail=f"商品 {product_id} 不存在")

    # ---- 全量口径：总数与极值算在整张表上，不是算在下面那个窗口上 ----
    # ★ 不能在截断后的列表上现算：`/{id}` 详情走 SQL 的 MIN/MAX（全量），而前端是
    #   Promise.all 同时取两个接口、渲染在**同一屏**上——算在窗口上就会出现
    #   "KPI 卡说历史最高 699，折线图最高标记停在 499"，两个接口各自都没写错。
    total = db.execute(
        select(func.count(PriceHistory.id)).where(PriceHistory.product_id == product_id)
    ).scalar_one()

    hist_min, hist_max, first_at, last_at = db.execute(
        select(
            func.min(PriceHistory.price),
            func.max(PriceHistory.price),
            func.min(PriceHistory.recorded_at),
            func.max(PriceHistory.recorded_at),
        ).where(PriceHistory.product_id == product_id)
    ).one()

    # ---- 取【最新】的 limit 个点 ----
    # ★ 排序必须 desc：写成 asc 配 limit 取到的是**最旧**的 N 个，把最新点全部丢掉，
    #   而丢掉近期数据的趋势图恰恰丢掉了它唯一想显示的东西——且静默发生。
    # ★ 第二排序键 id：recorded_at 只精确到秒，同一秒两条记录的顺序不能靠运气。
    points = db.execute(
        select(PriceHistory)
        .where(PriceHistory.product_id == product_id)
        .order_by(PriceHistory.recorded_at.desc(), PriceHistory.id.desc())
        .limit(limit)
    ).scalars().all()
    points.reverse()  # 翻回升序：折线图要从左到右按时间画

    # ★ 真实采集来的商品绝大多数只有一个价格点，折线图就是**一个孤零零的点**，
    #   用户会以为图画坏了——"事件归并"要解决的就是这件事：反复爆料会让它长出
    #   第二、第三个点。
    notice = None
    if total == 0:
        notice = "该商品没有任何价格记录。"
    elif total > len(points):
        # ★ 截断了就要说：不说的话图上少一截而没有任何人知道——
        #   而"图上没有这段"和"数据里没有这段"是完全不同的两件事。
        notice = (
            f"该商品共 {total} 个价格点，图上画的是最近 {len(points)} 个；"
            f"「历史最低 / 最高」两个数字是整个区间的。"
        )
    elif len(points) == 1:
        notice = "该商品目前只有一个价格记录点，无法构成趋势。真实采集中这很常见：只有一个爆料提到过它。"

    return PriceHistoryOut(
        product_id=int(product.id),
        title=product.title,
        source=product.source,
        source_channel=product.source_channel,
        total=int(total),
        points=[
            PricePointOut(
                recorded_at=p.recorded_at.isoformat(sep=" "),
                price=round(float(p.price), 2),
                source_ref=p.source_ref,
            )
            for p in points
        ],
        first_recorded_at=first_at.isoformat(sep=" ") if first_at else None,
        last_recorded_at=last_at.isoformat(sep=" ") if last_at else None,
        min_price=round(float(hist_min), 2) if hist_min is not None else None,
        max_price=round(float(hist_max), 2) if hist_max is not None else None,
        x_axis_label=_x_axis_label(product.source_channel),
        notice=notice,
    )
