"""数据写入层 —— 两张表**相反**的 upsert 语义都在这里，便于对照。

    products        ON CONFLICT DO UPDATE   刷新价格等字段
    price_history   ON CONFLICT DO NOTHING  同一篇文章 = 同一个价格事件
★ 两个都写 DO NOTHING 是最容易犯的错，后果静默：price_history 正确跳过，
但 products.last_crawled_at 永远停在第一次采集，"连续 N 天抓不到就判下架"
这类功能直接失效且不报错。

价格用 source_ref 而不是 recorded_at 做唯一键：后者是时间属性、不该承担
身份职责（同秒的两篇爆料会被静默丢掉）。见 app/db/models.py 与
tests/test_price_history.py 里守着这条的三个测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import PriceHistory, Product, Shop


@dataclass(frozen=True, slots=True)
class ProductRow:
    """一个待写入的商品（数据源解析后的统一中间表示）。

    ★ 所有来源都先转成这个结构再入库：解析逻辑与入库逻辑解耦，
      加新数据源不用重写入库代码。
    """

    source: str                 # 数据血缘：mock / smzdm
    source_id: str              # 源站 id
    title: str
    source_channel: str = "jingxuan"
    title_norm: str = ""
    brand: str | None = None
    platform: str | None = None
    category: str | None = None
    url: str | None = None
    image_url: str | None = None
    price: float | None = None
    price_max: float | None = None
    sales: int | None = None
    shop_id: int | None = None
    last_crawled_at: datetime | None = None
    # 灰色地带标记：疑似是某个已有商品的重复（归并判定给的是 MARK）。
    # 只在插入时写入，冲突更新时不刷新——见 _UPSERT_UPDATE_COLS 注释。
    suspected_duplicate_of: int | None = None


@dataclass(frozen=True, slots=True)
class PricePoint:
    """一个待写入的价格点。"""

    product_id: int
    price: float
    recorded_at: datetime
    # 源站记录 id。Mock 恒为 None：SQL 把多个 NULL 视为互不相同，
    # 所以 Mock 的 90 天序列不会被 (product_id, source_ref) 唯一键挡住。
    source_ref: str | None = None


# --- products：冲突时更新 ---

# ★ 冲突时要刷新的列。不更新 title / brand / title_norm——它们是归并判断的
# 输入，悄悄改掉会让已做的归并决定失去依据；不更新 suspected_duplicate_of——
# 那是给人核对的待办标记，写回去会让清理过的待办复活。last_crawled_at 必须每次刷。
_UPSERT_UPDATE_COLS = ("price", "price_max", "image_url", "url", "last_crawled_at")


def upsert_product(db: Session, row: ProductRow) -> int:
    """写入或更新一个商品，返回它的 id。

    ★ 冲突动作是 DO UPDATE（不是 DO NOTHING），否则 last_crawled_at 永远停住。
    """
    values = {
        "source": row.source,
        "source_id": row.source_id,
        "source_channel": row.source_channel,
        "title": row.title,
        "title_norm": row.title_norm or row.title,
        "brand": row.brand,
        "platform": row.platform,
        "category": row.category,
        "url": row.url,
        "image_url": row.image_url,
        "price": row.price,
        "price_max": row.price_max,
        "sales": row.sales,
        "shop_id": row.shop_id,
        # ★ 兜底成 utcnow()：列定义里的 Python 端 default 永远不会触发
        #   （这里总是显式传值），留空只会得到 NOT NULL constraint failed。
        #   兜底后语义是诚实的：不填 = "就是现在采集的"。
        "last_crawled_at": row.last_crawled_at or utcnow(),
        "suspected_duplicate_of": row.suspected_duplicate_of,
    }

    stmt = (
        sqlite_insert(Product)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["source", "source_id"],
            # ★ set_ 的列与 values 里的列分开写：全塞进去会连 source/source_id
            # 一起"更新"成同样的值，显式列出要更新的列更清楚。
            set_={c: values[c] for c in _UPSERT_UPDATE_COLS},
        )
        # ★ RETURNING：一次往返同时拿到"插入的"和"更新的"那行的 id，
        # 让 upsert 两种分支的行为无歧义（SQLite 3.35+ 支持）。
        .returning(Product.id)
    )
    product_id = db.execute(stmt).scalar_one()
    return int(product_id)


def upsert_products(db: Session, rows: list[ProductRow]) -> dict[tuple[str, str], int]:
    """批量 upsert，返回 (source, source_id) → id 的映射。

    ★ 逐个执行而不是 executemany：需要每一行的 id，而这个量级往返不是瓶颈。
    """
    out: dict[tuple[str, str], int] = {}
    for row in rows:
        out[(row.source, row.source_id)] = upsert_product(db, row)
    return out


def find_product_id(db: Session, source: str, source_id: str) -> int | None:
    """按业务主键查商品 id，查不到返回 None。"""
    return db.execute(
        select(Product.id).where(Product.source == source, Product.source_id == source_id)
    ).scalar_one_or_none()


# --- shops：有名字就 resolve-or-create ---


def ensure_shop(db: Session, platform: str, name: str) -> int:
    """按 (平台, 店名) 找店铺，没有就建一个。返回 shop_id。

    ★ platform 必填（尽管模型上可空）：SQL 把多个 NULL 视为互不相同，
      (NULL, "京东自营") 能重复插入、唯一约束形同虚设。店铺量级很小，
      查一遍再建就够，不必上 upsert。
    """
    if not platform:
        raise ValueError("ensure_shop 需要 platform：唯一约束在 platform 为 NULL 时失效")

    existing = db.execute(
        select(Shop.id).where(Shop.platform == platform, Shop.name == name)
    ).scalar_one_or_none()
    if existing is not None:
        return int(existing)

    shop = Shop(name=name, platform=platform)
    db.add(shop)
    # flush 而不是 commit：要的是自增 id，提交时机由调用方决定。
    db.flush()
    return int(shop.id)


# --- price_history：冲突时跳过 ---


def record_price(
    db: Session,
    product_id: int,
    price: float,
    recorded_at: datetime,
    source_ref: str | None = None,
) -> bool:
    """记录一个价格点。重复则跳过。

    返回 True 表示真的新增了一行。返回它是因为采集报告的数字必须来自
    数据库的真实反馈——应用层"先查再判断"在并发采集时有竞态。
    """
    stmt = (
        sqlite_insert(PriceHistory)
        .values(
            product_id=product_id,
            price=price,
            recorded_at=recorded_at,
            source_ref=source_ref,
        )
        # ★ 冲突动作是 DO NOTHING（与 products 相反）。
        #   同一篇爆料 = 同一个价格事件，重复采集不该产生第二条记录。
        .on_conflict_do_nothing(index_elements=["product_id", "source_ref"])
        # ★ returns：冲突时不会 RETURNING 任何行，所以"有没有拿到 id"即
        # "有没有真的插入"。比 rowcount 可靠（各方言处理不一致）。
        .returning(PriceHistory.id)
    )
    return db.execute(stmt).scalar_one_or_none() is not None


def record_prices(db: Session, points: list[PricePoint]) -> int:
    """批量记录价格点，返回**实际新增**的行数（传 30 条、28 条已存在则返回 2）。

    ★ 走 executemany 而不是循环 record_price：实测 9 万行 0.8 秒 vs 27 秒
      （45 倍）——seed 每次要写 9 万行，这个差距决定调参循环的快慢。
    ★ 用 RETURNING 数行数而不是 rowcount：executemany 下的 IteratorResult
      没有 rowcount，而 RETURNING 的行数天然等于真正插入的行数。
    """
    if not points:
        return 0

    stmt = (
        sqlite_insert(PriceHistory)
        .on_conflict_do_nothing(index_elements=["product_id", "source_ref"])
        .returning(PriceHistory.id)
    )
    rows = [
        {
            "product_id": p.product_id,
            "price": p.price,
            "recorded_at": p.recorded_at,
            "source_ref": p.source_ref,
        }
        for p in points
    ]
    # 不在这里 commit：让调用方把相关的写操作放在一个事务里，失败一起回滚。
    return len(db.execute(stmt, rows).all())
