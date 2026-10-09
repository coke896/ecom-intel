"""入库管道测试 —— 归并接入、幂等、价格新旧。

scraper/ingest.py 是"归并"从纯函数库变成流水线的地方，也是阶段 4
验收点「连抓两轮，第二轮不报 IntegrityError」的现场。三组最重要的用例：
连抓两轮（第二轮必须是"刷新 N / 新增价格点 0"）、归并时不能用更早的爆料
覆盖更新的价格、没有价格的爆料不产生价格点（NOT NULL 防线）。
三个坑都不会抛异常——全都是静默的，所以只能靠测试守。
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

# 真实页面的抓取时间在 2026-09-27 晚上；围绕它取基准时刻，让"更新/更旧"一眼可读。
BASE_TIME = datetime(2026, 9, 27, 20, 0, 0)


def deal(
    source_id: str,
    title: str,
    *,
    price_raw: str | None = "199元",
    recorded_at: datetime | None = BASE_TIME,
    brand: str | None = None,
    # ★ 默认值是"源站声明的通道"：写死成 None 的话，用例实际上在依赖管道兜底——
    #   而那个兜底正是把 'youhui' 写进库里的那条路
    channel: str | None = "jingxuan",
    url: str | None = None,
    # ★ 默认 None 是有意的：真实数据里"这条爆料没有图"是常态之一。
    image_url: str | None = None,
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
        image_url=image_url,
    )


def count(db: Session, model) -> int:
    return int(db.execute(select(func.count()).select_from(model)).scalar_one())


def product_by_source_id(db: Session, source_id: str) -> Product:
    return db.execute(
        select(Product).where(Product.source == "smzdm", Product.source_id == source_id)
    ).scalar_one()


# 基本形状


def test_empty_input_returns_empty_report(db: Session):
    """空输入不该查库、不该建任何东西——空列表时 `deals[0]` 会 IndexError，提前返回是那道防线。"""
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
    # ★ sales 永远是 None（SMZDM 页面不提供销量）——防止有人顺手补一个默认值
    assert p.sales is None
    assert p.shop_id is None
    assert p.suspected_duplicate_of is None

    ph = db.execute(select(PriceHistory)).scalars().all()
    assert len(ph) == 1
    # ★ 价格点的身份是 source_ref（爆料文章 id），不是时间
    assert ph[0].source_ref == "1001"
    assert ph[0].recorded_at == BASE_TIME


def test_source_channel_comes_from_the_source_not_the_pipeline(db: Session):
    """★ 通道由**数据源**声明，公共管道不替它猜。

    真实事故：`youhui`（源站对文章的类型分类）曾被当成我方通道写进库，
    它不在声明的取值族里，100 条真实数据的坐标轴全部退化成兜底的"记录时间"。
    一条把 bug 当预期钉死的测试比没有测试更糟。
    """
    ingest.ingest_deals(db, [deal("1001", "安克 充电宝")])

    assert product_by_source_id(db, "1001").source_channel == "jingxuan"


def test_pipeline_refuses_a_deal_without_a_channel(db: Session):
    """源站没声明通道时要**响**，不能替它猜一个——兜底的值看起来正常，但 recorded_at 的语义已经错了。"""
    with pytest.raises(ValueError, match="没有声明 channel"):
        ingest.ingest_deals(db, [deal("1009", "没声明通道的爆料", channel=None)])


def test_price_is_parsed_from_raw_not_guessed(db: Session):
    """价格走 cleaner.parse_price，返回的是二元组（最低价, 最高价）。"""
    ingest.ingest_deals(db, [deal("1001", "安克 充电宝", price_raw="券后199-299元")])

    p = product_by_source_id(db, "1001")
    assert p.price == pytest.approx(199.0)
    assert p.price_max == pytest.approx(299.0)


# ★ 没有价格的爆料：商品照建，价格点不建


def test_deal_without_price_creates_product_but_no_price_point(db: Session):
    """★ price_history.price 是 NOT NULL——没有价格的爆料塞 None 会抛 IntegrityError。

    真实数据里确有这种卡片；正确语义是商品照建、不写价格点：
    "有个商品被爆料了"和"我们知道它多少钱"是两件事。
    """
    report = ingest.ingest_deals(
        db, [deal("1001", "安克 充电宝 20000毫安", price_raw=None)]
    )

    assert report.products_new == 1
    assert report.prices_new == 0
    assert product_by_source_id(db, "1001").price is None
    assert count(db, PriceHistory) == 0


def test_deal_without_timestamp_creates_product_but_no_price_point(db: Session):
    """同理，没有时间戳也无法形成价格点（recorded_at 也是 NOT NULL）——没有位置的点只会是孤儿。"""
    report = ingest.ingest_deals(
        db, [deal("1001", "安克 充电宝 20000毫安", recorded_at=None)]
    )

    assert report.products_new == 1
    assert report.prices_new == 0
    assert product_by_source_id(db, "1001").price == pytest.approx(199.0)
    assert count(db, PriceHistory) == 0


def test_mixed_batch_does_not_abort_on_the_bad_apple(db: Session):
    """★ 一条没价格的爆料不能影响同批其他爆料——"清洗不抛异常"承诺在管道层的兑现：
    坏的那条在入列之前拦住，整批就不会在写入那一刻失败。"""
    deals = [deal(f"100{i}", f"商品{i} 20000毫安", price_raw=None if i == 5 else "199元")
             for i in range(10)]
    report = ingest.ingest_deals(db, deals)

    assert report.products_new == 10
    assert report.prices_new == 9
    assert count(db, Product) == 10
    assert count(db, PriceHistory) == 9


# ★ 归并


def test_same_product_second_deal_merges(db: Session):
    """两条爆料指向同一商品 → 一个商品 + 两个价格点（型号 token 相等、包含度 1.0 ≥ 0.75 → 归并）。"""
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

    # 两条爆料各贡献一个价格点，挂在同一个 product 上
    ph = db.execute(select(PriceHistory).order_by(PriceHistory.recorded_at)).scalars().all()
    assert [p.source_ref for p in ph] == ["1002", "1001"]
    assert {p.product_id for p in ph} == {product_by_source_id(db, "1001").id}


def test_batch_internal_merge_keeps_the_newest_price(db: Session):
    """★ 归并时不能用更早的爆料覆盖更新的价格。

    feed 是时间倒序（最上面最新），所以先处理最新那条：
    1001 最新（199 元），1002 是前一天的（189 元），归并后商品价必须是 199。
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
    """★ MERGE 分支里那个 UPDATE 不能省——只写 price_history 的话列表页显示旧价，和趋势图对不上。

    构造：先建旧商品（100），再归并一条**更新的**爆料（88）；不 UPDATE 则 price 停在 100。
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
    """★ 同批新建的商品也必须进比对池——否则同一批里"同一个商品的两条爆料"会建成两个商品
    （真实场景：精选页第 1 页和第 2 页有重叠）。"""
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
    """型号 token 不同 → 硬门槛一票否决（根本不进相似度计算），绝不自动合并。

    iPhone 15 128G vs 256G 是两个 SKU，价格也不同。断言是"1 新建 + 1 待核对"
    而非两个 NEW：decide() 的 conflict 分支对"文本极像但硬门槛拦下"的对
    不合并（保守）但标记（否则最该被看见的情况会被静默丢弃）。
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

    最危险的一类对（'iPhone 18 Pro' vs 'iPhone Duo'，包含度 0.800；'FreeClip 2' vs
    'FreeClip'）：任何单一阈值都收不下真阳性、排不掉这些假阳性，硬门槛让它们到不了相似度那层。
    断言两条独立性质——不合并（价格序列不被污染）**且**被标记（不静默丢弃）——
    只断言前者的话，"遇到冲突就 return NEW"的实现也能通过。
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
    """near_miss 分支：型号 token 相同、包含度落在 [0.50, 0.75) → 标记载（5 token 交集 3 → 0.6）。

    ★ 下界取 0.50 而不是更低：低于它的标题大概率就是不同商品，标成"疑似重复"会让
    suspected_duplicate_of 充满假阳性、"待人工核对"永远核对不完；只有中间这档值得花人眼。
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
    """包含度 0.4（5 token 交集 2）低于灰区下界 → 普通新商品，**不打标记**。

    上一条的反向断言：少了它，"全都标记成疑似重复"的实现也能让灰区用例通过。
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


