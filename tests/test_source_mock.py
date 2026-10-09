"""Mock 采集源测试。

守的是一条【性质】：Mock 源的全部价值在于能和 seed.py 的直写路径**收敛到同一个库状态**——
"同一个 (count, days, seed_value)，不管 seed 直写还是走采集管道，得到同一批商品、
同样的标题、同样的价格"。一旦不成立，`?source=mock` 下的统计会因为"数据从哪个入口进来"
而不同，而这种差异极难排查：两个入口单看都是对的。

★ 对齐只能用【默认参数】：随机数流的对齐要求走完 `build_products(rng, count, days)` 的完整循环，
传 count=20 得到的第 21 条和 seed 的第 21 条不是同一个（价格序列、销量、店铺全错位）。
"""

from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import func, select

from app.db.models import PriceHistory, Product
from scraper.cleaner import parse_price
from scraper.seed import (
    DEFAULT_DAYS,
    DEFAULT_PRODUCT_COUNT,
    DEFAULT_SEED,
    build_products,
)
from scraper.sources.mock import PER_PAGE, MockSource


@pytest.fixture(scope="module")
def seed_products() -> list[dict]:
    """seed.py 同一套参数生成的商品，用来做对齐基准。

    ★ 重跑一遍 build_products 而不是去问 seed() 要——seed() 会清库写库，
    而这里要验证的恰恰是"生成器本身的输出"。
    """
    rng = np.random.default_rng(DEFAULT_SEED)
    return build_products(rng, DEFAULT_PRODUCT_COUNT, DEFAULT_DAYS)


# 与 seed 生成器对齐


def test_first_page_matches_seed_generator_exactly(seed_products):
    """★ 前 20 条必须和 seed 生成的逐字段一致（除 recorded_at）。

    不成立的话，Mock 采集会在 `?source=mock` 下造出第二批商品，或拿错位的价格覆盖 seed 的数据。
    """
    _layer, deals, _stats = MockSource().fetch(pages=1)

    assert len(deals) == PER_PAGE == 20
    for deal, p in zip(deals, seed_products):
        assert deal.source_id == p["source_id"]
        assert deal.title == p["title"]
        assert deal.brand == p["brand"]
        assert deal.category == p["category"]
        assert deal.url == p["url"]
        assert deal.platform == p["_shop"][0]
        assert deal.shop_name == p["_shop"][1]
        assert deal.sales == p["sales"]


def test_deals_carry_sales_and_shop(seed_products):
    """★ Mock 源必须带销量和店铺——这是它和 SMZDM 源最大的不同。

    SMZDM 好价页没有"已售"/"销量"字样，所以真实数据的 sales 恒为 NULL、
    店铺占比饼图在 ?source=smzdm 下是空的；Mock 数据的价值就是把这些图撑起来。
    ★ 这两个字段是 RawDeal 上的可选字段，不是 ingest 里的分支——管道不该知道"哪个源没有销量"。
    """
    _layer, deals, _stats = MockSource().fetch(pages=2)

    assert all(d.sales is not None and d.sales >= 0 for d in deals)
    assert all(d.shop_name for d in deals)
    assert all(d.platform in ("京东", "天猫") for d in deals)


def test_price_raw_round_trips_through_cleaner(seed_products):
    """★ 价格必须是**原文**，而且洗回来要和原值完全相同——"转换刻意倒着走"那一步的验收：
    把算好的浮点价格重新写成 `"199.9元"`，让下游 cleaner.parse_price 真正跑一遍；
    直接塞浮点就绕过了清洗层，而那正是要被验证的东西。

    ★ 用 `:g` 而不是 `:%.2f`：149.90 洗回来也是 149.9，但 `:g` 不引入"两位小数"这个源站没有的约定。
    """
    _layer, deals, _stats = MockSource().fetch(pages=1)

    for deal, p in zip(deals, seed_products):
        price, price_max = parse_price(deal.price_raw)
        assert price == pytest.approx(p["price"]), \
            f"{deal.price_raw} 洗出来是 {price}，seed 写的是 {p['price']}"
        # 单值成交 → 区间上下限相等（和 seed 的语义一致）
        assert price_max == pytest.approx(p["price"])


# ===========================================================================
# 确定性
# ===========================================================================


def test_repeated_fetch_agrees_on_identity(seed_products):
    """两次 fetch 的商品身份/标题/价格完全一致。

    ★ 断言的是"前一组字段"不是整个 RawDeal——recorded_at 每次都不同（它是抓取时刻），
    写成 `fetch(...) == fetch(...)` 会因为时间戳而假红。
    """
    first = MockSource().fetch(pages=2)[1]
    second = MockSource().fetch(pages=2)[1]

    assert len(first) == len(second)
    for a, b in zip(first, second):
        assert a.source_id == b.source_id
        assert a.title == b.title
        assert a.price_raw == b.price_raw
        assert a.brand == b.brand
        assert a.sales == b.sales
        assert a.shop_name == b.shop_name


