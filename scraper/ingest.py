"""入库管道 —— 把一批原始爆料变成数据库里的商品和价格点。

Mock 与 SMZDM 走的是同一条管道（差别只在怎么拿到 RawDeal），
所以本文件里没有一个字提到数据源的名字。

每条爆料先回答"它已经有主了吗"（resolve_target）：有主 → 只刷新不新建；
无主 → 进归并判定 MERGE / MARK / NEW。"有主"有两种形态：① 它自己就是商品
（products.source_id）；② 它被归并进了别的商品（price_history.source_ref，
在 products 表里查不到，漏掉它第二轮会重复报"归并"而实际什么都没发生）。

三个不写清楚就出错的细节：MERGE / 刷新分支的 UPDATE 不能省（列表页显示的是
products 的价格）；价格更新必须按时间新旧定序（基准跨批次，取内存与库里更晚
的那个），否则更旧的爆料会把价格改旧；没有价格或时间戳的爆料照样建商品、
但不写价格点（那两列 NOT NULL，实测 100 条里 2 条没有价格）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import PriceHistory, Product
from scraper import dedup
from scraper.base import CrawlReport, RawDeal
from scraper.cleaner import clean_text, normalize_width, parse_price
from scraper.dedup import Candidate, Decision, ExistingProduct
from scraper.store import (
    PricePoint,
    ProductRow,
    ensure_shop,
    record_prices,
    upsert_products,
)


# --- 读已有商品 ---


@dataclass(slots=True)
class ExistingIndex:
    """某个数据源已入库的东西，三种视角一次读齐。

    ★ 一次读完而不是各读各的：它们描述同一批数据，分开读会有时间差。
    ★ 这些字典在 ingest_deals 循环里被持续更新，同一批里后面的爆料能看到
      前面刚建的商品——否则同批的两条同款爆料会建成两个商品。
    """

    # 归并比对用的池子（带 token，能算相似度）
    pool: list[ExistingProduct] = field(default_factory=list)
    # 形态 ①：这条爆料自己就是一个商品  source_id → product_id
    by_source_id: dict[str, int] = field(default_factory=dict)
    # 形态 ②：这条爆料被归并进了别的商品  source_ref → product_id
    by_ref: dict[str, int] = field(default_factory=dict)

    def resolve_target(self, source_id: str) -> int | None:
        """这条爆料在库里已经有主了吗？有就返回主的 product_id。

        ★ 先查 by_source_id 再查 by_ref：自己就是商品时两个字典给出同一答案，
          先查自己更直观。
        """
        own = self.by_source_id.get(source_id)
        if own is not None:
            return own
        return self.by_ref.get(source_id)


def load_existing(db: Session, source: str) -> ExistingIndex:
    """把某个数据源已入库的商品和价格记录读成归并比对需要的形状。

    ★ 一次性读完（2 次查询），不是每条爆料查一次库。
    ★ token 从 title_norm 现算、不从数据库读：它是 normalize_title 的推导
      结果，存一份就等于多一处会跟算法版本脱节的数据（且错误完全静默）。
    """
    idx = ExistingIndex()

    rows = db.execute(
        select(Product.id, Product.source_id, Product.title_norm, Product.brand).where(
            Product.source == source
        )
    ).all()
    for pid, source_id, title_norm, brand in rows:
        norm = title_norm or ""
        pid = int(pid)
        idx.pool.append(
            ExistingProduct(
                product_id=pid,
                title_norm=norm,
                # ★ 从库里的值现算规范形态：products.brand 存的是显示形态
                # （"Soundcore/声阔"），比对要的是规范形态（"soundcore|声阔"）。
                # extract_brand 幂等，所以库里存哪种形态都能得到正确结果。
                brand=dedup.extract_brand(brand),
                model_tokens=dedup.extract_model_tokens(norm),
                tokens=frozenset(dedup.tokenize(norm)),
                source_id=source_id,
            )
        )
        idx.by_source_id[source_id] = pid

    # ★ 形态 ② 的来源：价格记录上的 source_ref。关联 products 限定在本源内——
    # 不同源的 source_ref 可能撞（都是数字 id）。
    ref_rows = db.execute(
        select(PriceHistory.source_ref, PriceHistory.product_id)
        .join(Product, Product.id == PriceHistory.product_id)
        .where(Product.source == source, PriceHistory.source_ref.is_not(None))
    ).all()
    for source_ref, pid in ref_rows:
        idx.by_ref[source_ref] = int(pid)

    return idx


# --- 清洗 ---


@dataclass(frozen=True, slots=True)
class CleanedDeal:
    """洗完的爆料 —— 可以直接变成 ProductRow 了。"""

    deal: RawDeal
    title: str
    title_norm: str
    brand: str | None
    price: float | None
    price_max: float | None
    recorded_at: datetime | None


def clean_deal(deal: RawDeal) -> CleanedDeal:
    """清洗一条爆料。

    ★ 依赖 cleaner 的核心承诺"不抛异常"：价格洗不出来时是 None，不是异常。
    """
    # 全角转半角必须在 parse_price 之前：全角 `１９９` 和半角 `199` 是不同字符。
    title = normalize_width(clean_text(deal.title) or "")
    price, price_max = parse_price(deal.price_raw)

    return CleanedDeal(
        deal=deal,
        title=title,
        title_norm=dedup.normalize_title(title),
        # ★ 品牌取显示形态（源站原文，给人看），和 seed.py 写入的同一形状；
        # 比对用的规范形态由 load_existing 现算。品牌只来自源站字段、不从标题猜
        # （标题里的"适配华为"这类猜错会同时污染硬门槛和品牌分析）。
        brand=dedup.display_brand(deal.brand),
        price=price,
        price_max=price_max,
        recorded_at=deal.recorded_at,
    )


# --- 主入口 ---


def ingest_deals(
    db: Session,
    deals: list[RawDeal],
    *,
    source: str | None = None,
) -> CrawlReport:
    """把一批爆料清洗、找主、归并、写入。**调用方负责 commit。**

    ★ 不在这里 commit：让一批爆料成为一个事务，失败整批回滚。
    ★ 本函数是**幂等**的：商品走 upsert、价格点走 ON CONFLICT DO NOTHING，
      第二轮走"刷新"分支——"连抓两轮不报 IntegrityError"的验收点靠它。
    """
    if not deals:
        return CrawlReport()

    src = source or deals[0].source

    # 已刷新的商品：target_id → 我们见过的**最新** record time。
    # 用来实现【二】：不用更早的爆料覆盖更新的价格。
    newest_seen: dict[int, datetime] = {}

    idx = load_existing(db, src)

    # 用局部计数器最后一次性构造 CrawlReport（frozen，原地累加会抛异常）。
    new = merged = marked = refreshed = 0

    pending_points: list[PricePoint] = []
    # (平台, 店名) → shop_id 的会话内缓存。见 _resolve_shop_id。
    shop_cache: dict[tuple[str, str], int] = {}

    for deal in deals:
        cleaned = clean_deal(deal)
        if not cleaned.title:
            # 没有标题没法参与归并比对，直接跳过（解析层已过滤一道，这里是纵深防御）。
            continue

        # ---- 第一步：这条爆料已经有主了吗 ----
        target_id = idx.resolve_target(deal.source_id)
        if target_id is not None:
            refreshed += 1
            _refresh_known(db, target_id, cleaned, newest_seen, pending_points)
            continue

        # ---- 第二步：没有主，进归并判定 ----
        cand = Candidate(
            source=src,
            source_id=deal.source_id,
            title=cleaned.title,
            brand_field=deal.brand,
        )
        result = dedup.decide(cand, idx.pool)

        if result.decision is Decision.MERGE and result.target_id is not None:
            merged += 1
            # 记下这条爆料的新主人，之后同批再见到它直接走刷新分支。
            idx.by_ref[deal.source_id] = result.target_id
            _refresh_known(db, result.target_id, cleaned, newest_seen, pending_points)
            continue

        # ---- 第三步：新建（MARK 或 NEW）----
        # ★ 通道必须由数据源自己声明，公共管道不兜底：兜底会把某个源的名字
        # 写进通道，而 recorded_at 的语义跟着这个字段走（见 base.py）。
        # 忘了声明是编程错误，宁可让它响。
        channel = (deal.channel or "").strip()
        if not channel:
            raise ValueError(
                f"数据源 {src!r} 没有声明 channel（RawDeal.channel）。"
                "这个字段决定 price_history.recorded_at 的语义，"
                "不能由公共管道猜——smzdm 源曾经就是被这里兜底成了 "
                "'jingxuan'，而它填的是源站的 'youhui' 标记。"
            )

        row = ProductRow(
            source=src,
            source_id=deal.source_id,
            source_channel=channel[:16],
            title=cleaned.title,
            title_norm=cleaned.title_norm,
            brand=cleaned.brand,
            platform=deal.platform,
            category=deal.category,
            url=deal.url,
            image_url=deal.image_url,
            price=cleaned.price,
            price_max=cleaned.price_max,
            # ★ 销量和店铺直接透传源给的值，管道不猜。这两个字段不在
            # upsert 的更新列里（见 store.py），所以不提供销量的源不会把
            # 已有销量抹成 NULL。
            sales=deal.sales,
            shop_id=_resolve_shop_id(db, shop_cache, deal.platform, deal.shop_name),
            last_crawled_at=utcnow(),
            suspected_duplicate_of=(
                result.target_id if result.decision is Decision.MARK else None
            ),
        )
        product_id = upsert_products(db, [row])[(src, deal.source_id)]

        if result.decision is Decision.MARK:
            marked += 1
        else:
            new += 1

        # ★ 一边入库一边更新索引：不更新的话，同一批里的同款两条爆料会建两个商品。
        idx.by_source_id[deal.source_id] = product_id
        idx.pool.append(
            ExistingProduct(
                product_id=product_id,
                title_norm=cleaned.title_norm,
                # ★ 池子里放**规范形态**（不是 cleaned.brand 显示形态）：
                # 混用会让批内归并永远失败、跨批正常——只在某一批内失效，最难查。
                brand=dedup.extract_brand(cleaned.brand),
                model_tokens=dedup.extract_model_tokens(cleaned.title_norm),
                tokens=frozenset(dedup.tokenize(cleaned.title_norm)),
                source_id=deal.source_id,
            )
        )

        # ★ 登记新建商品的价格时间基准：不登记的话，新建后第一条归并进来的
        # 爆料会以为"这是最新的"，拿更旧的价格覆盖掉刚写入的新价（全程无报错）。
        if cleaned.recorded_at is not None:
            newest_seen[product_id] = cleaned.recorded_at

        _queue_price_point(pending_points, product_id, cleaned)

    # ★ prices_new 取 record_prices 的返回值（RETURNING 行数），不是
    # len(pending_points)——否则第二轮会显示"新增价格点 100"而实际是 0。
    return CrawlReport(
        fetched=len(deals),
        products_new=new,
        products_merged=merged,
        products_marked=marked,
        products_refreshed=refreshed,
        prices_new=record_prices(db, pending_points),
    )


# --- 辅助函数（主流程的刷新 / 归并 / 新建三个分支共用）---


def _resolve_shop_id(
    db: Session,
    cache: dict[tuple[str, str], int],
    platform: str | None,
    shop_name: str | None,
) -> int | None:
    """解析店铺名 → shop_id；源没给店铺名就返回 None。

    ★ 会话内缓存：同一批里反复出现同一个店铺是常态，避免反复走
      "查了没有、建一个"。
    ★ 平台为空的店铺名直接当没有处理：SQL 里多个 NULL 互不相等，
      (NULL, 'X') 能重复插入、唯一约束失效（ensure_shop 里也挡一道）。
    """
    if not shop_name or not platform:
        return None
    key = (platform, shop_name)
    if key not in cache:
        cache[key] = ensure_shop(db, platform, shop_name)
    return cache[key]


def _queue_price_point(
    pending: list[PricePoint], product_id: int, cleaned: CleanedDeal
) -> None:
    """把一个价格点排进待写列表。

    ★ 没有价格或没有时间戳就不写价格点（实测 100 条真实爆料里 2 条没有价格）：
      price_history 两列都是 NOT NULL，塞 None 会在批量写入那一刻抛
      IntegrityError——前面几十条已写进去，看起来像整轮失败。
      在入列之前拦住，错误就不会传播。
    """
    if cleaned.price is None or cleaned.recorded_at is None:
        return
    pending.append(
        PricePoint(
            product_id=product_id,
            price=cleaned.price,
            recorded_at=cleaned.recorded_at,
            # ★ source_ref = 爆料文章 id，是价格点的身份：同一篇文章重复采集
            # 撞唯一约束被跳过。不用 recorded_at 做身份的理由见 store.py 顶部。
            source_ref=cleaned.deal.source_id,
        )
    )


def _newest_known_recorded_at(
    db: Session, target_id: int, newest_seen: dict[int, datetime]
) -> datetime | None:
    """我们**已知**的这个商品最新的 price 事件时间。

    ★ 必须同时看内存和数据库：只看内存时，第 2 批看不到第 1 批记下的时间
      （newest_seen 每次调用新建），更旧的爆料会把价格改旧；只看数据库时，
      同批里还躺在 pending、没落库的价格点看不到。取两者最大值。
    ★ 查询走 ix_ph_product_recorded(product_id, recorded_at)，能直接定位 max。
    """
    db_prev = db.execute(
        select(func.max(PriceHistory.recorded_at)).where(
            PriceHistory.product_id == target_id
        )
    ).scalar_one_or_none()

    mem_prev = newest_seen.get(target_id)
    if db_prev is None:
        return mem_prev
    if mem_prev is None or db_prev > mem_prev:
        return db_prev
    return mem_prev


def _refresh_known(
    db: Session,
    target_id: int,
    cleaned: CleanedDeal,
    newest_seen: dict[int, datetime],
    pending: list[PricePoint],
) -> None:
    """刷新一个已有商品（归并分支与"已有主"分支共用）。

    ★ 判据只有一条：价格更新必须能定序。爆料按时间倒序，所以只在"这条比
      已知最新还新"时才覆盖价格；缺价格或缺时间戳都不能定序，只刷 last_crawled_at。
    ★ 用 Core 的 update() 而不是改 ORM 对象：只改几个字段，避免"先 SELECT
      再 UPDATE"和身份映射带来的读到内存值/库值的不确定性。
    """
    # ★ 价格点先排进去：即使这条更旧、不改商品价，它本身也是一条真实数据，
    # 该留在价格历史里；唯一约束会挡掉重放产生的重复行。
    _queue_price_point(pending, target_id, cleaned)

    # ---- 能不能定序？ ----
    if cleaned.recorded_at is None or cleaned.price is None:
        db.execute(
            update(Product).where(Product.id == target_id).values(last_crawled_at=utcnow())
        )
        return

    prev = _newest_known_recorded_at(db, target_id, newest_seen)
    if prev is not None and cleaned.recorded_at < prev:
        # 见过更新的爆料了，这条是旧的：不覆盖价格，也不改 last_crawled_at。
        # ★ 判据是 `<` 而不是 `<=`：时间戳相等 = 同一价格事件（重放），放行无
        # 风险且必要——否则 last_crawled_at 永远停住，增量采集的"刷新"失效。
        return
    newest_seen[target_id] = cleaned.recorded_at

    values: dict[str, object] = {
        "price": cleaned.price,
        "price_max": cleaned.price_max,
        "last_crawled_at": utcnow(),
    }
    # ★ url / image_url 一起刷新（列表页想显示最新那条爆料的图），但只在这条
    # 确实带了值时才写：不能拿"不知道"覆盖"已知"（实测 3 条没有 <img>，
    # 无条件写会抹掉已有缩略图）。
    if cleaned.deal.url:
        values["url"] = cleaned.deal.url
    if cleaned.deal.image_url:
        values["image_url"] = cleaned.deal.image_url

    db.execute(update(Product).where(Product.id == target_id).values(**values))