# ★ 连抓两轮（阶段 4 的验收点）


def test_second_round_reports_refresh_not_new(db: Session):
    """★ 连抓两轮：第二轮不报 IntegrityError，也不该报"新商品 100"。

    正确输出是「新商品 0，归并 0，刷新 N，新增价格点 0」。
    守的是报告的**真实性**不是数据正确性：数据两轮都对（upsert 更新同一行、价格点被唯一约束挡掉），
    所以假数字的 bug 完全静默——而"连抓两轮看报告"恰恰是验收的方式。
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

    assert count(db, Product) == 3
    assert count(db, PriceHistory) == 3


def test_second_round_does_not_resurrect_resolved_mark(db: Session):
    """★ 人工核对过的灰色地带标记，重新采集不能把它写回去。

    suspected_duplicate_of 是给【人工】的待办标记：人清成 NULL 后，若 upsert 的冲突更新带上这一列，
    人的工作会被自动覆盖、清理过的待办一次次复活。第二轮这条爆料已有主，走的是
    resolve_target 的刷新分支、压根不重新做归并判定——标记得以保留。
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
    p.suspected_duplicate_of = None  # 人工核对完毕：不是重复
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

    resolve_target 的形态 ②：它自己**没有**商品行，只能靠价格记录上的 source_ref 找到主人。
    不做这一步的话第二轮会重新归并 → 又报"归并"，界面每采一次都显示"归并 1"，实际什么都没发生。
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

    ★ 检查的是**总体形状**（商品数、价格点数、商品价格）而不是某个计数器——
    计数器可以"调对"，数据一致性不行。
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