def test_repeated_fetch_uses_fresh_rng(seed_products):
    """★ 每次 fetch 都重新建 rng——用同一个 rng 连着取两次会错位（把 rng 存成实例属性
    就会这样：第二次调用拿到随机数流里靠后的位置，产出完全不同的数据）。
    第一页和第二页必须不同（rng 在前进），但两次"第一页"必须相同（每次重新开始）。
    """
    src = MockSource()
    page1 = src.fetch(pages=1)[1]
    page2 = src.fetch(pages=2)[1]

    # 第二次请求 2 页 → 前 20 条应该还是第一次那 20 条
    assert [d.source_id for d in page2[:PER_PAGE]] == [d.source_id for d in page1]
    # 而后 20 条是新内容（rng 确实在前进，不是每页都返回同一批）
    assert page2[PER_PAGE].source_id != page1[0].source_id


def test_different_seed_value_gives_different_data():
    """换个种子就该是另一批数据——少了这条，"永远返回同一份写死数据"的实现也能通过上面所有测试。"""
    a = MockSource(count=100, seed_value=1).fetch(pages=1)[1]
    b = MockSource(count=100, seed_value=2).fetch(pages=1)[1]

    assert [d.title for d in a] != [d.title for d in b]


# 分页与统计


def test_page_count_is_capped_by_dataset_size():
    """请求的页数超过数据集大小时，pages_ok 按**实际产出**算。

    ★ 否则小 count 的源"抓 100 页"会报"100 页成功"而实际只有 1 页有数据——
    这个数字显示在任务进度里，**假的进度比没有进度更糟**，它会让人以为采集在正常工作。
    """
    src = MockSource(count=30, seed_value=7)

    _layer, deals, stats = src.fetch(pages=100)

    assert len(deals) == 30                 # 数据集只有 30 条
    assert stats.pages_ok == 2              # 30 条 = 1 整页 + 1 个半页
    assert stats.pages_blocked == 0
    assert stats.pages_ugc == 0


def test_all_deals_returns_full_dataset():
    """all_deals() 把整个数据集一次交出来，页数换算向上取整。"""
    src = MockSource(count=50, seed_value=7)

    deals = src.all_deals()

    assert len(deals) == 50
    assert len({d.source_id for d in deals}) == 50


def test_zero_pages_is_rejected():
    """pages < 1 直接报错，而不是"安静地返回空列表"。

    ★ 静默返回空的后果：任务显示 success、progress 100%、消息是"没有解析到任何爆料
    （页面结构可能变了）"——而真相是调用方传错了参数，排查方向被完全带偏。
    """
    with pytest.raises(ValueError):
        MockSource().fetch(pages=0)


def test_source_is_offline_and_declares_itself():
    """Mock 源必须自称 'mock'，且 stats.layer 说明数据来自内存。

    layer 回答"数据是哪一层给的"：SMZDM 可能是 html 或 browser，这里是 memory——
    看到 memory 就知道这批数据没经过网络。
    """
    src = MockSource()

    assert src.name == "mock"
    layer, _deals, stats = src.fetch(pages=1)

    assert layer == "memory"
    assert stats.layer == "memory"


def test_source_satisfies_datasource_protocol():
    """运行时检查：MockSource 确实长得像 DataSource。

    ★ base.py 选 Protocol 而不是 ABC 的理由的实测——MockSource 没有继承任何东西，只要求"长得像"。
    """
    from scraper.base import DataSource

    assert isinstance(MockSource(), DataSource)


# ★★ 收敛：两个入口写出来的库状态必须一致


# 上面几节验的是**内存里**的一致（MockSource 吐的 RawDeal 和 seed 生成器一致）；
# 这一节验的是**库里**的一致——中间还隔着 clean_deal、归并判定、两张表的 upsert，
# 那一段才是真正会出事的地方，而它的失败方式是**静默的**（"没归并上"只是多出 100 个商品）。
# 跑完整的 seed + 采集 1000 × 90 天太慢，用小参数；count/days 必须【两边传同一组】
# （随机数流的对齐要求走完 build_products 的完整循环，见文件顶部说明）。
_PROBE_COUNT = 100
_PROBE_DAYS = 30


def _mock_params() -> dict:
    return {
        "count": _PROBE_COUNT,
        "days": _PROBE_DAYS,
        "seed_value": DEFAULT_SEED,
    }


def _products_snapshot(db) -> dict[str, tuple]:
    """把 mock 商品的"会被人看见的那几列"抓成快照，用于比对两个入口。"""
    return {
        sid: (title, brand, price, price_max, sales)
        for sid, title, brand, price, price_max, sales in db.execute(
            select(
                Product.source_id,
                Product.title,
                Product.brand,
                Product.price,
                Product.price_max,
                Product.sales,
            ).where(Product.source == "mock")
        )
    }


