"""告警状态机（`app/alerts_engine.py`）的测试。

================================================================================
这个文件里最要紧的是"没有发生的事"
================================================================================
状态机的三条规则里只有第一条会写数据，另外两条是**不写**：
不重复写、不发通知。

    armed + 成立   → 触发
    fired + 成立   → 跳过      ← 不写
    fired + 不成立 → 重新武装   ← 不写（也不发通知）

而"没发生的事"是最难测的。一个把 fired 判成 armed 的实现，在测试里
表现为"多写了一条记录"，在真实系统里表现为**用户被同一个事件骚扰 288 次**。
所以下面的断言几乎都是**精确计数**：

    assert count_alerts(db) == 1     ← 而不是 >= 1

`== 1` 和 `>= 1` 的区别，就是这个文件存在的全部理由。

================================================================================
★ 检查器不改价格——测试必须自己造成状态变化
================================================================================
`check_alerts()` 只读 `products.price`（和 price_history 的历史最高价）。
所以"价格涨回阈值上方"这件事在测试里必须由测试自己改库来模拟，
而不是等检查器去做。

这看着像测试在"作弊"，其实正相反：**它证明了检查器只依赖"当前价"这一个输入**。
如果哪天有人让检查器顺手去改价格，这些测试会立刻失去意义（价格会被它改回去），
那时就该有人问一句"为什么告警检查要改价格"。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.models import Alert, AlertRule
from app.alerts_engine import (
    RULE_PRICE_BELOW,
    RULE_PRICE_DROP_PCT,
    AlertReport,
    _triggered,
    check_alerts,
)
from tests.factories import (
    alert_rule_factory,
    count_alerts,
    product_factory,
    set_price,
    state_of,
)


@pytest.fixture()
def add_rule(db: Session):
    return alert_rule_factory(db)


@pytest.fixture()
def add_product(db: Session):
    return product_factory(db)


# ★ `count_alerts` / `state_of` / `set_price` 现在住在 tests/factories.py。
#   搬过去的原因是 test_api_alerts.py 也需要它们——而这两个文件各留一份的话，
#   某天有人修好了其中一份的取数方式（比如发现少了一次 expire），
#   另一份就继续按老写法"通过"，验的却明明是同一件事。
#   （这和"造商品"只能有一份实现是同一条理由，见本文件所在目录的说明。）


# ===========================================================================
# 一、三态状态机 —— 本文件的核心
# ===========================================================================


def test_armed_and_below_threshold_fires_once(db, add_product, add_rule):
    """armed + 价格低于阈值 → 触发一次，写一条记录，状态变 fired。"""
    p = add_product(sid="a-1", title="安克 充电宝", price=88.0)
    rule = add_rule(product=p, rule_type=RULE_PRICE_BELOW, threshold=100.0)

    report = check_alerts(db)

    assert report.rules_checked == 1
    assert report.fired == 1
    assert count_alerts(db) == 1
    assert state_of(db, rule) == "fired"


def test_still_below_threshold_does_not_fire_twice(db, add_product, add_rule):
    """★★ 全文件最重要的一条：价格持续低于阈值，只能有一条记录。

    没有状态机的话，这一条会失败——而且失败的方式取决于检查频率：
    5 分钟一次 → 一天 288 条。用户第一次会看，第二次会皱眉，第三次就关掉了。
    """
    p = add_product(sid="a-1", title="安克 充电宝", price=88.0)
    add_rule(product=p, rule_type=RULE_PRICE_BELOW, threshold=100.0)

    first = check_alerts(db)
    # 价格没变（下一轮采集没带来新爆料，价格还是 88）
    second = check_alerts(db)
    third = check_alerts(db)

    assert first.fired == 1
    assert (second.fired, third.fired) == (0, 0)
    # ★ 精确计数：三次检查加起来，记录必须【恰好】一条。
    assert count_alerts(db) == 1
    # 没触发的那两轮也不该被算成"判不了"——它们是正常工作的
    assert (second.undecidable, third.undecidable) == (0, 0)


def test_price_back_above_threshold_rearms_without_notifying(db, add_product, add_rule):
    """fired + 条件不成立 → 重新武装，但**不写记录、不发通知**。

    价格涨回阈值上方不是用户关心的事件。发了的话，一次价格波动
    会变成两条通知（跌破、涨回），而涨回那条没有任何可执行的信息。
    """
    p = add_product(sid="a-1", title="安克 充电宝", price=88.0)
    rule = add_rule(product=p, rule_type=RULE_PRICE_BELOW, threshold=100.0)
    check_alerts(db)
    assert count_alerts(db) == 1

    set_price(db, p, 120.0)
    report = check_alerts(db)

    assert report.rearmed == 1
    assert report.fired == 0
    assert state_of(db, rule) == "armed"
    # ★ 关键：重新武装**没有**产生第二条记录
    assert count_alerts(db) == 1


def test_rearm_then_drop_again_fires_a_second_time(db, add_product, add_rule):
    """重新武装之后再跌破 → 这是一个**新的**价格事件，要再触发一次。

    如果没有"重新武装"这条规则（比如做成"触发一次就永久熄火"），
    第二次跌破会被静默漏掉——而用户以为规则还在看着。
    """
    p = add_product(sid="a-1", title="安克 充电宝", price=88.0)
    add_rule(product=p, rule_type=RULE_PRICE_BELOW, threshold=100.0)

    assert check_alerts(db).fired == 1
    set_price(db, p, 120.0)
    assert check_alerts(db).rearmed == 1
    set_price(db, p, 79.0)
    assert check_alerts(db).fired == 1

    assert count_alerts(db) == 2


def test_armed_and_condition_false_does_nothing(db, add_product, add_rule):
    """armed + 条件不成立 → 什么也不做（不写记录，状态保持 armed）。"""
    p = add_product(sid="a-1", title="安克 充电宝", price=200.0)
    rule = add_rule(product=p, rule_type=RULE_PRICE_BELOW, threshold=100.0)

    report = check_alerts(db)

    assert (report.fired, report.rearmed, report.undecidable) == (0, 0, 0)
    assert count_alerts(db) == 0
    assert state_of(db, rule) == "armed"


def test_fired_rule_that_stays_below_keeps_its_state(db, add_product, add_rule):
    """连续多轮低于阈值，状态必须**稳定停在 fired**，不能来回抖动。"""
    p = add_product(sid="a-1", title="安克 充电宝", price=88.0)
    rule = add_rule(product=p, rule_type=RULE_PRICE_BELOW, threshold=100.0)

    for _ in range(5):
        check_alerts(db)
        assert state_of(db, rule) == "fired"

    assert count_alerts(db) == 1


# ===========================================================================
# 二、条件本身（纯函数，可以逐条测边界）
# ===========================================================================


@pytest.mark.parametrize(
    ("price", "threshold", "expected"),
    [
        (99.9, 100.0, True),
        # ★ 恰好等于阈值**不算**跌破——用的是 `<` 而不是 `<=`。
        #   这个选择要说清楚：用户写"低于 100 元提醒我"，
        #   而 100.00 元既不低于也不高于。边界值上宁可少报一次，
        #   也不要为了它去解释"为什么明明没低于也提醒了"。
        (100.0, 100.0, False),
        (100.1, 100.0, False),
    ],
)
def test_price_below_is_strict(price, threshold, expected):
    assert _triggered(RULE_PRICE_BELOW, threshold, price, None) is expected


@pytest.mark.parametrize(
    ("price", "baseline", "threshold", "expected"),
    [
        # 200 → 150 是跌 25%，恰好等于阈值 → 触发（跌幅用 >=）
        (150.0, 200.0, 25.0, True),
        (150.0, 200.0, 25.01, False),
        (100.0, 200.0, 50.0, True),
        # 从没跌过 → 0%
        (200.0, 200.0, 1.0, False),
        # ★ 涨过历史最高价（新爆料更贵）→ 跌幅是负数，不该触发。
        #   不能让"负数 >= 阈值"这种比较在某个阈值下意外成立。
        (250.0, 200.0, 0.0, False),
    ],
)
def test_drop_pct_boundaries(price, baseline, threshold, expected):
    assert _triggered(RULE_PRICE_DROP_PCT, threshold, price, baseline) is expected


@pytest.mark.parametrize("baseline", [None, 0.0, -1.0])
def test_drop_pct_without_a_usable_baseline_is_undecidable(baseline):
    """没有基准价 → None（判不了），**不是** False（检查过了、没到）。

    这两个回答在界面上长得一样（都没写 alerts），含义却相反：
    False 是"规则在工作"，None 是"规则根本没执行"。
    """
    assert _triggered(RULE_PRICE_DROP_PCT, 10.0, 150.0, baseline) is None


def test_unknown_rule_type_is_undecidable_not_false():
    """不认识的规则类型 → None，不是 False。

    返回 False 等于说"我检查过了，条件不成立"——而这是一句假话：
    代码根本不知道该怎么判它。
    """
    assert _triggered("price_goes_to_the_moon", 10.0, 150.0, 200.0) is None


# ===========================================================================
# 三、判不了的规则（"没触发"和"没工作"必须分开）
# ===========================================================================


def test_product_without_price_is_undecidable(db, add_product, add_rule):
    """商品没有当前价（真实数据里确实存在）→ 判不了，不触发。

    100 条真实爆料里有 2 条卡片上没写价格（见 RawDeal.price_raw 的说明）。
    对这两条规则，诚实的回答是"判不了"，不是"没到阈值"。
    """
    p = add_product(sid="a-1", title="某商品", price=None)
    rule = add_rule(product=p, rule_type=RULE_PRICE_BELOW, threshold=100.0)

    report = check_alerts(db)

    assert report.rules_checked == 1
    assert report.undecidable == 1
    assert count_alerts(db) == 0
    # ★ 判不了【不该】改变状态：下一轮价格来了还要能正常触发
    assert state_of(db, rule) == "armed"


def test_drop_rule_without_history_is_undecidable(db, add_product, add_rule):
    """跌幅规则但没有任何历史价（刚采到、还没积累）→ 判不了。"""
    p = add_product(sid="a-1", title="某商品", price=88.0, points=0)
    add_rule(product=p, rule_type=RULE_PRICE_DROP_PCT, threshold=10.0)

    report = check_alerts(db)

    assert report.undecidable == 1
    assert count_alerts(db) == 0
    assert any("跌幅基准" in r for r in report.reasons)


def test_unknown_rule_type_in_the_database_is_reported_not_ignored(
    db, add_product, add_rule
):
    """库里有一行规则类型不认识（手工写的 / 将来新增的类型）→ 要报出来。

    ★ 它必须是 undecidable 而不是被静默跳过：静默跳过的话，
      这条规则永远不会触发，而界面上它看起来"启用中、正在工作"。
    """
    p = add_product(sid="a-1", title="某商品", price=88.0)
    add_rule(product=p, rule_type="price_is_funny", threshold=100.0)

    report = check_alerts(db)

    assert report.undecidable == 1
    assert any("不认识" in r for r in report.reasons)
    assert count_alerts(db) == 0


def test_reasons_name_the_rule_so_it_can_be_found(db, add_product, add_rule):
    """原因里必须带规则 id 和具体商品——否则日志里只有一句"判不了"。"""
    p = add_product(sid="a-1", title="某商品", price=None)
    rule = add_rule(product=p, rule_type=RULE_PRICE_BELOW, threshold=100.0)

    report = check_alerts(db)

    assert len(report.reasons) == 1
    assert f"#{rule.id}" in report.reasons[0]
    assert f"#{p.id}" in report.reasons[0]


# ===========================================================================
# 四、哪些规则会被检查
# ===========================================================================


def test_disabled_rules_are_not_checked(db, add_product, add_rule):
    """停用的规则不参与检查，也不计入"检查了几条"。"""
    p = add_product(sid="a-1", title="安克 充电宝", price=88.0)
    add_rule(product=p, rule_type=RULE_PRICE_BELOW, threshold=100.0, enabled=False)

    report = check_alerts(db)

    assert report.rules_checked == 0
    assert count_alerts(db) == 0


def test_rules_are_independent(db, add_product, add_rule):
    """多条规则各自独立：一条触发不影响另一条的状态。"""
    cheap = add_product(sid="a-1", title="便宜的", price=50.0)
    pricey = add_product(sid="a-2", title="贵的", price=500.0)
    fired_rule = add_rule(product=cheap, rule_type=RULE_PRICE_BELOW, threshold=100.0)
    quiet_rule = add_rule(product=pricey, rule_type=RULE_PRICE_BELOW, threshold=100.0)

    report = check_alerts(db)

    assert report.rules_checked == 2
    assert report.fired == 1
    assert state_of(db, fired_rule) == "fired"
    assert state_of(db, quiet_rule) == "armed"


def test_no_rules_is_not_an_error(db):
    """一条规则都没有 → 正常返回，报告说人话。"""
    report = check_alerts(db)

    assert report.rules_checked == 0
    assert report.as_message() == "没有启用中的告警规则"


# ===========================================================================
# 五、跌幅规则的基准：必须来自 price_history，而不是 products.price_max
# ===========================================================================


def test_drop_pct_uses_history_max_not_the_deal_range(db, add_product, add_rule):
    """★★ 回归测试：跌幅基准是**历史最高价**，不是本次爆料的价格区间上限。

    这两个值在故意构造的数据里差得很远：

        products.price_max = 1000   ← 本次爆料的区间上限（"券后190-1000"）
        price_history MAX  = 200    ← 历史上真的卖到过的最高价
        当前价             = 190

    用 price_history 算：(200-190)/200 = 5%     → 不到阈值，不触发 ✅
    用 price_max 算：    (1000-190)/1000 = 81%   → 触发 ❌（而且是个假警报）

    ★ 为什么这个 bug 特别值得守：它在 Mock 数据上几乎不会暴露——
      Mock 的 price_max 和历史最高价数值接近，两种算法给出的结论常常一样。
      而在真实数据上 price_max **恒等于当前价**（每条爆料都是单值区间），
      于是"跌幅"永远是 0%、告警永远不触发，**而且不报错**。
    """
    p = add_product(sid="a-1", title="某商品", price=190.0, history=[150.0, 200.0, 180.0])
    add_rule(product=p, rule_type=RULE_PRICE_DROP_PCT, threshold=20.0)

    # 先确认这份数据确实是"两种算法结论相反"的构造
    db.execute(
        update(type(p)).where(type(p).id == p.id).values(price=190.0, price_max=1000.0)
    )
    db.commit()

    # ★ 证明这份数据**确实有区分度**：如果基准错用了 price_max，它会触发。
    #   不做这个断言的话，这份数据完全可能是"两种基准都不触发"的——
    #   那样测试照样绿，却什么也没守住。测试必须能证明自己抓得住那个 bug。
    assert _triggered(RULE_PRICE_DROP_PCT, 20.0, 190.0, 1000.0) is True

    report = check_alerts(db)

    assert report.fired == 0
    assert count_alerts(db) == 0


def test_drop_pct_fires_when_the_drop_is_real(db, add_product, add_rule):
    """真的跌够了 → 触发（上面那条测试的对照，证明规则本身有效）。"""
    # 历史最高 400，当前 200 → 跌 50%
    p = add_product(sid="a-1", title="某商品", price=200.0, history=[400.0, 300.0, 200.0])
    rule = add_rule(product=p, rule_type=RULE_PRICE_DROP_PCT, threshold=40.0)

    report = check_alerts(db)

    assert report.fired == 1
    assert state_of(db, rule) == "fired"


# ===========================================================================
# 六、写进去的那条记录长什么样
# ===========================================================================


def test_records_the_price_at_trigger_time(db, add_product, add_rule):
    """`price_at_trigger` 必须冗余存下**当时**的价格。

    price_history 里可能有几万个点，事后想还原"当时为什么触发"
    要去反查时间点，既慢又可能因为数据清理而查不到。
    """
    p = add_product(sid="a-1", title="安克 充电宝", price=88.5)
    add_rule(product=p, rule_type=RULE_PRICE_BELOW, threshold=100.0)

    check_alerts(db)

    alert = db.execute(select(Alert)).scalar_one()
    assert alert.price_at_trigger == pytest.approx(88.5)


def test_trigger_writes_the_rule_snapshot(db, add_product, add_rule):
    """触发时必须把**当时生效的规则**一起写进记录。

    ★ 这是"记录触发原因"的另一半。`price_at_trigger` 只答了
      "当时多少钱"，答不了"当时在比什么"——而那要两个数：
      价格（有了）和**当时生效的阈值 / 规则类型**（这条在测的）。

    ★ 写进去的必须真的是**规则对象上的值**，不是重新查一遍库：
      这里让规则和参数故意不一致（建规则传 threshold=100.0，
      但断言的是记录里存的 100.0 而不是某个默认值），
      并且断言的是规则本身的值——两边一致才说明取的是同一处。
    """
    p = add_product(sid="a-1", title="安克 充电宝", price=88.5)
    rule = add_rule(product=p, rule_type=RULE_PRICE_BELOW, threshold=100.0)

    check_alerts(db)

    alert = db.execute(select(Alert)).scalar_one()
    assert alert.rule_type_at_trigger == RULE_PRICE_BELOW
    assert alert.threshold_at_trigger == pytest.approx(100.0)
    # 前提：这两列不该是"恰好和规则一样"的巧合——规则本身就是这个值
    assert rule.threshold == pytest.approx(100.0)


def test_the_snapshot_survives_a_later_rule_change(db, add_product, add_rule):
    """★ 端到端地把这个 bug 复现一遍，然后确认它没有了。

    这是**实测复现过**的那个场景（真库上跑的）：
    商品跌到 89.9 触发 → 用户把阈值改成 50 → 那条历史记录
    原本会变成"价格低于 ¥50 ｜ 触发价 ¥89.9"，一个假命题。

    ★ 和 test_api_alerts.py 里那条的区别：这条从**真实触发**开始，
      走完整的「触发 → 改规则 → 读库」路径。接口那条是单测读取侧。
      两条都要有——否则"触发时没写快照"和"读的时候没读快照"
      这两种坏法，只会各被其中一条抓到。
    """
    p = add_product(sid="a-1", title="安克 充电宝", price=89.9)
    rule = add_rule(product=p, rule_type=RULE_PRICE_BELOW, threshold=100.0)
    check_alerts(db)
    alert_id = db.execute(select(Alert.id)).scalar_one()

    # 模拟 PATCH：就地改规则（接口层做的事就是这两行赋值）
    rule.threshold = 50.0
    db.commit()

    # ★ expire_all() 不能省：测试的会话是 expire_on_commit=False，
    #   commit 不会让 Alert 对象过期——上面那次 commit 改的是 alert_rules，
    #   但 Alert 对象仍留在会话的身份映射里。不 expire 的话，下面读到的是
    #   **内存里那份**，而这条用例要证明的恰恰是"库里那行没被改"。
    #   同一件事 check_alerts 自己也做了一次（见 docs 16.10）。
    db.expire_all()
    stored = db.get(Alert, alert_id)
    assert stored.threshold_at_trigger == pytest.approx(100.0), (
        "阈值被追溯改写了：记录现在会说『价格低于 ¥50 ｜ 触发价 ¥89.9』，"
        "而 89.9 < 50 是假的"
    )
    assert stored.price_at_trigger == pytest.approx(89.9)


def test_notified_stays_false_because_there_is_no_push_channel(db, add_product, add_rule):
    """★ 锁定一个**当前不成立的功能**：本项目没有外部推送渠道。

    不发邮件、不发 webhook。日志和界面上的列表都是【拉】的，不是【推】的——
    用户打开页面才看得到。所以这一列恒为 False。

    ★ 为什么这值得一条测试：这一列的语义是"是否已经推送出去"。
      它现在恒为 False 是**事实**，而看数据的人很容易把它读成
      "推送失败了"或者"推送还没跑"。将来真的接了推送渠道时，
      这条测试会失败——那一刻正是需要有人回来重新定义这一列语义的时刻。
      这是主动给自己设的一个路标，不是"测了个没用的东西"。
    """
    p = add_product(sid="a-1", title="安克 充电宝", price=88.0)
    add_rule(product=p, rule_type=RULE_PRICE_BELOW, threshold=100.0)

    check_alerts(db)

    alert = db.execute(select(Alert)).scalar_one()
    assert alert.notified is False


def test_triggered_at_is_recorded(db, add_product, add_rule):
    """触发时间必须落库（前端按它排序显示）。"""
    p = add_product(sid="a-1", title="安克 充电宝", price=88.0)
    add_rule(product=p, rule_type=RULE_PRICE_BELOW, threshold=100.0)

    check_alerts(db)

    alert = db.execute(select(Alert)).scalar_one()
    assert alert.triggered_at is not None


# ===========================================================================
# 七、并发与事务：WHERE 子句才是权威
# ===========================================================================


def test_the_where_clause_wins_over_another_process(db, add_product, add_rule):
    """★ 模拟"另一个进程已经把它触发了"。

    用**另一个会话**改库（这正是另一个进程的样子），然后本会话里的对象
    还停在 armed 上。此时检查器必须**不**写第二条记录——
    因为权威是 UPDATE 的 WHERE 子句，不是上面读到的那个值。

    这条测试把本项目踩过的那个坑（16.10：批量 UPDATE 不同步会话对象）
    翻成了正面用法：既然读到的值可能过期，那就别拿它做判断依据。
    """
    p = add_product(sid="a-1", title="安克 充电宝", price=88.0)
    rule = add_rule(product=p, rule_type=RULE_PRICE_BELOW, threshold=100.0)

    # 另一个"进程"抢先触发了
    other = Session(bind=db.get_bind())
    other.execute(
        update(AlertRule).where(AlertRule.id == rule.id).values(state="fired")
    )
    other.commit()
    other.close()

    # 先确认前提成立：本会话里的对象**确实**是过期的
    assert rule.state == "armed", "这个测试的前提没成立：会话里的值没有过期"

    report = check_alerts(db)

    # 库里已经是 fired，所以这次不该再触发
    assert report.fired == 0
    assert count_alerts(db) == 0
    assert state_of(db, rule) == "fired"


def test_state_changes_are_visible_in_the_same_session(db, add_product, add_rule):
    """★ check_alerts 返回后，同一会话里必须能读到**新的**状态。

    实现里用的是批量 UPDATE，而它不会把新值同步到会话里已有的对象上
    （16.10）。不 expire 的话，调用方随后读 `rule.state` 会拿到旧值——
    而这次改的恰好是状态机本身的状态，"库改了、读的人不知道"
    在这个场景里意味着"刚触发的规则看起来还是 armed"。
    """
    p = add_product(sid="a-1", title="安克 充电宝", price=88.0)
    rule = add_rule(product=p, rule_type=RULE_PRICE_BELOW, threshold=100.0)

    check_alerts(db)

    # ★ 这里故意**不用** state_of()（它会先 expire_all）——
    #   要测的就是"调用方什么都不做时，读到的值对不对"。
    assert rule.state == "fired"


def test_a_crash_mid_way_can_be_rolled_back_cleanly(db, add_product, add_rule, monkeypatch):
    """崩在检查途中 → 回滚之后不留半截状态（规则仍是 armed，没有记录）。

    ★ 检查器**自己不吞异常**：吞掉的话，"崩了"和"检查过了没触发"
      在报告里长得一样，而这两件事的处置完全相反。
      回滚是调用方的事（worker 的 except 分支负责，见 scraper/worker.py）。
      这里把那条约定测出来。
    """
    p = add_product(sid="a-1", title="安克 充电宝", price=88.0)
    rule = add_rule(product=p, rule_type=RULE_PRICE_BELOW, threshold=100.0)

    # ★ 先把夹具建的数据提交掉。
    #   夹具只 flush 不 commit（那是对的——提交时机由被测代码决定），
    #   而这一条测的正是"回滚"，所以必须先让夹具的写入落在事务之外。
    #   不提交的话，回滚会把**夹具自己建的规则**一起抹掉，
    #   于是测的就不是"检查器有没有留半截状态"，而是"夹具的数据还在不在"。
    #   （这条一开始就是这么失败的：state_of 读出来 None，规则整行没了。）
    db.commit()

    def boom(*args, **kwargs):
        raise RuntimeError("模拟写 alerts 时崩了")

    monkeypatch.setattr(db, "add", boom)

    with pytest.raises(RuntimeError):
        check_alerts(db)

    # 调用方（worker）做的就是这一下
    db.rollback()

    assert state_of(db, rule) == "armed"
    assert count_alerts(db) == 0

    # 而且回滚之后这个会话还能继续用——不会被一条断掉的事务卡死
    monkeypatch.undo()  # 撤掉上面那个"崩"，否则它会对每一次调用都崩
    assert check_alerts(db).fired == 1


# ===========================================================================
# 八、报告本身
# ===========================================================================


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        (AlertReport(), "没有启用中的告警规则"),
        (AlertReport(rules_checked=3), "检查 3 条规则，无变化"),
        (AlertReport(rules_checked=3, fired=1), "检查 3 条规则，触发 1"),
        (
            AlertReport(rules_checked=3, fired=1, rearmed=2, undecidable=1),
            "检查 3 条规则，触发 1，重新武装 2，★ 1 条判不了",
        ),
        (AlertReport(rules_checked=2, undecidable=2), "检查 2 条规则，★ 2 条判不了"),
    ],
)
def test_report_message(report, expected):
    """报告的一行摘要——它会被写进 worker 日志，也会显示在界面上。"""
    assert report.as_message() == expected


def test_report_message_says_undecidable_loudly(db, add_product, add_rule):
    """★ "判不了"必须带个记号，不能和"没变化"长一样。

    没有记号的话，"我的告警怎么一直没响"会被答成"价格还没到"，
    而真相是那条规则从来没有被真正执行过。
    """
    p = add_product(sid="a-1", title="某商品", price=None)
    add_rule(product=p, rule_type=RULE_PRICE_BELOW, threshold=100.0)

    assert "★" in check_alerts(db).as_message()


# ===========================================================================
# 九、和 worker 的接缝
#
# ★ 这一节测的是**集成**，不是状态机：告警逻辑再对，
#   如果 worker 里那两行没接上，功能就是没生效的。
#   而"没生效"和"生效了但没触发"在日志里长得一样。
# ===========================================================================


def test_check_alerts_once_swallows_errors_and_leaves_the_session_usable(
    db, add_product, add_rule, monkeypatch
):
    """★ 告警检查崩了不能拖垮 worker，而且必须回滚。

    告警是循环里的**附带动作**。它出问题不该让采集一起停——
    "告警查不出来"和"采集停了"是完全不同的两个故障等级。

    回滚那一半同样重要：异常抛出时事务是开着的，
    不回滚的话这个会话后续每一次操作都会在出错的事务里跑，
    报出来的错会离真正的原因越来越远。
    """
    import scraper.worker as worker

    p = add_product(sid="a-1", title="安克 充电宝", price=88.0)
    rule = add_rule(product=p, rule_type=RULE_PRICE_BELOW, threshold=100.0)
    # ★ 夹具只 flush 不 commit，而这一条测的正是回滚——
    #   不先提交的话，回滚会把**夹具建的规则**一起抹掉，
    #   表现是 rules_checked=0（看着像"检查器没工作"，其实是数据没了）。
    #   见 tests/factories.py 顶部那段。
    db.commit()

    def boom(*args, **kwargs):
        raise RuntimeError("模拟检查器崩了")

    monkeypatch.setattr(worker, "check_alerts", boom)

    # ★ 不抛异常：函数自己吞掉
    worker.check_alerts_once(db)

    # 会话仍然可用（没有被一条断掉的事务卡住）
    monkeypatch.undo()
    assert check_alerts(db).fired == 1
    assert state_of(db, rule) == "fired"


def test_once_mode_runs_an_alert_check(db, add_product, add_rule, monkeypatch):
    """★ `--once` 也要跑一次告警检查。

    否则"我手动跑一次 worker 试试告警"会什么也不发生——
    而那正是演示和排查时的用法。人看到的是"没反应"，
    于是会去怀疑规则、阈值、商品，而不是"这一步根本没被调用"。
    """
    import scraper.worker as worker

    p = add_product(sid="a-1", title="安克 充电宝", price=88.0)
    add_rule(product=p, rule_type=RULE_PRICE_BELOW, threshold=100.0)

    # ★ 这两个替换是为了让 main() 落在**测试库**上：
    #   main() 正常会用 SessionLocal（开发库 data/ecom.db），
    #   而测试绝不能碰它——跑一次测试顺手改了开发库是那种
    #   "当时看不出、事后很难解释"的副作用。
    monkeypatch.setattr(worker, "init_database", lambda: None)
    monkeypatch.setattr(worker, "SessionLocal", lambda: db)

    exit_code = worker.main(["--once"])

    assert exit_code == 0
    assert count_alerts(db) == 1


class _StopLoop(Exception):
    """从 worker 真实的 `while True` 里跳出来。

    ★ 用一个自己的异常，不用 `KeyboardInterrupt`——那个会被 main() 捕获
      并变成 `return 130`，于是"循环被中断"和"正常退出"就分不清了。
    """


def test_the_loop_checks_alerts_right_after_startup(db, monkeypatch):
    """★ 启动后的**第一次**告警检查，不能取决于"这台机器开了多久"。

    修之前：`last_alert_check` 初始化成 `0.0`，而下面用的是
    `time.monotonic()`，它的原点是**开机时刻**（Windows 上是开机以来的秒数）。
    于是 `now - 0.0 >= 300` 悄悄变成了「开机时长 ≥ 300 秒」。

    实测（把 monotonic 换成一个受控的值，跑真实的 main() 循环）：

        开机       0.0 秒后启动 worker → ★ 没查
        开机     120.0 秒后启动 worker → ★ 没查
        开机     299.0 秒后启动 worker → ★ 没查
        开机     300.0 秒后启动 worker → 查了

    ★ 这个 bug 的形状值得记一笔：注释写的是"一启动就先查一次"，
      代码也**看起来**是在表达那个意思（0.0 = 很久以前），
      而它实际的含义完全取决于这个函数选的时间原点。
      换成 `time.time()` 它就是对的——所以它也是那种"改一行看似无关的
      代码就会忽然坏掉"的地方，必须有测试钉住。

    ★ 和最上面那条 `--once` 的测试是两种启动方式，都要守：
      那条守"别漏掉这个动作"，这条守"别等到开机满 5 分钟才做"。
    """
    import scraper.worker as worker

    called: list[int] = []

    monkeypatch.setattr(worker, "init_database", lambda: None)
    monkeypatch.setattr(worker, "SessionLocal", lambda: db)
    monkeypatch.setattr(worker, "recover_stale_tasks", lambda _db: 0)
    # 没有任务可跑 → 循环会走到 sleep，正好在那里跳出来
    monkeypatch.setattr(worker, "run_once", lambda _db, **kw: 0)
    monkeypatch.setattr(worker, "check_alerts_once", lambda _db: called.append(1))

    class FreshBoot:
        """模拟"刚开机 30 秒就启动了 worker"。

        ★ 整块替换 worker 模块里的 `time` 引用，而不是去改
          `time.monotonic` 这个属性——后者改的是**全局 time 模块**，
          会把整个进程（包括 pytest 自己的计时）一起改掉。
        """

        @staticmethod
        def monotonic() -> float:
            return 30.0

        @staticmethod
        def sleep(_seconds: float) -> None:
            raise _StopLoop

    monkeypatch.setattr(worker, "time", FreshBoot)

    with pytest.raises(_StopLoop):
        worker.main([])

    assert called == [1], (
        "worker 启动后没有立刻查一次告警。"
        "★ 先去看 last_alert_check 的初值是不是又写成了 0.0："
        "monotonic 的原点是开机时刻，0.0 的意思是『刚开机』而不是『很久以前』。"
    )