# ★ 不能拿"不知道"去覆盖"已知"

IMG = "https://y.zdmimg.com/20260927/abc123.jpg_a320.jpg"


def test_a_deal_without_an_image_does_not_erase_the_existing_one(db: Session):
    """更新的那条爆料没带图时，不能把商品已经有的缩略图抹成 NULL。

    顺序是这条用例的全部意义：更新的那条【没图】才会走 UPDATE、才有机会抹掉已有的图；
    【有图】的话更旧的那条被"不用更旧的覆盖更新的"挡在门外、根本走不到 UPDATE。
    所以必须按"先有图、后无图（且更新）"构造。无图不是异常输入——实测 100 条真实爆料有 3 条没有 <img>。

    ★ 判据和 sales / shop_id 不在更新列里是同一条——**不能拿"不知道"去覆盖"已知"**；
    那两个字段靠"不列进更新列"，url / image_url 要跟着最新爆料走，只能靠"值非空才写"。
    """
    # 先来一条更旧的、**有图**的爆料，把商品的图立起来
    ingest.ingest_deals(
        db,
        [
            deal(
                "C1",
                "罗技 MX Master 3S 无线鼠标",
                price_raw="599元",
                recorded_at=BASE_TIME - timedelta(days=2),
                brand="罗技",
                image_url=IMG,
            )
        ],
    )
    db.commit()
    assert product_by_source_id(db, "C1").image_url == IMG, "前置条件没立起来"

    # ★ 比上一条新 → 会真的走 UPDATE；这条没带图
    ingest.ingest_deals(
        db,
        [
            deal(
                "C2",
                "罗技 MX Master 3S 无线鼠标",
                price_raw="549元",
                recorded_at=BASE_TIME,
                brand="罗技",
                image_url=None,
            )
        ],
    )
    db.commit()

    p = product_by_source_id(db, "C1")
    assert p.price == pytest.approx(549.0)  # 价格该更新（这就是归并的意义）
    assert p.image_url == IMG, "没有图的爆料把商品已有的缩略图抹掉了"