def test_crawl_converges_with_seed_direct_write(db):
    """★★ 全文件最重要的一条：seed 直写 → 采集 → 商品表逐字段不变。

    这是 MockSource 存在的**全部理由**的验收：不成立的话，演示时点一下"采集"
    就会在库里造出第二批商品，两次看统计图得到的数字不同——**取决于你上次点没点采集**。
    实测（count=100, days=30, seed=20260928）：报告"新商品 0，刷新 100，新增价格点 100"，
    商品表 100 行 → 100 行、**0 处差异**。

    ★ 走"刷新"分支而不是"归并"分支本身就是正确的：同一批数据里每条爆料的 source_id
    和库里商品的完全相同（都是 mock-000xx），`resolve_target` 的**形态 ①**一眼认出，
    根本不需要进相似度比对——能确定的事不靠猜。
    """
    from scraper import ingest, seed

    seed.seed(db, **_mock_params())
    before = _products_snapshot(db)
    assert len(before) == _PROBE_COUNT

    # ---- 走采集管道把同一批数据再灌一遍 ----
    _layer, deals, _stats = MockSource(**_mock_params()).fetch(pages=10_000)
    assert len(deals) == _PROBE_COUNT, "数据集没取全，后面的比对就不成立了"
    report = ingest.ingest_deals(db, deals, source="mock")
    db.commit()

    # ---- 报告：一个新品都不该有 ----
    assert report.products_new == 0, (
        f"采集管道把已有商品当成新商品了，多出 {report.products_new} 个——"
        "演示时点一次采集就会污染统计图"
    )
    assert report.products_merged == 0, "同一 source_id 不该走归并，应该走刷新"
    assert report.products_marked == 0
    assert report.products_refreshed == _PROBE_COUNT

    # ---- 商品表：一行不多、一列不差 ----
    after = _products_snapshot(db)

    assert len(after) == len(before), "商品行数变了"
    diverged = sorted(k for k in before if before[k] != after[k])
    assert not diverged, (
        f"这些商品的字段在采集后被改掉了：{diverged[:5]}\n"
        f"  例如 {diverged[0]}: {before[diverged[0]]} → {after[diverged[0]]}"
        if diverged else ""
    )


def test_crawl_observes_now_while_seed_owns_the_history(db):
    """★ 商品表收敛，但 price_history **必然不同**——而这个不同是对的。

    两边"谁有资格写历史"不一样：seed 直写有生成器的特权，一次造出 90 天完整序列
    （source_ref 为 NULL）；采集只能观测"此刻"、一次采一条——所以采集后价格点**多出 count 条**。

    ★ 若哪天断言失败，先别改测试，先想清楚是哪一边变了：变成"多出 0 条"→ 采集的价格点
    被唯一约束吃掉了（多半有人给 source_ref 填了值）；变成"多出 100 条以上"→ 归并/刷新写了两遍。
    """
    from scraper import ingest, seed

    seed.seed(db, **_mock_params())
    seeded_points = db.execute(
        select(func.count()).select_from(PriceHistory)
    ).scalar_one()
    assert seeded_points == _PROBE_COUNT * _PROBE_DAYS

    # ★ seed 的价格点 source_ref 全是 NULL，不是"忘了填"——Mock 数据没有"爆料 id"这个东西；
    #   SQLite 的 UNIQUE 把多个 NULL 视为互不相同，所以同一商品的多条历史不会被挡住
    nulls = db.execute(
        select(func.count())
        .select_from(PriceHistory)
        .where(PriceHistory.source_ref.is_(None))
    ).scalar_one()
    assert nulls == seeded_points

    # ---- 采集 ----
    _layer, deals, _stats = MockSource(**_mock_params()).fetch(pages=10_000)
    report = ingest.ingest_deals(db, deals, source="mock")
    db.commit()

    after = db.execute(select(func.count()).select_from(PriceHistory)).scalar_one()
    assert after == seeded_points + _PROBE_COUNT, (
        f"价格点从 {seeded_points} 变成 {after}，期望多出 {_PROBE_COUNT} 条（一条商品一条）"
    )
    assert report.prices_new == _PROBE_COUNT

    # ★ 采集写进去的那条带着 source_ref（= 源站 id），和 seed 的 NULL 区分开——
    #   这正是两种来源能在同一张表里共存的原因
    crawled = db.execute(
        select(PriceHistory.recorded_at, PriceHistory.price)
        .where(PriceHistory.source_ref == "mock-00000")
    ).all()
    assert len(crawled) == 1, "同一条爆料被写了两遍"

    # ★ 采集那一刻比 seed 序列的最后一天更晚（seed 结束在 start + (days-1) = now - 1 天），
    #   所以采集点确实"更新"，它会成为趋势图上最新的那个点
    last_seeded = db.execute(
        select(func.max(PriceHistory.recorded_at)).where(
            PriceHistory.source_ref.is_(None)
        )
    ).scalar_one()
    assert crawled[0].recorded_at > last_seeded

    # ★ 而且采集点的价格 = seed 写在 products.price 上的那个值；
    #   两边对不上，趋势图末端和列表页就会显示两个数
    assert crawled[0].price == pytest.approx(
        db.execute(
            select(Product.price).where(Product.source_id == "mock-00000")
        ).scalar_one()
    )
