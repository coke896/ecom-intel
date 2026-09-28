"""数据写入层 —— 两张表【相反】的 upsert 语义都在这里。

================================================================================
为什么要把写入单独抽一个模块
================================================================================
"插入商品"和"插入价格"看起来都是 `db.execute(insert(...))`，
随手写两行就完事。但这两张表的冲突语义**恰好相反**，而且写错了都不会报错：

    products        ON CONFLICT DO UPDATE   刷新 price / price_max / last_crawled_at
    price_history   ON CONFLICT DO NOTHING  同一篇文章 = 同一个价格事件

★ 这是最容易"图省事两个都写 DO NOTHING"的地方，而后果是**静默的**：

  两个都 DONOTHING 时，同一篇爆料第二天再被采集到时：
    · price_history 不重复记录 —— 正确，这是我们想要的
    · products 的 last_crawled_at 永远停在第一次采集 —— ← 错
  于是"连续 N 天抓不到就判定下架"这类功能直接失效，而且没有任何报错。
  等到有人问"这个商品明明还在卖，为什么被标成下架了"才会发现。

把两者放在同一个文件里紧挨着，是为了让这个对比**在 review 时一眼可见**——
分散在两个 source 实现里的话，没人会把它们放在一起看。

================================================================================
价格为什么用 source_ref 而不是 recorded_at 做唯一键
================================================================================
见 app/db/models.py 里 PriceHistory 的注释。这里只重复结论：

  recorded_at 是【时间属性】，不该承担"身份"职责。
  同一商品被两篇爆料在同一秒提及（SMZDM 的 timesort 精度到秒）时，
  用 (product_id, recorded_at) 做唯一键会**静默丢掉一条真实数据**。
  source_ref（爆料文章 id）才是真正的身份。

tests/test_price_history.py 里有三个测试专门守着这条。
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

    ★ 所有来源（mock / smzdm / 将来别的站）都先转成这个结构再入库。
      这样"两个源的解析逻辑"和"入库逻辑"是解耦的：
      改去重策略不用动任何一个解析器，加新数据源也不用重写入库代码。
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
    # 灰色地带标记：这条疑似是某个已有商品的重复（归并判定给的是 MARK）。
    # ★ 只在【插入】时写入，不在冲突更新时刷新——见 _UPSERT_UPDATE_COLS 注释。
    suspected_duplicate_of: int | None = None


@dataclass(frozen=True, slots=True)
class PricePoint:
    """一个待写入的价格点。"""

    product_id: int
    price: float
    recorded_at: datetime
    # ★ 源站记录 id。Mock 数据恒为 None。
    #   SQLite 的 UNIQUE 约束把多个 NULL 视为互不相同（SQL 标准行为，
    #   因为 NULL != NULL），所以 Mock 的 90 天序列不会被唯一键挡住。
    source_ref: str | None = None


# ---------------------------------------------------------------------------
# products：冲突时【更新】
# ---------------------------------------------------------------------------

# ★ 冲突时要刷新的列。
#
#   为什么不更新 title / brand / title_norm：
#     这些是"归并判断的输入"。同一篇爆料第二次采集时它们不会变；
#     而如果变了（源站改了标题），悄悄改掉会让已经做的归并决定失去依据。
#     真出现这种情况应该走人工确认，不是自动覆盖。
#
#   为什么不更新 suspected_duplicate_of：
#     这个字段是给【人工】用的待办标记。人去核对之后会把它清成 NULL
#     （"我看过了，不是重复"），而重新采集时如果又把它写回去，
#     人的工作就被自动覆盖了——清理过的待办会一次次复活。
#     ★ 所以它是"只在插入时写一次"的字段。
#
#   last_crawled_at 是这里最重要的一个：
#     它承担"这个商品还活着吗"的判断，必须每次采集都刷新。
_UPSERT_UPDATE_COLS = ("price", "price_max", "image_url", "url", "last_crawled_at")


def upsert_product(db: Session, row: ProductRow) -> int:
    """写入或更新一个商品，返回它的 id。

    ★ 冲突动作是 DO UPDATE（不是 DO NOTHING）。
      理由见模块顶部：DO NOTHING 会让 last_crawled_at 永远停在第一次采集。
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
        # ★ 这一列的模型定义是 `nullable=False, default=utcnow`，
        #   但**那个 Python 端默认值永远不会触发**——因为它只在列
        #   不出现在 INSERT 里时才生效，而这里总是显式传进去。
        #   于是 ProductRow 的 last_crawled_at 留空（默认 None）时，
        #   报错是一句 NOT NULL constraint failed: products.last_crawled_at，
        #   而不是任何跟"你没填 last_crawled_at"有关的话。
        #
        #   在这里兜底成 utcnow()，让 dataclass 上 `= None` 这个默认值
        #   在语义上是诚实的：不填 = "就是现在采集的"。
        "last_crawled_at": row.last_crawled_at or utcnow(),
        "suspected_duplicate_of": row.suspected_duplicate_of,
    }

    stmt = (
        sqlite_insert(Product)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["source", "source_id"],
            # ★ set_ 里的列必须与 values 里的列【分开写】：
            #   直接把 values 全塞进 set_ 的话，source/source_id 也会被"更新"
            #   成同样的值——看着无害，但会让 SQLite 把这一行当作真正的更新，
            #   在某些触发器/审计场景下产生噪声。显式列出要更新的列更清楚。
            set_={c: values[c] for c in _UPSERT_UPDATE_COLS},
        )
        # ★ RETURNING：一次往返同时拿到"插入的"和"更新的"那行的 id。
        #   不用它就得再发一条 SELECT，而 upsert 的两种分支各自返回什么
        #   在 SQLAlchemy 里并不直观——RETURNING 让行为无歧义。
        #   （SQLite 3.35+ 支持，本环境 3.50.4）
        .returning(Product.id)
    )
    product_id = db.execute(stmt).scalar_one()
    return int(product_id)


