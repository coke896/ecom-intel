"""入库管道 —— 把一批原始爆料变成数据库里的商品和价格点。

================================================================================
它在整条链路上的位置
================================================================================

    DataSource.fetch() / parse_files()   →  list[RawDeal]   原始，一个字没洗
                    │
                    ▼
              ★ ingest_deals()  ← 本文件：清洗 → 找主 → 归并判定 → 写入
                    │
                    ▼
    store.upsert_product / record_prices  →  数据库

**Mock 和 SMZDM 走的是这同一条管道。** 两个源的差别只在"怎么拿到 RawDeal"，
从清洗往后完全一致——所以本文件里没有一个字提到数据源的名字。

================================================================================
每条爆料先回答一个问题：它已经有主了吗
================================================================================
这是本文件的核心，也是"第二轮采集"能不能正确工作的分水岭。

    resolve_target(deal) ─┬─ 有主  → 【刷新】不新建、不归并
                          │          已入库的爆料第二轮再见到时走这条
                          └─ 无主  → 进归并判定：MERGE / MARK / NEW

"有主"有两种形态，两种都必须认得：

    ① 它自己就是一个商品      products.source_id == deal.source_id
    ② 它被归并进了别的商品    price_history.source_ref == deal.source_id
                              → 主是那条价格记录挂在的 product_id

★ 漏掉 ② 的后果很具体：一条爆料第一轮被归并进商品 X，
  第二轮它"自己有主吗"查 products 表查不到（它从来没有自己的商品行），
  于是走了归并判定 → 又会命中 X → 又报一次"归并"，
  界面上每采一次就显示"归并 1"，而实际什么都没发生。

★ 为什么不用 (source, source_id) 直接当"有没有见过"的判据：
  因为 ② 那种爆料在 products 表里根本不存在。
  价格记录上的 source_ref 是**唯一**能回答"这条爆料去哪了"的地方——
  这也正是当初把 source_ref 做成一等字段、而不是随手丢掉的回报。

================================================================================
★ 不做这个判断会怎样（我在写这一版之前差点犯的错）
================================================================================
如果每条爆料都直接进归并判定，那么第二轮采集时：

  load_existing 读回来的池子里**包含这条爆料自己的商品行**，
  而 dedup.decide() 是把候选和池子里每一个比对——
  于是候选跟**它自己**比对：品牌相同、型号 token 相同、包含度 1.0
  → 判定 MERGE，归并到它自己。

结果是第二轮报告"解析 100 条，新商品 0，归并 100"。
**数据库内容其实是对的**（价格没变、价格点被唯一约束挡掉），
所以这个 bug 不会以任何形式报警——它只让报告变成一个假数字。
而"连抓两轮看报告"恰恰是阶段 4 的验收方式，假数字会让验收失去意义。

dedup.decide() 那边也加了一道 source_id 自比对防护（纵深防御），
但**主要机制在这里**：认出来了就压根不进比对。

================================================================================
★ 另外两个不写清楚就一定会出错的细节
================================================================================

【一】MERGE / 刷新分支里那个 UPDATE 不能省
    只写 price_history 不更新 products 的话，商品列表页显示的还是旧价，
    **趋势图和列表对不上**——而且这种不一致极难察觉，
    因为两边单看都是"对"的。

【二】MERGE / 刷新更新价格时要看时间新旧
    爆料是按时间倒序排的（feed 最上面是最新的），所以同一个商品的多条爆料
    里，**第一条是它最新的价格**，后面那些是更早的。
    如果无条件用后到的覆盖先到的，最终存下来的会是**最旧**的那个价格。

    ★ 而且"见过的最新时间"必须是**跨批次**的，否则这条规则只在一批之内成立。
      worker 每 20 条调一次 ingest_deals（PROGRESS_EVERY），而当初
      newest_seen 这个字典是**每次调用新建**的——第 2 批看不到第 1 批记下的
      时间，于是一条更旧的爆料照样能把价格改旧。
      实测复现过：同一个商品，500 元的爆料在第 1 批、100 元的排在第 26 位，
      按 20 条一批跑完，product.price = 100，
      而 price_history 里最新的一点是 500——列表页和趋势图自相矛盾。
      所以基准要取「内存里的」和「库里已有的」两者中更新的那个，
      见 _newest_known_recorded_at。

【三】没有价格 / 没有时间戳的爆料，不能产生价格点
    price_history 的 price 和 recorded_at 都是 NOT NULL。
    实测 100 条真实爆料里有 **2 条没有价格**（卡片上确实没写），
    直接拿 None 去插会抛 IntegrityError —— 而且是在跑到第 80 条的时候才炸，
    前面 79 条已经写进去了，任务状态变成 failed，看起来像"整轮都失败了"。

    正确处理：商品照建（这个爆料确实存在，只是没写价格），
    **但不为它写价格点**。这两件事是独立的。
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


# ===========================================================================
# 读已有商品
# ===========================================================================


@dataclass(slots=True)
class ExistingIndex:
    """某个数据源已入库的东西，三种视角一次读齐。

    ★ 为什么三种视角要一起读，而不是各读各的：
      它们描述的是同一批数据（"库里现在有什么"），分开读会出现
      时间差——读到一半时别的事务插了一条，两个视角就对不上了。
      更实际的理由是：一次 SELECT 比三次好写也好测。

    ★ 这些字典在 ingest_deals 的循环里会被**持续更新**（新商品、新归并），
      所以同一个批次里后面的爆料能看到前面刚建的商品的。
      没有这一步的话，"同一批里出现同一个商品的两条爆料"会建成两个商品——
      而归并的全部意义就是避免这件事。
    """

    # 归并比对用的池子（带 token，能算相似度）
    pool: list[ExistingProduct] = field(default_factory=list)
    # 形态 ①：这条爆料自己就是一个商品  source_id → product_id
    by_source_id: dict[str, int] = field(default_factory=dict)
    # 形态 ②：这条爆料被归并进了别的商品  source_ref → product_id
    by_ref: dict[str, int] = field(default_factory=dict)

    def resolve_target(self, source_id: str) -> int | None:
        """这条爆料在库里已经有主了吗？有就返回主的 product_id。

        ★ 先查 by_source_id 再查 by_ref：顺序不影响正确性
          （自己就是商品时，它的价格点也挂在同一个 product 上，
           两个字典给出同一个答案），但先查自己更直观。
        """
        own = self.by_source_id.get(source_id)
        if own is not None:
            return own
        return self.by_ref.get(source_id)


def load_existing(db: Session, source: str) -> ExistingIndex:
    """把某个数据源已入库的商品和价格记录读成归并比对需要的形状。

    ★ 一次性读完，而不是每条爆料查一次库。
      100 条爆料 × 每条查 1000 个商品 = 10 万次行读取，
      而这里只要 2 次查询。

    ★ token 是**从 title_norm 现算**的，不是从数据库读的。
      因为 products 表里没有存 token 列——token 是 normalize_title 的推导结果，
      存一份就等于多了一处会跟算法版本脱节的数据。
      （改了分词规则却不重新计算存量 token，会让新旧商品用两套规则比对，
        而这种错误完全静默：比对照样出结果，只是结果是错的。）
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
                # ★ 品牌和 token 一样，**从库里的值现算规范形态**，
                #   而不是假定库里存的就是规范形态。
                #
                #   原因：products.brand 存的是**显示形态**（"Soundcore/声阔"，
                #   给人看的、保留大小写），而硬门槛比对要的是规范形态
                #   （"soundcore|声阔"）。两种形态如果混进同一个比对里，
                #   同一品牌的商品会永远匹配不上——而且是静默的，
                #   表现只是"没合并"，不报错。
                #
                #   extract_brand 是幂等的，所以库里无论存的是哪种形态，
                #   这一行都能得到正确结果。
                brand=dedup.extract_brand(brand),
                model_tokens=dedup.extract_model_tokens(norm),
                tokens=frozenset(dedup.tokenize(norm)),
                source_id=source_id,
            )
        )
        idx.by_source_id[source_id] = pid

    # ★ 形态 ② 的来源：价格记录上的 source_ref。
    #   关联 products 是为了限定在本数据源内——
    #   不同源的 source_ref 理论上可能撞（都是数字 id），
    #   不限定的话 smzdm 的爆料可能被认成 mock 的商品的重复。
    ref_rows = db.execute(
        select(PriceHistory.source_ref, PriceHistory.product_id)
        .join(Product, Product.id == PriceHistory.product_id)
        .where(Product.source == source, PriceHistory.source_ref.is_not(None))
    ).all()
    for source_ref, pid in ref_rows:
        idx.by_ref[source_ref] = int(pid)

    return idx


