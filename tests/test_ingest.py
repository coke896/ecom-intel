"""入库管道测试 —— 归并接入、幂等、价格新旧。

================================================================================
这份测试守的是什么
================================================================================
scraper/ingest.py 是"归并"从一个纯函数库变成流水线的地方，
也是阶段 4 验收点「连抓两轮，第二轮不报 IntegrityError」的现场。

这里最重要的三组用例：

  · test_second_round_***   连抓两轮：第二轮必须是"刷新 100 / 新增价格点 0"
  · test_older_deal_***     归并时不能用更早的爆料覆盖更新的价格
  · test_deal_without_***   没有价格的爆料不能产生价格点（NOT NULL 防线）

★ 第一组抓到的 bug 是"报告变成假数字"，第二组抓到的是"价格静默变旧"，
  第三组抓到的是"整轮任务看起来失败了"。
  三个都不会抛异常、都不会让数据明显损坏——**全都是静默的**。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import PriceHistory, Product
from scraper import dedup, ingest
from scraper.base import RawDeal
from scraper.dedup import (
    Candidate,
    Decision,
    ExistingProduct,
    decide,
)
from scraper.sources.smzdm import SmzdmSource

FIXTURES = Path(__file__).parent / "fixtures"

# 真实页面的抓取时间落在 2026-09-27 晚上。构造用例时围绕它取基准时刻，
# 让"更新/更旧"的语义一眼可读。
BASE_TIME = datetime(2026, 9, 27, 20, 0, 0)


def deal(
    source_id: str,
    title: str,
    *,
    price_raw: str | None = "199元",
    recorded_at: datetime | None = BASE_TIME,
    brand: str | None = None,
    channel: str | None = None,
    url: str | None = None,
    source: str = "smzdm",
) -> RawDeal:
    """造一条爆料。默认值挑成"最普通的那一条"，用例只写它关心的字段。"""
    return RawDeal(
        source=source,
        source_id=source_id,
        title=title,
        price_raw=price_raw,
        recorded_at=recorded_at,
        brand=brand,
        channel=channel,
        url=url,
    )


def count(db: Session, model) -> int:
    return int(db.execute(select(func.count()).select_from(model)).scalar_one())


def product_by_source_id(db: Session, source_id: str) -> Product:
    return db.execute(
        select(Product).where(Product.source == "smzdm", Product.source_id == source_id)
    ).scalar_one()


# ===========================================================================
# 基本形状
# ===========================================================================


def test_empty_input_returns_empty_report(db: Session):
    """空输入不该查库、不该建任何东西。

    ★ 这不只是"省一次查询"：ingest_deals 里有一句
      `src = source or deals[0].source`——空列表时 deals[0] 会 IndexError。
      提前返回是那道防线的实测。
    """
    report = ingest.ingest_deals(db, [])
    assert report.fetched == 0
    assert report.products_new == 0
    assert count(db, Product) == 0


def test_one_deal_creates_one_product_and_one_price_point(db: Session):
    report = ingest.ingest_deals(db, [deal("1001", "安克 充电宝 20000毫安 快充")])

    assert report.fetched == 1
    assert report.products_new == 1
    assert report.prices_new == 1

    p = product_by_source_id(db, "1001")
    assert p.title == "安克 充电宝 20000毫安 快充"
    assert p.price == pytest.approx(199.0)
    # ★ sales 永远是 None：SMZDM 页面不提供销量（见 docs 发现 2）。
    #   写成断言是为了防止将来有人"顺手补一个默认值"——
    #   那会让"真实数据没有销量"这个事实在数据库里看不出来。
    assert p.sales is None
    assert p.shop_id is None
    assert p.suspected_duplicate_of is None

    ph = db.execute(select(PriceHistory)).scalars().all()
    assert len(ph) == 1
    # ★ 价格点的身份是 source_ref（爆料文章 id），不是时间。
    assert ph[0].source_ref == "1001"
    assert ph[0].recorded_at == BASE_TIME


def test_source_channel_falls_back_to_jingxuan(db: Session):
    """没给 channel 时回退到 'jingxuan'。

    真实数据里 channel 来自 onclick 的第 4 个参数，值是 'youhui'。
    回退值存在是为了"源站哪天不给了"时不至于写进 NULL——
    source_channel 是列表页筛选用的，NULL 会让筛选漏掉这些行。
    """
    ingest.ingest_deals(db, [deal("1001", "安克 充电宝"), deal("1002", "小米 手环", channel="youhui")])

    assert product_by_source_id(db, "1001").source_channel == "jingxuan"
    assert product_by_source_id(db, "1002").source_channel == "youhui"


def test_price_is_parsed_from_raw_not_guessed(db: Session):
    """价格走 cleaner.parse_price，返回的是二元组（最低价, 最高价）。"""
    ingest.ingest_deals(db, [deal("1001", "安克 充电宝", price_raw="券后199-299元")])

    p = product_by_source_id(db, "1001")
    assert p.price == pytest.approx(199.0)
    assert p.price_max == pytest.approx(299.0)


# ===========================================================================
# ★ 没有价格的爆料：商品照建，价格点不建
# ===========================================================================


def test_deal_without_price_creates_product_but_no_price_point(db: Session):
    """★ price_history.price 是 NOT NULL，没有价格的爆料塞 None 会抛 IntegrityError。

    实测 100 条真实爆料里有 2 条卡片上确实没写价格，所以这不是理论防御。

    正确的语义：商品照建（这条爆料确实存在），但不为它写价格点。
    两件事是独立的——"有个商品被爆料了"和"我们知道它多少钱"不是一回事。
    """
    report = ingest.ingest_deals(
        db, [deal("1001", "安克 充电宝 20000毫安", price_raw=None)]
    )

    assert report.products_new == 1
    assert report.prices_new == 0
    assert product_by_source_id(db, "1001").price is None
    assert count(db, PriceHistory) == 0


def test_deal_without_timestamp_creates_product_but_no_price_point(db: Session):
    """同理，没有时间戳也无法形成一个价格点（recorded_at 也是 NOT NULL）。

    没有时间戳的价格点在趋势图上没有位置，存下来只会是个孤儿。
    """
    report = ingest.ingest_deals(
        db, [deal("1001", "安克 充电宝 20000毫安", recorded_at=None)]
    )

    assert report.products_new == 1
    assert report.prices_new == 0
    assert product_by_source_id(db, "1001").price == pytest.approx(199.0)
    assert count(db, PriceHistory) == 0


def test_mixed_batch_does_not_abort_on_the_bad_apple(db: Session):
    """★ 一条没价格的爆料不能影响同批其他爆料。

    这是"清洗不抛异常"承诺在管道层的实际兑现方式——
    如果实现改成"先全量构造 PricePoint 再批量写"，坏的那条会让
    整批在写入那一刻失败，前面几十条已经写进去了，任务状态变成 failed。
    在入列之前拦住，错误就不会传播。
    """
    deals = [deal(f"100{i}", f"商品{i} 20000毫安", price_raw=None if i == 5 else "199元")
             for i in range(10)]
    report = ingest.ingest_deals(db, deals)

    assert report.products_new == 10      # 10 条都建了商品
    assert report.prices_new == 9         # 只有 9 条有价格
    assert count(db, Product) == 10
    assert count(db, PriceHistory) == 9


# ===========================================================================
# ★ 归并
# ===========================================================================


def test_same_product_second_deal_merges(db: Session):
    """两条爆料指向同一商品 → 一个商品 + 两个价格点。

    构造方式：硬门槛要求型号 token 严格相等（这里都是 {20000毫安}），
    包含度 = |{安克,充电宝,20000毫安}| / min(4,3) = 1.0 ≥ 0.75 → 归并。
    """
    report = ingest.ingest_deals(
        db,
        [
            deal("1001", "安克 充电宝 20000毫安 快充", price_raw="199元"),
            deal("1002", "安克 充电宝 20000毫安", price_raw="189元",
                 recorded_at=BASE_TIME - timedelta(days=1)),
        ],
    )

    assert report.products_new == 1
    assert report.products_merged == 1
    assert count(db, Product) == 1

    # 两条爆料各贡献一个价格点，挂在同一个 product 上。
    ph = db.execute(select(PriceHistory).order_by(PriceHistory.recorded_at)).scalars().all()
    assert [p.source_ref for p in ph] == ["1002", "1001"]
    assert {p.product_id for p in ph} == {product_by_source_id(db, "1001").id}


def test_batch_internal_merge_keeps_the_newest_price(db: Session):
    """★ 归并时不能用更早的爆料覆盖更新的价格。

    爆料 feed 是【时间倒序】的（最上面最新），所以先处理的是最新那条。
    1001 最新（199 元），1002 是前一天的（189 元）。
    归并之后商品行的价格必须是 199。

    ★ 这个用例抓到了一个真实 bug：newest_seen 只在 _refresh_known 里登记，
      而 NEW 分支是直接写价格的。于是"新建之后第一条归并进来"的爆料
      会认为自己是第一个（prev is None）→ 拿旧价覆盖新价。
      商品价格静默变旧，全程不报错。
    """
    ingest.ingest_deals(
        db,
        [
            deal("1001", "安克 充电宝 20000毫安 快充", price_raw="199元"),
            deal("1002", "安克 充电宝 20000毫安", price_raw="189元",
                 recorded_at=BASE_TIME - timedelta(days=1)),
        ],
    )

    assert product_by_source_id(db, "1001").price == pytest.approx(199.0)


def test_merge_updates_product_row_not_only_price_history(db: Session):
    """★ MERGE 分支里那个 UPDATE 不能省。

    只写 price_history 不更新 products 的话，商品列表页显示的还是旧价，
    和趋势图对不上——而两边单看都是"对"的，所以这种不一致极难察觉。

    这里反着构造：先建一个旧商品（价格 100），再归并一条**更新的**爆料（88）。
    如果 MERGE 分支不 UPDATE products，price 会停在 100。
    """
    from scraper.store import ProductRow, upsert_products

    old_id = upsert_products(
        db,
        [ProductRow(
            source="smzdm",
            source_id="9001",
            title="安克 充电宝 20000毫安",
            title_norm="安克 充电宝 20000毫安",
            price=100.0,
        )],
    )[("smzdm", "9001")]
    db.flush()

    report = ingest.ingest_deals(
        db,
        [deal("1002", "安克 充电宝 20000毫安", price_raw="88元", recorded_at=BASE_TIME)],
    )

    assert report.products_merged == 1
    p = db.execute(select(Product).where(Product.id == old_id)).scalar_one()
    assert p.price == pytest.approx(88.0)
    assert p.last_crawled_at is not None


def test_later_deal_in_same_batch_can_merge_into_earlier_one(db: Session):
    """★ 同批新建的商品也必须进比对池。

    不把它加进池子里的话，同一批里出现"同一个商品的两条爆料"时会建成
    两个商品——而归并的全部意义就是避免这件事。
    （真实场景：精选页第 1 页和第 2 页有重叠，同一篇文章会出现两次。）
    """
    report = ingest.ingest_deals(
        db,
        [
            deal("1001", "安克 充电宝 20000毫安 快充", price_raw="199元"),
            deal("1002", "安克 充电宝 20000毫安", price_raw="189元",
                 recorded_at=BASE_TIME - timedelta(hours=1)),
        ],
    )

    assert count(db, Product) == 1
    assert report.products_merged == 1


def test_different_model_token_never_auto_merges(db: Session):
    """型号 token 不同 → 硬门槛一票否决，绝不自动合并。

    iPhone 15 128G vs iPhone 15 256G：这是两个 SKU，价格也不同。
    硬门槛的价值就在于**根本不进相似度计算**，比调阈值可靠得多。

    ★ 注意它**也不是 NEW**，而是 MARK —— 我写这个用例时第一版期望是"两个 NEW"，
      跑出来是"1 新建 + 1 待核对"，一开始以为是实现错了。

      实际上是设计如此：decide() 里有一条 conflict 分支，
      专门处理"文本极像但硬门槛拦下"的对。它不自动合并（保守），
      但也不当无事发生（否则这种最该被看见的情况会被静默丢弃）。
      漏归并可以人工恢复，直接丢弃则永远发现不了。

      所以断言写成 MARK 是对实现的理解，不是对实现的迁就——
      下面单独一条用例把这个分支钉住。
    """
    report = ingest.ingest_deals(
        db,
        [
            deal("1001", "苹果 iPhone 15 128G 蓝色 手机"),
            deal("1002", "苹果 iPhone 15 256G 蓝色 手机"),
        ],
    )

    assert report.products_merged == 0, "两个不同 SKU 被合并了 —— 价格序列会被污染"
    assert report.products_new == 1
    assert report.products_marked == 1
    assert count(db, Product) == 2


def test_conflict_branch_marks_but_does_not_merge(db: Session):
    """★ conflict 分支：包含度很高但型号 token 不同 → 标记，不合并。

    这是"最危险也最该被看一眼"的那类对：
    'iPhone 18 Pro' vs 'iPhone Duo'（包含度 0.800，高于四个真阳性中的三个），
    以及 'FreeClip 2' vs 'FreeClip'（文本极像，差一个数字）。

    任何单一阈值都不可能同时收下真阳性和排除这些假阳性——
    这正是硬门槛存在的理由。它们到不了相似度判定那一层。

    这里断言的是**两条独立的性质**：
      · 不合并（价格序列不能被污染）
      · 但被标记出来（不能静默丢弃）
    只断言前者的话，一个"遇到冲突就 return NEW"的实现也能通过。
    """
    report = ingest.ingest_deals(
        db,
        [
            deal("1001", "苹果 iPhone 18 Pro 手机"),
            deal("1002", "苹果 iPhone 18 Duo 手机"),
        ],
    )

    assert report.products_merged == 0
    marked = product_by_source_id(db, "1002")
    assert marked.suspected_duplicate_of == product_by_source_id(db, "1001").id


def test_grey_zone_near_miss_creates_product_and_marks_it(db: Session):
    """near_miss 分支：型号 token 相同、包含度落在 [0.50, 0.75) → 标记载。

    构造：两边都是 5 个 token，交集 3 个 → 包含度 3/5 = 0.6，落在灰区。

    ★ 为什么灰区下界是 0.50 而不是更低：
      低于它的两个标题大概率就是不同商品。把那些也标记成"疑似重复"，
      suspected_duplicate_of 会迅速充满假阳性，"待人工核对"永远核对不完。
      只有中间这档才是真正值得花人眼的。
    """
    report = ingest.ingest_deals(
        db,
        [
            deal("1001", "安克 充电宝 20000毫安 快充 便携"),
            deal("1002", "小米 充电宝 20000毫安 快充 移动电源"),
        ],
    )

    assert report.products_new == 1
    assert report.products_marked == 1
    marked = product_by_source_id(db, "1002")
    assert marked.suspected_duplicate_of == product_by_source_id(db, "1001").id


def test_low_similarity_is_plain_new(db: Session):
    """包含度低于灰区下界 → 普通新商品，**不打标记**。

    构造：两边各 5 个 token，交集 2 个 → 包含度 0.4。
    这条是上一条的反向断言：少了它，一个"全都标记成疑似重复"的实现
    也能让灰区用例通过。
    """
    report = ingest.ingest_deals(
        db,
        [
            deal("1001", "安克 充电宝 20000毫安 快充 便携"),
            deal("1002", "小米 充电宝 20000毫安 大容量 移动电源"),
        ],
    )

    assert report.products_new == 2
    assert report.products_marked == 0
    assert product_by_source_id(db, "1002").suspected_duplicate_of is None


# ===========================================================================
# ★ 连抓两轮（阶段 4 的验收点）
# ===========================================================================


def test_second_round_reports_refresh_not_new(db: Session):
    """★ 连抓两轮：第二轮不报 IntegrityError，也不该报"新商品 100"。

    第二轮的正确输出是「新商品 0，归并 0，刷新 N，新增价格点 0」——
    这一行读起来就说明增量采集在工作。

    ★ 这里守的是报告的真实性，不是数据的正确性。
      数据其实两轮都对（upsert 更新同一行、价格点被唯一约束挡掉），
      所以这个 bug 完全静默，只让报告变成一个假数字。
      而"连抓两轮看报告"恰恰是验收的方式，假数字会让验收失去意义。
    """
    deals = [
        deal("1001", "安克 充电宝 20000毫安 快充", price_raw="199元"),
        deal("1002", "小米 手环 8 NFC版", price_raw="249元"),
        deal("1003", "罗技 鼠标 MX Master 3S", price_raw="699元"),
    ]

    first = ingest.ingest_deals(db, deals)
    assert (first.products_new, first.products_merged, first.products_refreshed) == (3, 0, 0)
    assert first.prices_new == 3
    db.commit()

    second = ingest.ingest_deals(db, deals)

    assert second.fetched == 3
    assert second.products_new == 0, "第二轮把已存在的商品又算成了新商品"
    assert second.products_merged == 0, "第二轮把爆料的重新见到算成了归并"
    assert second.products_refreshed == 3
    assert second.prices_new == 0, "第二轮不该产生新价格点"

    # 数据层面：一行没多。
    assert count(db, Product) == 3
    assert count(db, PriceHistory) == 3


def test_second_round_does_not_resurrect_resolved_mark(db: Session):
    """★ 人工核对过的灰色地带标记，重新采集不能把它写回去。

    suspected_duplicate_of 是给【人工】用的待办标记：人去看过之后会把它
    清成 NULL（"我看过了，不是重复"）。如果 upsert 的冲突更新里带上这一列，
    人的工作会被自动覆盖，清理过的待办会一次次复活。

    ★ 这个用例的写法：第二轮时这条爆料已经有主了，走的是 resolve_target
      的刷新分支，压根不会重新做归并判定——所以标记得以保留。
    """
    ingest.ingest_deals(
        db,
        [
            deal("1001", "安克 充电宝 20000毫安 快充 便携"),
            deal("1002", "小米 充电宝 20000毫安 快充 移动电源"),
        ],
    )
    db.commit()

    p = product_by_source_id(db, "1002")
    assert p.suspected_duplicate_of is not None
    p.suspected_duplicate_of = None      # 人工核对完毕：不是重复
    db.commit()

    ingest.ingest_deals(
        db,
        [
            deal("1001", "安克 充电宝 20000毫安 快充 便携"),
            deal("1002", "小米 充电宝 20000毫安 快充 移动电源"),
        ],
    )

    assert product_by_source_id(db, "1002").suspected_duplicate_of is None, \
        "重新采集把人工清掉的待办标记又写回来了"


def test_second_round_of_merged_deal_stays_merged(db: Session):
    """★ 被归并的爆料在第二轮必须走"刷新"，不能重新归并一次。

    这是 resolve_target 的形态 ② —— 它自己**没有**商品行，
    只能靠价格记录上的 source_ref 找到主人。

    不做这一步的话：第二轮它重新进归并判定 → 又命中同一个目标 →
    又报一次"归并"，界面上每采一次就显示"归并 1"，实际什么都没发生。
    """
    deals = [
        deal("1001", "安克 充电宝 20000毫安 快充", price_raw="199元"),
        deal("1002", "安克 充电宝 20000毫安", price_raw="189元",
             recorded_at=BASE_TIME - timedelta(days=1)),
    ]

    first = ingest.ingest_deals(db, deals)
    assert first.products_merged == 1
    db.commit()

    # 1002 在 products 表里不存在（它被归并进了 1001 的商品）
    assert db.execute(
        select(Product).where(Product.source_id == "1002")
    ).scalar_one_or_none() is None

    second = ingest.ingest_deals(db, deals)

    assert second.products_merged == 0, "被归并的爆料在第二轮被重新归并了一次"
    assert second.products_refreshed == 2
    assert second.products_new == 0
    assert count(db, Product) == 1
    assert count(db, PriceHistory) == 2


def test_ingest_is_idempotent_across_many_rounds(db: Session):
    """连抓五轮，数据库内容必须和第一轮结束时完全一致。

    ★ 这个用例的价值在于它检查的是**总体形状**（有多少商品、多少价格点、
      商品价格是多少），而不是某个计数器。计数器是可以"调对"的，
      数据一致性不行。
    """
    deals = [
        deal("1001", "安克 充电宝 20000毫安 快充", price_raw="199元"),
        deal("1002", "安克 充电宝 20000毫安", price_raw="189元",
             recorded_at=BASE_TIME - timedelta(days=1)),
        deal("1003", "小米 手环 8 NFC版", price_raw=None),
    ]

    ingest.ingest_deals(db, deals)
    db.commit()
    baseline = (
        count(db, Product),
        count(db, PriceHistory),
        product_by_source_id(db, "1001").price,
    )
    assert baseline == (2, 2, pytest.approx(199.0))

    for _ in range(4):
        ingest.ingest_deals(db, deals)
        db.commit()

    assert (
        count(db, Product),
        count(db, PriceHistory),
        product_by_source_id(db, "1001").price,
    ) == baseline


# ===========================================================================
# ★ 纵深防御：decide() 不能把候选归并到它自己
# ===========================================================================


def test_decide_never_merges_candidate_into_itself():
    """★ 池子里包含候选自己时，必须跳过它。

    主防线在 ingest（已有主的爆料压根不进比对），这里是第二道。
    decide() 是公开函数，任何调用方都不该踩这个坑。

    不跳过的后果：候选跟自己比 → 品牌相同、型号 token 相同、包含度 1.0
    → 判定 MERGE 归并到自己。数据上无害（价格没变、价格点被唯一约束挡掉），
    所以它完全静默，只是报告变成"新商品 0，归并 100"。
    """
    title = "安克 充电宝 20000毫安 快充"
    from scraper.dedup import build_signature

    norm, model_toks, toks = build_signature(
        Candidate(source="smzdm", source_id="1001", title=title)
    )

    result = decide(
        Candidate(source="smzdm", source_id="1001", title=title),
        [
            ExistingProduct(
                product_id=7,
                title_norm=norm,
                brand=None,
                model_tokens=model_toks,
                tokens=toks,
                source_id="1001",     # ← 就是候选自己
            )
        ],
    )

    assert result.decision is Decision.NEW
    assert result.target_id is None


def test_decide_still_matches_a_different_source_id():
    """★ 上一条防线不能宽到把"另一个 source_id 的同一商品"也挡掉。

    这是紧接着的一条反向断言：加了自比对豁免之后，
    真正的归并必须照样工作。少了这条，一个"return NEW"的实现
    也能让上一条测试通过。
    """
    title = "安克 充电宝 20000毫安 快充"
    from scraper.dedup import build_signature

    norm, model_toks, toks = build_signature(
        Candidate(source="smzdm", source_id="1001", title=title)
    )

    result = decide(
        Candidate(source="smzdm", source_id="1002", title="安克 充电宝 20000毫安"),
        [
            ExistingProduct(
                product_id=7,
                title_norm=norm,
                brand=None,
                model_tokens=model_toks,
                tokens=toks,
                source_id="1001",
            )
        ],
    )

    assert result.decision is Decision.MERGE
    assert result.target_id == 7


# ===========================================================================
# 品牌：源站字段优先
# ===========================================================================


def test_brand_stored_in_display_form(db: Session):
    """★ 库里存的品牌是**显示形态**（源站原文），不是比对形态。

    ★ 这条断言在实现改过一次之后才变成现在这样。
      原来它断言的是 "skechers|斯凯奇"（小写、排序、| 连接），
      因为那时 clean_deal 直接把 extract_brand 的结果落库了。

      那个做法的毛病是：`seed.py` 直写路径存的是 "Soundcore/声阔"
      （双语、保留大小写，而且 test_seed.py 有测试明确要求这个格式），
      于是 `products.brand` 一列里出现了**两种格式**，
      取决于数据是从哪个入口进来的。

      修法是把"存什么"和"比什么"分开，和 token 的处理方式一致：
        库里存显示形态（这个测试守的） / 比对时现算规范形态（下一个测试守的）
    """
    ingest.ingest_deals(
        db, [deal("1001", "适配华为 数据线", brand="SKECHERS/斯凯奇")]
    )

    assert product_by_source_id(db, "1001").brand == "SKECHERS/斯凯奇"


def test_brand_from_source_field_not_guessed_from_title(db: Session):
    """品牌取源站字段，**不从标题猜**。

    ★ 这条在真实数据上有实际意义：SMZDM 的 onclick 里有
      'brand':'SKECHERS/斯凯奇'，是权威值。
      而标题里可能出现"适配华为"这类词——从这个标题猜，
      会把一根斯凯奇的配件当成华为的商品。

      所以断言是"品牌字段给了什么就是什么"，哪怕它和标题看起来无关。
    """
    ingest.ingest_deals(
        db, [deal("1001", "适配华为 数据线", brand="SKECHERS/斯凯奇")]
    )

    assert product_by_source_id(db, "1001").brand == "SKECHERS/斯凯奇"


def test_display_and_canonical_brand_agree_across_entry_paths(db: Session):
    """★ 两种品牌形态必须能正确比对——这是双形态拆分的**目的**。

    场景：库里已经有一个由 seed.py 直写的商品，品牌存的是显示形态
    "Soundcore/声阔"。现在采集管道送来同一品牌的爆料，
    `brand_field` 是源站原文 "Soundcore/声阔"。

    两边形态不同（一个来自库、一个来自源站），但如果比对时不统一，
    硬门槛会把它们判成不同品牌 → 永远不归并 → **而且完全静默**
    （归并只是"没发生"，不报错）。

    ★ 这个测试是"跨入口归并"的守卫。少了它，
      品牌格式一旦再次分叉，没有任何东西会报警。
    """
    from scraper.store import ProductRow, upsert_products

    # 模拟 seed.py 直写的行：品牌是双语显示形态
    pid = upsert_products(
        db,
        [ProductRow(
            source="mock",
            source_id="mock-00001",
            title="声阔 Liberty 4 NC 主动降噪 真无线蓝牙耳机",
            title_norm="声阔 liberty 4 nc 主动降噪 真无线蓝牙耳机",
            brand="Soundcore/声阔",          # ← 显示形态
            price=369.0,
        )],
    )[("mock", "mock-00001")]
    db.flush()

    # 采集管道送来同一商品（少一个词，应当归并）
    report = ingest.ingest_deals(
        db,
        [deal("smzdm-1", "声阔 Liberty 4 NC 主动降噪 蓝牙耳机",
              brand="Soundcore/声阔", source="mock", price_raw="349元")],
    )

    assert report.products_merged == 1, \
        "显示形态与规范形态没统一，硬门槛把同一品牌判成了不同品牌"
    assert report.products_new == 0

    # 归并目标就是 seed 直写的那一行
    assert db.execute(
        select(PriceHistory.product_id).where(PriceHistory.source_ref == "smzdm-1")
    ).scalar_one() == pid


def test_no_brand_field_leaves_brand_none(db: Session):
    """没有品牌字段就是 None——**不从标题里猜**。

    计划里明确否决了"从标题猜品牌"这条路：猜错了会同时污染硬门槛
    和品牌分析，而且测试用例根本构造不出来。
    """
    ingest.ingest_deals(db, [deal("1001", "安克 充电宝 20000毫安")])

    assert product_by_source_id(db, "1001").brand is None


# ===========================================================================
# ★ 真实数据：离线重放两个真实页面
# ===========================================================================


@pytest.fixture(scope="module")
def real_deals() -> list[RawDeal]:
    """从真站点抓下来的 5 个精选页解析出的 100 条真实爆料。

    ★ 为什么 fixture 是**真实 HTML** 而不是"解析结果的 JSON"：
      JSON 是派生物。解析器一改，JSON 就悄悄过期了，
      而测试会继续对着旧解析结果变绿——它测的已经不是现在这份代码。

      真实 HTML 没这个问题：解析器和页面是一对，页面不变、
      解析器改了，测试立刻反映出来。
      代价是仓库里多了 1.7MB 的 HTML，值。

    ★ scope="module"：解析 1.7MB HTML 值得做一次，不是每个用例一次。
      而且这是**纯函数**（读文件 + 正则），没有共享状态的问题。
    """
    paths = sorted(FIXTURES.glob("smzdm_jingxuan_page*.html"))
    assert len(paths) == 5, f"fixture 页数变了：{len(paths)}"
    _layer, deals, stats = SmzdmSource.parse_files(paths)
    assert stats.pages_ok == 5
    assert stats.pages_blocked == 0 and stats.pages_ugc == 0
    return deals


def test_real_pages_parse_to_the_measured_count(real_deals: list[RawDeal]):
    """5 个真实页面 → 100 条爆料，且 100 条互不重复。

    实测：每页 20 条卡片，5 页 = 100 条。spike 阶段还抓了
    jingxuan.html 和 jingxuan_p1.html，两者内容完全相同（同一页的两次抓取），
    所以 6 个文件其实只有 5 个唯一页面——这里只放唯一的那些。

    ★ 写死数字是有意的：解析器和 fixture 是一对。数字变了说明
      要么 fixture 被换了，要么解析器改了。两种都该被看见。
    """
    assert len(real_deals) == 100
    assert len({d.source_id for d in real_deals}) == 100, "解析出了重复的 articleid"


def test_real_data_ingests_cleanly(db: Session, real_deals: list[RawDeal]):
    """真实数据完整入库，且报告的数字和实测一致。

    实测结论（见 docs/可行性评估.md 第十六节 16.4）：
      100 条 → 0 归并 / 6 灰色地带 / 94 新建 = 100 个商品

    6 对灰色地带全是斯凯奇 5 折鞋（跑鞋/休闲皮鞋/摩卡熊/厚底增高/健步鞋
    vs "女士轻质老爹鞋"）——**全部是真阴性，设计正确地拒绝合并**。
    0 条归并则是因为归并的用武之地是"同一商品跨天重复爆料"，
    而我只抓到了一次快照。
    """
    report = ingest.ingest_deals(db, real_deals)

    assert report.fetched == 100
    assert report.products_new == 94
    assert report.products_marked == 6
    assert report.products_merged == 0
    assert report.products_refreshed == 0
    assert count(db, Product) == 100


def test_real_data_grey_zone_composition(db: Session, real_deals):
    """★ 灰色地带的真实构成：5 对斯凯奇鞋 + 1 对米良品，**没有一对被误合并**。

    ┌──────────────────────────────────────────────────────────────────────┐
    │ 候选（新）                          目标（旧）              包含度    │
    ├──────────────────────────────────────────────────────────────────────┤
    │ 斯凯奇 5折 舒适缓震 女士跑鞋        斯凯奇 5折 女士轻质老爹鞋   0.667 │
    │ 斯凯奇 5折 男鞋秋季休闲皮鞋         同上                        0.667 │
    │ 斯凯奇 5折 女鞋摩卡熊 复古老爹鞋     同上                        0.667 │
    │ 斯凯奇 5折 男厚底增高缓震老爹鞋      同上                        0.667 │
    │ 斯凯奇 5折 Slip-ins闪穿 男士健步鞋   同上                        0.667 │
    │ 米良品 创意电动发光自吸掏耳神器      米良品 家用便携可折叠购物车  0.500 │
    └──────────────────────────────────────────────────────────────────────┘

    前 5 对是**真阴性**：同一品牌同一品类下不同款式的鞋，
    token 高度重叠但确实是不同商品，归并逻辑正确地拒绝了合并。
    它们也是自动合并阈值 0.75 之下最高的分数（0.667），
    和 0.75 之间留有余量——这正是不误合并的原因。

    第 6 对则是**灰区的一次假阳性**，而且原因是可解释的，
    见下面 test_whitespace_tokenization_degrades_containment 的说明。
    这一对的代价是"多让人看一眼"，不是"污染价格序列"，
    所以阈值保持在 0.75 是对的——**为了排掉这一个而调阈值，
    就是拿单个数据点去凑参数**。

    ★ 断言写成"5 + 1"而不是"6 对全是斯凯奇"：后者是我记忆里的版本，
      跑出来才发现是错的。测试的价值就在于它记的是跑出来的事实，
      不是我以为的事实。
    """
    ingest.ingest_deals(db, real_deals)

    marked = db.execute(
        select(Product.title, Product.suspected_duplicate_of).where(
            Product.suspected_duplicate_of.is_not(None)
        )
    ).all()
    assert len(marked) == 6

    titles = sorted(t for t, _ in marked)
    assert sum("斯凯奇" in t for t in titles) == 5
    assert sum("米良品" in t for t in titles) == 1

    # 每一对里的"目标"必须真的存在（自引用外键不能指向空）。
    targets = {t for _, t in marked}
    existing = set(db.execute(select(Product.id)).scalars().all())
    assert targets <= existing


def test_whitespace_tokenization_degrades_containment(db: Session, real_deals):
    """★ 上面那个灰区假阳性的根因 —— 记下来，因为它是个真实的方法论局限。

    tokenize() 只按空格切（这是有意的，见 dedup.py：不引 jieba 是为了
    结果可复现）。后果是**中文标题里没有空格时，整个句子变成一个 token**：

        "米良品 创意电动发光自吸掏耳神器"
        → {米良品, 创意电动发光自吸掏耳神器}      只有 2 个 token

        "米良品 家用便携可折叠拖轮购物车"
        → {米良品, 家用便携可折叠拖轮购物车}      也只有 2 个 token

    两者交集只有 {米良品} 一个词 → 包含度 = 1/2 = 0.500，
    正好等于灰区下界，于是这一对被标记成"疑似重复"。

    **也就是说：在无空格的中文标题上，这个分数退化成了
      "两个标题有没有共用品牌词"**，而不是真正的语义重叠度。

    而斯凯奇那 5 对拿到 0.667 而不是更高，也有一半是运气——
    它们的标题里**恰好**有空格，所以被切成了更多 token。

    ★ 为什么这条不构成"把阈值调到 0.51"的理由：
      · 自动合并线在 0.75，真实数据里最高的对是 0.667，安全余量真实存在
      · 灰区假阳性的代价是"多让人看一眼"，
        而调阈值去排掉它，会让**下一个**落在 0.49x 的真阳性被漏掉
      · 用单个数据点定阈值，正是 dedup.py 开头批评的那种拍脑袋做法

      真正该修的是分词（用 jieba 或加一层中文短语切分），
      但那会把"结果可复现"这条性质换掉。这是个明确的取舍，
      不是疏忽——所以记在这里，而不是偷偷绕过去。
    """
    ingest.ingest_deals(db, real_deals)

    ear = db.execute(
        select(Product).where(Product.title.contains("掏耳"))
    ).scalar_one()
    cart = db.execute(
        select(Product).where(Product.title.contains("购物车"))
    ).scalar_one()

    assert ear.suspected_duplicate_of == cart.id

    # 根因：无空格的中文标题整句变成一个 token，所以 token 总数只有 2。
    _, _, ear_tokens = dedup.build_signature(
        dedup.Candidate(source="smzdm", source_id=ear.source_id, title=ear.title)
    )
    assert len(ear_tokens) == 2, f"分词行为变了：{sorted(ear_tokens)}"
    assert dedup.containment(ear_tokens, ear_tokens) == 1.0  # 自比当然满分
    # 两个毫不相干的商品，得分却正好压在灰区下界上。
    assert dedup.GREY_ZONE_THRESHOLD == 0.50


def test_real_data_price_cleaning_matches_measurement(db: Session, real_deals):
    """★ 价格点数量 = 98，不是 100。

    实测：100 条真实爆料里 2 条卡片上没写价格（price_raw is None）。
    price_history 的 price 是 NOT NULL，这 2 条不能产生价格点。

    ★ 这个断言故意写成 98 而不是"len(real_deals)"——它把
      "真实数据里有 2 条没有价格"这个事实固化在测试里。
      哪天这个数字变了，说明解析器或页面变了，而不是"测试太脆"。
    """
    ingest.ingest_deals(db, real_deals)

    assert count(db, PriceHistory) == 98
    assert count(db, Product) == 100

    # 那 2 条没价格的商品确实在库里，只是价格为 NULL。
    no_price = db.execute(
        select(func.count()).select_from(Product).where(Product.price.is_(None))
    ).scalar_one()
    assert int(no_price) == 2


def test_real_data_second_round_is_pure_refresh(db: Session, real_deals: list[RawDeal]):
    """★ 真实数据的连抓两轮——阶段 4 的验收点，在真实数据上跑一遍。

    第一轮：94 新建 / 6 待核对 / 0 刷新 / 98 价格点
    第二轮：0 新建 / 0 归并 / 100 刷新 / 0 价格点 —— 一行数据都不多

    ★ 这是整份测试里最接近"面试现场演示"的一个：
      拿真页面跑、连跑两轮、报告读起来就是增量采集在工作。
    """
    ingest.ingest_deals(db, real_deals)
    db.commit()

    second = ingest.ingest_deals(db, real_deals)
    db.commit()

    assert second.products_new == 0
    assert second.products_merged == 0
    assert second.products_marked == 0
    assert second.products_refreshed == 100
    assert second.prices_new == 0

    assert count(db, Product) == 100
    assert count(db, PriceHistory) == 98


def test_real_data_field_completeness(db: Session, real_deals: list[RawDeal]):
    """字段完整度与 spike 实测一致。

    实测（100 条真实爆料）：
      title / brand / platform / category / channel / url  100%
      price_raw  98%   ← 2 条卡片上没写价格
      image_url  97%   ← 3 条卡片上没有图

    ★ 完整度**不是 100% 才是真实数据的特征**。
      如果哪天这些数字全变成 100%，更可能的解释是 fixture 被换成了
      手工编的"完美"页面，而不是源站突然补齐了字段。
    """
    ingest.ingest_deals(db, real_deals)
    total = count(db, Product)
    assert total == 100

    for col, expected in (
        (Product.brand, 100),
        (Product.platform, 100),
        (Product.category, 100),
        (Product.url, 100),
        (Product.image_url, 97),
    ):
        got = int(
            db.execute(
                select(func.count()).select_from(Product).where(col.is_not(None))
            ).scalar_one()
        )
        assert got == expected, f"{col} 完整度变了：{got} != {expected}（共 {total} 条）"

    # price 是 98 而不是 100：那 2 条没价格的爆料商品照建、价格点为 NULL。
    priced = int(
        db.execute(
            select(func.count()).select_from(Product).where(Product.price.is_not(None))
        ).scalar_one()
    )
    assert priced == 98


def test_real_data_urls_point_at_smzdm(db: Session, real_deals: list[RawDeal]):
    """真实数据的 url 必须指向 smzdm.com 的文章页。

    ★ 这条防的是"fixture 被换成造出来的假页面"——
      如果哪天有人为了修测试把 fixture 换成手工编的 HTML，
      这里会立刻红。真实采集线的全部说服力都建立在"数据来自真站点"上。
    """
    ingest.ingest_deals(db, real_deals)

    urls = db.execute(select(Product.url)).scalars().all()
    assert all(u.startswith("https://www.smzdm.com/p/") for u in urls)

    ids = db.execute(select(Product.source_id)).scalars().all()
    assert all(i.isdigit() for i in ids), "source_id 应该是 SMZDM 的 articleid（纯数字）"