# ★ 分批次：worker 不是一次把 100 条交给 ingest_deals 的


def test_newest_price_survives_across_batches(db: Session):
    """同一个商品的两条爆料落在**不同批次**时，留下的必须是更新的那条的价格。

    ★ 实测复现过的 bug：worker 每 PROGRESS_EVERY（20）条调一次 ingest_deals，
    而 newest_seen 当初每次调用新建——第 2 批看不到第 1 批记下的时间。于是 500 元的爆料
    在第 1 批、100 元的在第 26 位（第 2 批）→ product.price = 100，而 price_history
    最新的点还是 500：列表页与趋势图并排才矛盾。feed 是时间倒序的，越靠后的批次越旧，
    触发方向正好是最坏的。

    ★ 断言不看中间计数，只看最终落库的价格和价格点——计数器可以"调对"，数据一致性不行。
    """
    deals_with_padding = [
        # A1：最新的那条，落在第 1 批
        deal("A1", "安克 充电宝 20000毫安 快充", price_raw="500元",
             recorded_at=BASE_TIME, brand="安克"),
        # 中间 24 条无关爆料（品牌互不相同 → 各自新建）：只为把 A2 挤出第 1 批
        *[
            deal(f"F{i:02d}", f"填充商品 {i} 型号{i}", price_raw=f"{10 + i}元",
                 recorded_at=BASE_TIME - timedelta(days=1), brand=f"牌子{i}")
            for i in range(24)
        ],
        # A2：更旧的那条，落在第 2 批
        deal("A2", "安克 充电宝 20000毫安 快充", price_raw="100元",
             recorded_at=BASE_TIME - timedelta(hours=11), brand="安克"),
    ]
    assert len(deals_with_padding) == 26

    # ★ 完全照 worker 的循环分批来——一次全交的话 newest_seen 还在内存里，就绕过了要测的场景
    for start in range(0, len(deals_with_padding), 20):
        ingest.ingest_deals(db, deals_with_padding[start : start + 20])
        db.commit()

    p = product_by_source_id(db, "A1")
    assert p.price == pytest.approx(500.0), "被更旧的爆料覆盖了"
    assert count(db, Product) == 25  # 26 条爆料里有 2 条是同一个商品
    # ★ 价格点是 **26** 个而不是 25：A2 没能改商品价格，但它的价格点依然是真实数据，必须留下。
    #   "不覆盖价格"和"不记录这个点"是两件事——写成 25 会把"价格历史永久少一个点"锁成预期行为。
    assert count(db, PriceHistory) == 26


def test_older_deal_still_contributes_its_price_point(db: Session):
    """更旧的那条爆料不更新商品价格，但**它的价格点必须保留**。

    ★ 两件事独立、很容易被一起砍掉：商品行的 price 是"当前价"（取最新），
    price_history 是"发生过什么"（每条都是真实数据）。因为后者不该被覆盖而顺手丢掉前者，
    价格序列就永久少一个点——而且丢失是静默的，只能靠人眼比对才发现。
    """
    newest = deal("B1", "小米 手环 8 NFC版", price_raw="299元",
                  recorded_at=BASE_TIME, brand="小米")
    older = deal("B2", "小米 手环 8 NFC版", price_raw="249元",
                 recorded_at=BASE_TIME - timedelta(days=3), brand="小米")

    ingest.ingest_deals(db, [newest])
    db.commit()
    ingest.ingest_deals(db, [older])  # 第 2 次调用 = 另一个批次
    db.commit()

    p = product_by_source_id(db, "B1")
    assert p.price == pytest.approx(299.0), "商品价格被更旧的爆料改旧了"

    points = db.execute(
        select(PriceHistory).where(PriceHistory.product_id == p.id)
    ).scalars().all()
    assert sorted(pt.price for pt in points) == [249.0, 299.0], "更旧那条的价格点丢了"


# ★ 纵深防御：decide() 不能把候选归并到它自己


