"""价格历史表的写入语义 —— 两个"静默丢数据"bug 的防线。

两个设计决策做错了都**不报错**，只是数据悄悄变少或变旧：

【坑 1】唯一键用 (product_id, source_ref) 而不是 (product_id, recorded_at)。
recorded_at 是**时间属性**、不该承担"身份"职责；timesort 精度只到秒，
同一商品被两篇爆料在同一秒提及时，ON CONFLICT DO NOTHING 会**静默丢掉一条真实数据**
（症状：折线图少一个点，90 个点里根本看不出来）。

【坑 2】products 的冲突动作不能也写成 DO NOTHING：last_crawled_at 会永远停在第一次采集，
"连续 N 天抓不到则判定下架"直接失效（症状：还在卖的商品被标成下架）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.db.models import PriceHistory, Product, Shop
from scraper.store import (
    PricePoint,
    ProductRow,
    ensure_shop,
    find_product_id,
    record_price,
    record_prices,
    upsert_product,
    upsert_products,
)

# 固定时间点而非 utcnow()：测试要可复现，且"同一秒"场景需要精确控制时间
T0 = datetime(2026, 9, 1, 12, 0, 0)


# 夹具


@pytest.fixture()
def shop(db) -> Shop:
    s = Shop(name="京东自营", platform="京东")
    db.add(s)
    db.commit()
    return s


def make_product(db, shop: Shop, source: str = "smzdm", source_id: str = "182950953") -> int:
    """建一个商品，返回 id。"""
    pid = upsert_product(
        db,
        ProductRow(
            source=source,
            source_id=source_id,
            source_channel="jingxuan",
            title="京觅 猕猴桃 24枚礼盒装",
            title_norm="京觅 猕猴桃 24枚 礼盒装",
            brand="京觅",
            platform="京东",
            price=17.5,
            price_max=17.5,
            shop_id=shop.id,
            last_crawled_at=T0,
        ),
    )
    db.commit()
    return pid


# 一、price_history：ON CONFLICT DO NOTHING 真生效


def test_duplicate_source_ref_does_not_raise(db, shop: Shop) -> None:
    """★ 重复插入同一个 (product_id, source_ref) 不抛 IntegrityError。

    ★ 不能只靠"没报错"判断：唯一约束压根没建（__table_args__ 写漏了）时，
    重复插入同样不报错——它只是**插进去了两条**，所以下面断言行数必须是 1。
    """
    pid = make_product(db, shop)

    first = record_price(db, pid, 17.5, T0, source_ref="182950953")
    db.commit()
    assert first is True, "第一次写入应该是新增"

    # 同一篇文章再采一次
    second = record_price(db, pid, 17.5, T0, source_ref="182950953")
    db.commit()
    assert second is False, "重复写入应该被跳过，而不是新增"

    count = db.execute(
        select(func.count()).select_from(PriceHistory).where(PriceHistory.product_id == pid)
    ).scalar_one()
    assert count == 1, f"重复采集产生了 {count} 条记录，唯一约束没生效"


def test_duplicate_source_ref_keeps_the_original_price(db, shop: Shop) -> None:
    """重复采集时保留**第一次**的价格，不被后来的值覆盖。

    ★ 同一篇文章记的是同一个事件，价格本来就该一样；真不一样说明上游解析出了问题，
    静默覆盖会掩盖 bug——保留原值 + 返回 False，调用方可以据此告警。
    """
    pid = make_product(db, shop)
    record_price(db, pid, 17.5, T0, source_ref="182950953")
    db.commit()

    record_price(db, pid, 99.9, T0, source_ref="182950953")  # 冲突，应被跳过
    db.commit()

    price = db.execute(
        select(PriceHistory.price).where(PriceHistory.product_id == pid)
    ).scalar_one()
    assert price == pytest.approx(17.5), "DO NOTHING 没有生效，原值被覆盖了"


def test_mock_rows_with_null_source_ref_coexist(db, shop: Shop) -> None:
    """★ 同一商品下多条 source_ref 为 NULL 的记录能共存——Mock 数据能存 90 天价格序列的前提。

    SQL 的 UNIQUE 把多个 NULL 视为互不相同（NULL != NULL），(product_id, NULL) 可以有任意多条。
    但这是**必须验证而不是假设**的点：如果哪天 DB 换到某个严格模式，90 天数据会变成 1 天。
    """
    pid = make_product(db, shop)

    inserted = record_prices(
        db,
        [PricePoint(product_id=pid, price=10.0 + i, recorded_at=T0 + timedelta(days=i))
         for i in range(30)],
    )
    db.commit()

    assert inserted == 30, f"只写进去 {inserted} 条，NULL source_ref 被唯一约束挡住了"

    count = db.execute(
        select(func.count()).select_from(PriceHistory).where(PriceHistory.product_id == pid)
    ).scalar_one()
    assert count == 30


def test_same_second_two_articles_both_kept(db, shop: Shop) -> None:
    """★ 回归测试：同一商品被两篇爆料在**同一秒**提及，两条都要保留。

    这正是旧的 (product_id, recorded_at) 唯一键会静默丢数据的场景，而且很真实：
    timesort 精度只到秒、精选页一次返回 20 条，同一商品被"今日必买"和"百亿补贴"
    在同一秒推出来是会发生的事。把唯一键换回 recorded_at，这个测试立刻失败（两个点变 1 个）。
    """
    pid = make_product(db, shop)

    a = record_price(db, pid, 17.5, T0, source_ref="182950953")
    b = record_price(db, pid, 16.9, T0, source_ref="182950954")  # 同一秒，另一篇文章
    db.commit()

    assert a is True and b is True, "同一秒的两条不同爆料，有一条被丢掉了"

    rows = db.execute(
        select(PriceHistory.source_ref, PriceHistory.price)
        .where(PriceHistory.product_id == pid)
        .order_by(PriceHistory.source_ref)
    ).all()
    assert len(rows) == 2
    assert rows[0] == ("182950953", pytest.approx(17.5))
    assert rows[1] == ("182950954", pytest.approx(16.9))


def test_record_price_returns_false_on_conflict_not_raises(db, shop: Shop) -> None:
    """冲突必须是"返回 False"，不是"抛异常"。

    ★ 采集管道里一条重复数据不能让整个任务挂掉，和 cleaner.py 的"永不抛异常"同一个原则：
    批量任务里单条脏数据的代价应该是一条日志，不是一次失败。
    """
    pid = make_product(db, shop)
    record_price(db, pid, 17.5, T0, source_ref="1")
    db.commit()

    try:
        result = record_price(db, pid, 17.5, T0, source_ref="1")
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"重复写入抛了异常，采集任务会被一条重复数据打断：{exc!r}")

    assert result is False


def test_different_products_can_share_a_source_ref(db, shop: Shop) -> None:
    """不同商品可以引用同一篇爆料。

    ★ 唯一键是 (product_id, source_ref) 而不是 source_ref 单独一列——
    一篇文章可能同时推荐多个商品（"这三款充电宝都值得买"），单列唯一键会让它只能挂到一个商品上。
    """
    p1 = make_product(db, shop, source_id="A")
    p2 = make_product(db, shop, source_id="B")

    assert record_price(db, p1, 10.0, T0, source_ref="999") is True
    assert record_price(db, p2, 20.0, T0, source_ref="999") is True
    db.commit()

    count = db.execute(
        select(func.count()).select_from(PriceHistory).where(PriceHistory.source_ref == "999")
    ).scalar_one()
    assert count == 2


# 二、products：ON CONFLICT DO UPDATE 真生效


def test_upsert_updates_last_crawled_at(db, shop: Shop) -> None:
    """★ 重新采集同一商品必须刷新 last_crawled_at——products 用 DO UPDATE 而不是 DO NOTHING
    的**唯一理由**，也是"连续 N 天抓不到则判定下架"的基础。改成 DO NOTHING，这个测试会失败。
    """
    pid = make_product(db, shop)
    first = db.get(Product, pid).last_crawled_at

    later = T0 + timedelta(days=7)
    upsert_product(
        db,
        ProductRow(
            source="smzdm",
            source_id="182950953",
            title="京觅 猕猴桃 24枚礼盒装",
            title_norm="京觅 猕猴桃 24枚 礼盒装",
            price=15.9,  # 价格降了
            price_max=17.5,
            shop_id=shop.id,
            last_crawled_at=later,  # ★ 新的采集时间
        ),
    )
    db.commit()

    product = db.get(Product, pid)
    assert product is not None
    assert product.last_crawled_at == later, (
        "last_crawled_at 没有刷新——这会让'连续 N 天抓不到则下架'永久失效"
    )
    assert product.price == pytest.approx(15.9), "价格没有刷新，列表页会显示旧价"


def test_upsert_does_not_create_duplicate_rows(db, shop: Shop) -> None:
    """upsert 二次写入必须更新原行，不能插入新行。

    ★ 验证唯一约束 (source, source_id) 真的存在：约束缺失时 upsert 不报错，
    只会老实插入第二行，统计接口的商品数**悄悄翻倍**。
    """
    pid1 = make_product(db, shop)
    pid2 = make_product(db, shop)  # 同一个 source + source_id

    assert pid1 == pid2, "同一商品 upsert 两次得到了不同的 id"

    count = db.execute(
        select(func.count()).select_from(Product).where(Product.source == "smzdm")
    ).scalar_one()
    assert count == 1, f"同一商品被插成了 {count} 行"


def test_upsert_does_not_overwrite_identity_fields(db, shop: Shop) -> None:
    """归并判断的输入（title / brand）不能被 upsert 悄悄改掉。

    ★ _UPSERT_UPDATE_COLS 里刻意不含 title / brand / title_norm：它们是归并决策的依据，
    自动覆盖会让之前做过的归并决定失去依据且无记录，标题变化应该走人工确认。
    """
    pid = make_product(db, shop)
    original_title = db.get(Product, pid).title

    upsert_product(
        db,
        ProductRow(
            source="smzdm",
            source_id="182950953",
            title="完全不一样的标题",  # 源站改标题了
            brand="别的品牌",
            price=15.9,
            last_crawled_at=T0 + timedelta(days=1),
        ),
    )
    db.commit()

    product = db.get(Product, pid)
    assert product.title == original_title, "title 被 upsert 覆盖了，归并依据被悄悄改掉"
    assert product.brand == "京觅", "brand 被 upsert 覆盖了，硬门槛的数据基础被动摇"


def test_upsert_distinguishes_source(db, shop: Shop) -> None:
    """唯一键是 (source, source_id) 而不是 source_id 单独一列。

    ★ Mock 与真实数据共存的关键：同一个 id 出现在两个源里必须是两条独立记录，
    否则"重新 seed"会覆盖掉真实数据。
    """
    a = make_product(db, shop, source="smzdm", source_id="X1")
    b = make_product(db, shop, source="mock", source_id="X1")
    assert a != b

    assert find_product_id(db, "smzdm", "X1") == a
    assert find_product_id(db, "mock", "X1") == b
    assert find_product_id(db, "smzdm", "不存在") is None


def test_upsert_products_batch_returns_id_map(db, shop: Shop) -> None:
    """批量 upsert 返回 (source, source_id) → id 的映射。

    ★ 采集器拿到 20 条爆料后要立刻知道每条对应哪个 product_id 才能写价格历史，
    所以映射是必须的返回值，不能只返回"成功了几条"。
    """
    rows = [
        ProductRow(
            source="smzdm", source_id=f"id{i}", title=f"商品 {i}",
            title_norm=f"商品 {i}", price=10.0 * i, last_crawled_at=T0,
        )
        for i in range(1, 6)
    ]
    mapping = upsert_products(db, rows)
    db.commit()

    assert len(mapping) == 5
    for i in range(1, 6):
        assert ("smzdm", f"id{i}") in mapping
        assert find_product_id(db, "smzdm", f"id{i}") == mapping[("smzdm", f"id{i}")]


# 三、两张表的语义对照（把"写反了"这件事直接测出来）


def test_the_two_tables_have_opposite_conflict_semantics(db, shop: Shop) -> None:
    """★ 同一个"重复写入"动作，在两张表上必须得到**相反**的结果：
    products → 行被更新（last_crawled_at 刷新）；price_history → 被跳过（不新增行）。

    单独看任何一张表都测不出"写反了"，必须对照着看——两张表都改成 DO NOTHING
    （最常见的省事写法）时，这个测试会因为 products 那边没有变化而失败。
    """
    pid = make_product(db, shop)
    record_price(db, pid, 17.5, T0, source_ref="A")
    db.commit()

    before_crawled = db.get(Product, pid).last_crawled_at
    before_points = db.execute(
        select(func.count()).select_from(PriceHistory).where(PriceHistory.product_id == pid)
    ).scalar_one()

    # 同一篇爆料、同一价格，再采一遍
    upsert_product(
        db,
        ProductRow(
            source="smzdm", source_id="182950953",
            title="京觅 猕猴桃 24枚礼盒装", price=17.5, last_crawled_at=T0 + timedelta(hours=1),
        ),
    )
    added = record_price(db, pid, 17.5, T0, source_ref="A")
    db.commit()

    after_crawled = db.get(Product, pid).last_crawled_at
    after_points = db.execute(
        select(func.count()).select_from(PriceHistory).where(PriceHistory.product_id == pid)
    ).scalar_one()

    # products：变了（DO UPDATE）
    assert after_crawled != before_crawled, "products 的冲突动作不是 DO UPDATE"
    # price_history：没变（DO NOTHING）——不新增行
    assert added is False
    assert after_points == before_points, "price_history 的冲突动作不是 DO NOTHING"


# 四、shops：resolve-or-create（第三个写入路径）
# ★ 前两个的坑是"写反了语义"，这个的坑是"唯一约束形同虚设"——同属"不报错但数据坏掉"那一类


def test_ensure_shop_returns_same_id_on_second_call(db) -> None:
    """同一个 (平台, 店名) 查两次，拿到**同一个** id，且只建了一行。

    ★ 守的是"采集任务跑到第二页时店铺突然翻倍"：无条件 insert 会撞 (platform, name)
    唯一约束；不加约束又不查重则是一堆重复行，前端"店铺占比"饼图会被切成互不相干的几块。
    """
    first = ensure_shop(db, "京东", "京东自营")
    second = ensure_shop(db, "京东", "京东自营")

    assert first == second
    assert db.execute(
        select(func.count()).select_from(Shop).where(Shop.name == "京东自营")
    ).scalar_one() == 1


def test_ensure_shop_same_name_on_different_platforms_is_two_shops(db) -> None:
    """★ 店名相同但平台不同 = 两家店：唯一约束的键是 **(platform, name)** 而不是 name。"""
    jd = ensure_shop(db, "京东", "品牌自营")
    tm = ensure_shop(db, "天猫", "品牌自营")

    assert jd != tm


def test_ensure_shop_rejects_empty_platform(db) -> None:
    """★ 空平台必须**抛错**，而不是"安静地建一行 platform 为 NULL 的店"。

    ★ 第三个静默坑、也是最刁的：SQL 把多个 NULL 视为**互不相同**，`(NULL, "京东自营")`
    能重复插进去好几行——(platform, name) 唯一约束在这个组合上**形同虚设**。
    Mock 数据里恰好有一批店铺 platform 为 NULL（seed 模板里有不写平台的），不是理论担忧。
    """
    with pytest.raises(ValueError, match="platform"):
        ensure_shop(db, None, "京东自营")
    with pytest.raises(ValueError, match="platform"):
        ensure_shop(db, "", "京东自营")


def test_ensure_shop_gives_id_without_committing(db) -> None:
    """★ ensure_shop 只 flush，不 commit——提交时机由调用方决定。

    一批 100 条爆料要整批一个事务（中途失败整批回滚，不留"前 79 条进去了、后 21 条没进去"
    的半截状态），ensure_shop 自己 commit 会从中间切断那个事务边界。
    ★ 理想验证是新会话里查不到；本测试的 db 夹具只有一个会话，退一步断言可观测部分：
    id 自增、大于 0，说明 flush 确实拿到了主键。
    """
    sid = ensure_shop(db, "京东", "京东自营")

    assert sid > 0
    # flush 后同一会话内立刻能查到，说明真的写进了事务而不是空对象
    assert db.get(Shop, sid) is not None