# ===========================================================================
# 清洗
# ===========================================================================


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

    ★ 清洗【不抛异常】——这是 cleaner 模块的核心承诺，这里依赖它：
      采集管道里一条脏数据不能让整轮采集挂掉。
      所以价格洗不出来时是 None，而不是异常。
    """
    # 全角转半角 + 去空白。★ 必须在 parse_price 之前做：
    # 全角数字 `１９９` 和半角 `199` 是不同的字符，正则会漏掉前者。
    title = normalize_width(clean_text(deal.title) or "")
    price, price_max = parse_price(deal.price_raw)

    return CleanedDeal(
        deal=deal,
        title=title,
        title_norm=dedup.normalize_title(title),
        # ★ 品牌取**显示形态**（源站原文，只压空白），不是规范形态。
        #
        #   因为这一列是要给人看的、要显示在界面上的，而
        #   "SKECHERS/斯凯奇" 比 "skechers|斯凯奇" 好看得多。
        #   和 seed.py 写入的 "Soundcore/声阔" 也是同一个形状——
        #   两种入口写进库里的东西一致，界面才不会因为
        #   "这批数据是采集来的还是生成的"而显示成两种样子。
        #
        #   ★ 比对时用的规范形态由 load_existing 现算（extract_brand 是幂等的）。
        #     这跟 token 的处理方式完全一样：库里存给人看的，比对时算机器要的。
        #
        #   品牌只来自源站字段，不从标题猜——源站字段是权威的，
        #   而标题里可能出现"适配华为"这种，猜错了会同时污染硬门槛和品牌分析。
        brand=dedup.display_brand(deal.brand),
        price=price,
        price_max=price_max,
        recorded_at=deal.recorded_at,
    )


# ===========================================================================
# 主入口
# ===========================================================================


def ingest_deals(
    db: Session,
    deals: list[RawDeal],
    *,
    source: str | None = None,
) -> CrawlReport:
    """把一批爆料清洗、找主、归并、写入。**调用方负责 commit。**

    ★ 不在这里 commit，是为了让"一批爆料"成为一个事务：
      中途失败时整批回滚，不会留下"前 79 条进去了、后 21 条没进去"
      这种半截状态——那种状态比整批失败难查得多。

    ★ 本函数是**幂等**的（同一批爆料重放两次，数据库内容不变）：
      · 商品走 upsert（(source, source_id) 唯一约束）
      · 价格点走 ON CONFLICT DO NOTHING（(product_id, source_ref) 唯一约束）
      · 第二轮走"刷新"分支，不重复计数
      这正是阶段 4 的验收点「连抓两轮，第二轮不报 IntegrityError」。
    """
    if not deals:
        return CrawlReport()

    src = source or deals[0].source

    # 已刷新的商品：target_id → 我们见过的**最新** record time。
    # 用来实现【二】：不用更早的爆料覆盖更新的价格。
    newest_seen: dict[int, datetime] = {}

    idx = load_existing(db, src)

    # ★ 用局部计数器，最后一次性构造 CrawlReport。
    #   CrawlReport 是 frozen 的（不可变），原地累加会抛 FrozenInstanceError。
    #   这个约束是好事：报告对象一旦交出去就不该再变，
    #   否则调用方拿到的那个引用会在它眼皮底下变化。
    new = merged = marked = refreshed = 0

    pending_points: list[PricePoint] = []
    # (平台, 店名) → shop_id 的会话内缓存。见 _resolve_shop_id。
    shop_cache: dict[tuple[str, str], int] = {}

    for deal in deals:
        cleaned = clean_deal(deal)
        if not cleaned.title:
            # 没有标题就没法参与归并比对，直接跳过。
            # （解析层已经过滤了一道，这里是纵深防御。）
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
        # ★ 通道必须由数据源自己声明（见 base.py 里 RawDeal.channel 的注释）。
        #   这里**不再兜底**。原先写的是 `deal.channel or "jingxuan"`——那等于
        #   把 SMZDM 的通道名写进了公共管道：任何源忘了声明，都会被标成一个
        #   它根本不是的通道，而 recorded_at 的语义正是跟着这个字段走的
        #   （models.py 第 208-212 行）。NULL 会让语义「未知」，标错却会让
        #   语义「错误」——后者更糟，因为它看起来一切正常。
        #   宁可让它响：忘了声明是编程错误，不是运行时状况。
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
            # ★ 销量和店铺都直接透传源给的值，管道不猜也不补默认值。
            #   SMZDM 恒为 None（页面上确实没有），Mock 有值。
            #
            #   ★ 而且这两个字段**不在 upsert 的更新列里**（见 store.py），
            #     所以"一个不提供销量的源去采集一个已有销量的商品"时，
            #     库里的销量不会被抹成 NULL。这个性质不是巧合——
            #     它正是"更新列要显式列出"这条规则带来的。
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

        # ★ 一边入库一边更新索引：**同一批里后面的爆料要能看到它**。
        #   不更新的话，同一批里出现"同一个商品的两条爆料"时会建成两个商品。
        idx.by_source_id[deal.source_id] = product_id
        idx.pool.append(
            ExistingProduct(
                product_id=product_id,
                title_norm=cleaned.title_norm,
                # ★ 池子里放的是**规范形态**，不是 cleaned.brand（显示形态）。
                #
                #   这一行我第一版写的就是 `brand=cleaned.brand`——刚把
                #   clean_deal 改成存显示形态，转头就在这儿把显示形态
                #   塞进了比对池。后果是：同一批里新建的商品，
                #   品牌是 "Soundcore/声阔"（没有 "|"，被当成一个整体），
                #   而池子里从库里读回来的那些是 "soundcore|声阔"，
                #   于是**批内归并永远失败，跨批归并正常**。
                #
                #   这种"只在某一批内失效"的 bug 最难查，因为它复现不了：
                #   把同一批分两次采就正常了。
                #
                #   教训是：显示形态和规范形态的边界必须只有一处，
                #   而"进比对池"和"入库"是两条不同的路。
                brand=dedup.extract_brand(cleaned.brand),
                model_tokens=dedup.extract_model_tokens(cleaned.title_norm),
                tokens=frozenset(dedup.tokenize(cleaned.title_norm)),
                source_id=deal.source_id,
            )
        )

        # ★ 登记这个新建商品的价格时间基准。
        #
        #   漏掉这一步的后果很具体：newest_seen 只在 _refresh_known 里写，
        #   而 NEW 分支是**直接**把 cleaned.price 写进商品行的。
        #   于是新建之后第一条归并进来的爆料会发现 newest_seen 里没有这个
        #   product_id（prev is None）→ 认定"这是最新的"→ 拿一个**更旧**的
        #   价格覆盖掉刚写进去的新价。商品价格就此变旧，而全程没有任何报错。
        #
        #   这个 bug 只有在"同一批里既有新建又有归并"时才出现——
        #   也就是归并功能第一次真正派上用场的那一刻。
        if cleaned.recorded_at is not None:
            newest_seen[product_id] = cleaned.recorded_at

        _queue_price_point(pending_points, product_id, cleaned)

    # ★ prices_new 取 record_prices 的返回值（RETURNING 的行数），
    #   不是 len(pending_points)——后者会把"已存在被跳过"的也算进去，
    #   于是第二轮采集会显示"新增价格点 100"，而实际上一条新的都没有。
    return CrawlReport(
        fetched=len(deals),
        products_new=new,
        products_merged=merged,
        products_marked=marked,
        products_refreshed=refreshed,
        prices_new=record_prices(db, pending_points),
    )


# ---------------------------------------------------------------------------
# 下面两个函数是"写入一个价格点"和"刷新一个已有商品"，
# 抽出来是因为主流程里有三个地方要走它们（刷新 / 归并 / 新建）。
# ★ 写成模块级函数而不是方法：本模块没有类，也不该为这两个函数造一个类。
# ---------------------------------------------------------------------------


def _resolve_shop_id(
    db: Session,
    cache: dict[tuple[str, str], int],
    platform: str | None,
    shop_name: str | None,
) -> int | None:
    """解析店铺名 → shop_id；源没给店铺名就返回 None。

    ★ 带一个会话内缓存，因为 ensure_shop 每次都要查一次库。
      100 条爆料可能有 100 个不同店铺（也常常只有 5 个），
      缓存把最坏情况从 100 次查询压到 100 次、最好情况压到 5 次。
      更实际的理由：同一批里反复出现同一个店铺是常态，
      不缓存的话每次都要走一遍"查了没有、建一个"。

    ★ 平台为空的店铺名直接当没有处理，而不是拿 None 去建店铺：
      shops 的唯一约束是 (platform, name)，而 SQL 里多个 NULL 互不相等，
      所以 (NULL, 'X') 能重复插进去很多行，唯一约束失效。
      （同样的话 ensure_shop 里也写了一遍——两边都挡住，
        因为这是"数据静默重复"类型的坑，不值得赌哪一边先被执行到。）
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

    ★ 关键判据：**没有价格或没有时间戳就不写价格点**。
      price_history 这两列都是 NOT NULL，塞 None 进去会抛 IntegrityError，
      而抛出的位置在批量写入那一刻——那时前面几十条已经写进去了，
      任务看起来像"整轮失败"，实际只有一条有问题。
      在**入列之前**拦住，错误就不会传播。

      （实测 100 条真实爆料里 2 条没有价格，所以这条不是理论防御。）
    """
    if cleaned.price is None or cleaned.recorded_at is None:
        return
    pending.append(
        PricePoint(
            product_id=product_id,
            price=cleaned.price,
            recorded_at=cleaned.recorded_at,
            # ★ source_ref = 爆料文章 id，这是价格点的【身份】。
            #   同一篇文章重复采集 → 撞唯一约束 → 跳过（DO NOTHING）。
            #   不用 recorded_at 做身份的理由见 scraper/store.py 顶部。
            source_ref=cleaned.deal.source_id,
        )
    )


def _newest_known_recorded_at(
    db: Session, target_id: int, newest_seen: dict[int, datetime]
) -> datetime | None:
    """我们**已知**的这个商品最新的 price 事件时间。

    ★ 为什么必须同时看内存和数据库——只看任何一边都有洞：

      只看内存（原实现）：worker 每 20 条调一次 ingest_deals，
      而 newest_seen 是每次调用新建的字典。第 2 批看不到第 1 批记下的
      "最新时间"，于是一条更旧的爆料会把商品价格改旧（已实测复现）。

      只看数据库：同一批里前面几条的价格点还躺在 pending 里没落库
      （ingest_deals 末尾才 record_prices），查询根本看不到它们。

      取两者的最大值，两个场景都覆盖。

    ★ 这个查询走 ix_ph_product_recorded(product_id, recorded_at)：
      product_id 是索引前导列，recorded_at 紧随其后，
      所以 SQLite 能在索引里直接定位到 max，不是全表扫。
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

    ★ 判据只有一条：**价格更新必须能定序**。

      见模块顶部【二】：爆料是时间倒序的，第一条最新。
      所以只在"这条比我们见过的最新还新"时才更新商品价格。
      无条件覆盖的话，存下来的会是最旧的价格——
      而它看起来完全合法（是个真实出现过的价格），只是**过期了**。

      于是"缺价格"和"缺时间戳"collapse 成同一条路径：
      两者都不能确定这条爆料该不该覆盖当前价格 → **只刷 last_crawled_at**。
      （一开始我把"缺时间戳"写成了"照样更新价格"，那等于用一个无法定序的
        值去覆盖一个能定序的值——正是上面那个 bug 的弱化版。）

    ★ 用 Core 的 update() 而不是改 ORM 对象：
      这里只需要改几个字段，加载整个对象再改会让 SQLAlchemy 走
      "先 SELECT 再 UPDATE"两步，而且会把对象放进身份映射——
      而调用方随后可能还要读这个对象（比如统计），
      读到的是内存里改过的值还是库里的值，取决于有没有 flush，
      这种不确定性在批量循环里很难推理。
    """
    # ★ 价格点先排进去：即使商品行的价格不该被改（这条更旧），
    #   这个价格点本身也是一条真实数据，该留在价格历史里。
    #   唯一约束会挡掉重复的，所以重放同一批不会产生新行。
    _queue_price_point(pending, target_id, cleaned)

    # ---- 能不能定序？ ----
    if cleaned.recorded_at is None or cleaned.price is None:
        db.execute(
            update(Product).where(Product.id == target_id).values(last_crawled_at=utcnow())
        )
        return

    prev = _newest_known_recorded_at(db, target_id, newest_seen)
    if prev is not None and cleaned.recorded_at < prev:
        # 我们见过更新的爆料了，这条是旧的：不覆盖价格，也不改 last_crawled_at
        #（last_crawled_at 表示"最近一次采集见到它的时间"，而这次采集
        #  在处理一条旧数据，不代表现在见到的是它）。
        #
        # ★ 判据是 `<` 而不是 `<=`。时间戳**相等**意味着这是同一个价格事件
        #   （同一篇爆料被重放，第二次采集就是这种情况），价格本来就是同一个
        #   值，放行没有风险；而放行是**必要的**——增量采集的"刷新 N 条"
        #   就是要靠这一步把 last_crawled_at 推向前，
        #   挡掉的话它永远停在第一次采集的那天，
        #   统计页的"最近采集时间"会一直显示过期值。
        return
    newest_seen[target_id] = cleaned.recorded_at

    values: dict[str, object] = {
        "price": cleaned.price,
        "price_max": cleaned.price_max,
        "last_crawled_at": utcnow(),
    }
    # ★ url / image_url 也一起刷新：它们指向的是**这次爆料**的图片，
    #   而列表页显示商品图时希望看到最新那条爆料的图。
    #
    #   ★ 但只在这条爆料**确实带了值**的时候才写。
    #     判据和上面 sales / shop_id 不在更新列里是同一条：
    #     **不能拿"不知道"去覆盖"已知"**。
    #
    #     实测 100 条真实爆料里有 3 条卡片上没有 <img>（image_url 为 None），
    #     无条件写下去就会把商品已经有的缩略图抹掉——而且抹得没有痕迹，
    #     表现只是"这个商品的图突然变成占位图了"，没人会联想到采集。
    if cleaned.deal.url:
        values["url"] = cleaned.deal.url
    if cleaned.deal.image_url:
        values["image_url"] = cleaned.deal.image_url

    db.execute(update(Product).where(Product.id == target_id).values(**values))