def test_decide_never_merges_candidate_into_itself():
    """★ 池子里包含候选自己时，必须跳过它。主防线在 ingest（已有主的爆料压根不进比对），
    这里是第二道——decide() 是公开函数，任何调用方都不该踩这个坑。

    不跳过的后果：候选跟自己比（品牌、型号 token、包含度 1.0 全命中）→ MERGE 到自己。
    数据上无害（价格没变、价格点被唯一约束挡掉），所以完全静默，只是报告变成"新商品 0，归并 100"。
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
                source_id="1001",  # ← 就是候选自己
            )
        ],
    )

    assert result.decision is Decision.NEW
    assert result.target_id is None


def test_decide_still_matches_a_different_source_id():
    """★ 上一条防线不能宽到把"另一个 source_id 的同一商品"也挡掉。

    反向断言：加了自比对豁免之后真正的归并必须照样工作——少了这条，
    一个"return NEW"的实现也能让上一条测试通过。
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


# 品牌：源站字段优先


def test_brand_stored_in_display_form(db: Session):
    """★ 库里存的品牌是**显示形态**（源站原文），不是比对形态。

    ★ 曾经落库的是 "skechers|斯凯奇"（clean_deal 直接存 extract_brand 的结果），
    而 seed.py 直写路径存 "Soundcore/声阔"（test_seed.py 明确要求那个格式）——
    products.brand 一列出现两种格式，取决于数据从哪个入口进来。
    修法是把"存什么"和"比什么"分开（和 token 一致）：库里存显示形态（本测试守的），
    比对时现算规范形态（下一个测试守的）。
    """
    ingest.ingest_deals(
        db, [deal("1001", "适配华为 数据线", brand="SKECHERS/斯凯奇")]
    )

    assert product_by_source_id(db, "1001").brand == "SKECHERS/斯凯奇"


def test_brand_from_source_field_not_guessed_from_title(db: Session):
    """品牌取源站字段，**不从标题猜**。

    ★ SMZDM 的 onclick 里有 'brand':'SKECHERS/斯凯奇'，是权威值；标题里却可能出现
    "适配华为"这类词——从标题猜会把一根斯凯奇的配件当成华为的商品。
    断言是"品牌字段给了什么就是什么"，哪怕它和标题看起来无关。
    """
    ingest.ingest_deals(
        db, [deal("1001", "适配华为 数据线", brand="SKECHERS/斯凯奇")]
    )

    assert product_by_source_id(db, "1001").brand == "SKECHERS/斯凯奇"