def upsert_products(db: Session, rows: list[ProductRow]) -> dict[tuple[str, str], int]:
    """批量 upsert，返回 (source, source_id) → id 的映射。

    ★ 逐个执行而不是 executemany：因为需要每一行的 id。
      单页 20 条的量级下，这点往返完全不是瓶颈，
      而 executemany + RETURNING 在 SQLAlchemy 的 SQLite 方言里支持得很别扭。
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


# ---------------------------------------------------------------------------
# shops：有名字就 resolve-or-create
# ---------------------------------------------------------------------------


def ensure_shop(db: Session, platform: str, name: str) -> int:
    """按 (平台, 店名) 找店铺，没有就建一个。返回 shop_id。

    ★ 为什么要"查一遍再建"而不是无条件 insert：
      shops 有 (platform, name) 唯一约束，无条件插入第二次就撞约束。
      改用 `INSERT ... ON CONFLICT DO NOTHING` + 回查也能做，
      但这里读一次就够（店铺总量是几十个量级），没必要上 upsert。

    ★ platform 是必填的，尽管模型上这一列可空。
      因为 SQLite（以及 SQL 标准）把多个 NULL 视为互不相同——
      `(NULL, "京东自营")` 能重复插进去好几行，
      唯一约束在这个组合上**形同虚设**。要求调用方给平台，
      这个坑就不会被踩到。
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
    # ★ flush 而不是 commit：这里要的是自增 id，不是提交事务。
    #   提交时机由调用方决定（采集任务整批一个事务）。
    db.flush()
    return int(shop.id)


# ---------------------------------------------------------------------------
# price_history：冲突时【跳过】
# ---------------------------------------------------------------------------


def record_price(
    db: Session,
    product_id: int,
    price: float,
    recorded_at: datetime,
    source_ref: str | None = None,
) -> bool:
    """记录一个价格点。重复则跳过。

    返回 **True 表示真的新增了一行**，False 表示命中了冲突被跳过。
    ★ 为什么要返回这个：采集任务要汇报"本轮新增 3 个价格点"——
      而这个数字必须来自数据库的真实反馈，
      不能在应用层"先查再判断"，那在并发采集时有竞态。
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
        # ★ returns 的语义：冲突时不会 RETURNING 任何行，
        #   所以"有没有拿到 id"就是"有没有真的插入"。比 rowcount 可靠
        #   （SQLAlchemy 对 DO NOTHING 的 rowcount 处理在不同方言下不一致）。
        .returning(PriceHistory.id)
    )
    return db.execute(stmt).scalar_one_or_none() is not None


def record_prices(db: Session, points: list[PricePoint]) -> int:
    """批量记录价格点，返回**实际新增**的行数。

    ★ 返回的是新增数，不是传入数：传 30 条、其中 28 条已存在时返回 2。
      调用方拿它判断"这一轮采集有没有带来新信息"。

    ★ 为什么走 executemany 而不是循环调用 record_price：

        逐行 record_price   → 9 万行 27.0 秒
        本函数的批路径      → 9 万行  0.8 秒     （实测，45 倍）

      差距来自"每行一次 Python↔SQLite 往返 + 一次 RETURNING 解析"。
      seed 一次要写 1000 商品 × 90 天 = 9 万行，27 秒会让
      "改个参数重新灌一次看看图表"从几秒变成半分钟——
      调参阶段这个循环要重复几十次，累计就是几十分钟。

    ★ 为什么用 RETURNING 数行数，而不是 result.rowcount：
      SQLAlchemy 对 executemany 返回的是 IteratorResult（没有 rowcount），
      因为 insertmanyvalues 会把批次拆成多条语句执行。
      RETURNING 的行数**天然等于真正插入的行数**——
      ON CONFLICT DO NOTHING 跳过的行不会出现在返回值里。
      实测：全新 9 万条返回 90000，同一批重放返回 0。
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
    # ★ 不在这里 commit：调用方可能还要写别的表，
    #   让它们在一个事务里提交，失败时一起回滚。
    #   这也是 record_price（单行版）不 commit 的原因。
    return len(db.execute(stmt, rows).all())