def test_display_and_canonical_brand_agree_across_entry_paths(db: Session):
    """★ 两种品牌形态必须能正确比对——这是双形态拆分的**目的**。

    库里有一个 seed.py 直写的商品（显示形态 "Soundcore/声阔"），采集管道送来同一品牌。
    两边形态不同（一个来自库、一个来自源站），比对时不统一的话硬门槛会把它们判成不同品牌
    → 永远不归并，**而且完全静默**（归并只是"没发生"，不报错）。
    本测试是"跨入口归并"的守卫：少了它，品牌格式再次分叉时没有任何东西会报警。
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
            brand="Soundcore/声阔",  # ← 显示形态
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
    """没有品牌字段就是 None——**不从标题里猜**（猜错会同时污染硬门槛和品牌分析）。"""
    ingest.ingest_deals(db, [deal("1001", "安克 充电宝 20000毫安")])

    assert product_by_source_id(db, "1001").brand is None


# ★ 真实数据：离线重放真实页面


@pytest.fixture(scope="module")
def real_deals() -> list[RawDeal]:
    """从真站点抓下来的 5 个精选页解析出的 100 条真实爆料。

    ★ fixture 是**真实 HTML** 而不是"解析结果的 JSON"：JSON 是派生物，
    解析器一改它就悄悄过期，测试会继续对着旧解析结果变绿——测的已经不是现在这份代码。
    真实 HTML 没这个问题（解析器和页面是一对），代价是仓库多了 1.7MB，值。
    scope="module"：解析 1.7MB 值得做一次，而且这是纯函数（读文件 + 正则），无共享状态问题。
    """
    paths = sorted(FIXTURES.glob("smzdm_jingxuan_page*.html"))
    assert len(paths) == 5, f"fixture 页数变了：{len(paths)}"
    _layer, deals, stats = SmzdmSource.parse_files(paths)
    assert stats.pages_ok == 5
    assert stats.pages_blocked == 0 and stats.pages_ugc == 0
    return deals


def test_real_pages_parse_to_the_measured_count(real_deals: list[RawDeal]):
    """5 个真实页面 → 100 条爆料，且互不重复（每页 20 条卡片）。

    写死数字是有意的：解析器和 fixture 是一对，数字变了说明要么 fixture 被换了、
    要么解析器改了——两种都该被看见。
    """
    assert len(real_deals) == 100
    assert len({d.source_id for d in real_deals}) == 100, "解析出了重复的 articleid"


def test_every_real_deal_declares_the_smzdm_channel(real_deals: list[RawDeal]):
    """★ 100 条真实爆料都必须由**源自己**声明通道，且声明的是 'jingxuan'。

    曾经的值是 'youhui'（源站对文章的类型分类，被 onclick 参数顺手抓了进来）：
    100/100 条真实数据的 source_channel 落在声明取值族（mock / jingxuan / fenlei）之外，
    坐标轴标签查不到表，全部退化成兜底的"记录时间"。
    断言写在**真实 fixture** 上而不是自造的 RawDeal：造的那条只能证明"我填什么就是什么"，
    真实 onclick 的第 4 个参数长什么样、以后会不会变，只有真页面能回答。
    """
    channels = {d.channel for d in real_deals}
    assert channels == {"jingxuan"}, f"真实数据的通道声明变了：{channels}"
    # 把"那个错的值"也钉住
    assert "youhui" not in channels, "源站的类型分类被当成我方通道填进来了（老缺陷复发）"


def test_real_data_ingests_cleanly(db: Session, real_deals: list[RawDeal]):
    """真实数据完整入库，且报告的数字和实测一致。

    实测（docs/可行性评估.md 16.4）：100 条 → 0 归并 / 6 灰色地带 / 94 新建 = 100 个商品。
    0 归并是因为归并的用武之地是"跨天重复爆料"，而只抓了一次快照。
    """
    report = ingest.ingest_deals(db, real_deals)

    assert report.fetched == 100
    assert report.products_new == 94
    assert report.products_marked == 6
    assert report.products_merged == 0
    assert report.products_refreshed == 0
    assert count(db, Product) == 100


def test_real_data_grey_zone_composition(db: Session, real_deals):
    """★ 灰色地带的真实构成：5 对斯凯奇鞋（包含度 0.667，目标都是"女士轻质老爹鞋"）
    + 1 对米良品（0.500），**没有一对被误合并**。

    前 5 对是**真阴性**（同品牌同品类下不同款式的鞋），0.667 是 0.75 之下最高的分数、
    和阈值留有余量。第 6 对是灰区的一次假阳性、原因可解释（见
    test_whitespace_tokenization_degrades_containment）：代价是"多让人看一眼"而不是
    "污染价格序列"，所以 0.75 保持不动——**为排掉这一个而调阈值就是拿单个数据点凑参数**。
    断言写成"5 + 1"是跑出来的事实，不是记忆里的版本。
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

    # 每一对里的"目标"必须真的存在（自引用外键不能指向空）
    targets = {t for _, t in marked}
    existing = set(db.execute(select(Product.id)).scalars().all())
    assert targets <= existing


def test_whitespace_tokenization_degrades_containment(db: Session, real_deals):
    """★ 上面那个灰区假阳性的根因：tokenize() 只按空格切（不引 jieba 是为了可复现，见 dedup.py），
    于是**无空格的中文标题整句变成一个 token**——两条米良品标题各只有 {米良品, X} 2 个 token，
    交集 {米良品} → 包含度 1/2 = 0.500，正好压在灰区下界。**在无空格中文标题上，
    这个分数退化成了"两个标题有没有共用品牌词"**。斯凯奇那 5 对拿到 0.667 也有一半是运气
    （标题里恰好有空格、切出的 token 更多）。

    ★ 这不构成"把阈值调到 0.51"的理由：0.75 之下最高分是 0.667、余量真实存在；
    假阳性的代价只是"多看一眼"，调阈值会让下一个落在 0.49x 的真阳性漏掉；
    真正该修的是分词，但那会换掉"结果可复现"——这是明确的取舍，记在这里而不是偷偷绕过。
    """
    ingest.ingest_deals(db, real_deals)

    ear = db.execute(
        select(Product).where(Product.title.contains("掏耳"))
    ).scalar_one()
    cart = db.execute(
        select(Product).where(Product.title.contains("购物车"))
    ).scalar_one()

    assert ear.suspected_duplicate_of == cart.id

    # 根因：无空格的中文标题整句变成一个 token，所以 token 总数只有 2
    _, _, ear_tokens = dedup.build_signature(
        dedup.Candidate(source="smzdm", source_id=ear.source_id, title=ear.title)
    )
    assert len(ear_tokens) == 2, f"分词行为变了：{sorted(ear_tokens)}"
    assert dedup.containment(ear_tokens, ear_tokens) == 1.0  # 自比当然满分
    # 两个毫不相干的商品，得分却正好压在灰区下界上
    assert dedup.GREY_ZONE_THRESHOLD == 0.50


def test_real_data_price_cleaning_matches_measurement(db: Session, real_deals):
    """★ 价格点数量 = 98，不是 100：100 条真实爆料里 2 条卡片上没写价格（price_raw is None），
    而 price_history.price 是 NOT NULL。

    故意写 98 而不是"len(real_deals)"：把"有 2 条没有价格"这个事实固化在测试里，
    数字变了说明解析器或页面变了，而不是"测试太脆"。
    """
    ingest.ingest_deals(db, real_deals)

    assert count(db, PriceHistory) == 98
    assert count(db, Product) == 100

    # 那 2 条没价格的商品确实在库里，只是价格为 NULL
    no_price = db.execute(
        select(func.count()).select_from(Product).where(Product.price.is_(None))
    ).scalar_one()
    assert int(no_price) == 2


def test_real_data_second_round_is_pure_refresh(db: Session, real_deals: list[RawDeal]):
    """★ 真实数据的连抓两轮——阶段 4 验收点在真实数据上跑一遍。

    第一轮：94 新建 / 6 待核对 / 0 刷新 / 98 价格点；
    第二轮：0 新建 / 0 归并 / 100 刷新 / 0 价格点——一行数据都不多。
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
    """字段完整度与 spike 实测一致（100 条真实爆料）：title / brand / platform / category /
    channel / url 100%，price_raw 98%（2 条没价格），image_url 97%（3 条没有图）。

    ★ 完整度**不是 100% 才是真实数据的特征**——若哪天全变成 100%，更可能是 fixture
    被换成了手工编的"完美"页面，而不是源站突然补齐了字段。
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

    # price 是 98 而不是 100：那 2 条没价格的爆料商品照建、价格点为 NULL
    priced = int(
        db.execute(
            select(func.count()).select_from(Product).where(Product.price.is_not(None))
        ).scalar_one()
    )
    assert priced == 98


def test_real_data_urls_point_at_smzdm(db: Session, real_deals: list[RawDeal]):
    """真实数据的 url 必须指向 smzdm.com 的文章页。

    ★ 防的是"fixture 被换成造出来的假页面"：真实采集线的全部说服力都建立在"数据来自真站点"上。
    """
    ingest.ingest_deals(db, real_deals)

    urls = db.execute(select(Product.url)).scalars().all()
    assert all(u.startswith("https://www.smzdm.com/p/") for u in urls)

    ids = db.execute(select(Product.source_id)).scalars().all()
    assert all(i.isdigit() for i in ids), "source_id 应该是 SMZDM 的 articleid（纯数字）"