"""告警状态机（`app/alerts_engine.py`）的测试。

三条规则里只有第一条会写数据（armed+成立→触发；fired+成立→跳过；fired+不成立→
重新武装）。"没发生的事"最难测：把 fired 判成 armed 在真实系统里是用户被同一事件
骚扰 288 次/天，所以下面几乎都是**精确计数**（`== 1` 而不是 `>= 1`）。另注：
`check_alerts()` 只读价格、不改价格，"涨回阈值上方"由测试自己改库模拟。
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


# ★ `count_alerts` / `state_of` / `set_price` 住在 tests/factories.py：
#   test_api_alerts.py 也用它们，各留一份的话，修好一份、另一份照旧"通过"。


# 一、三态状态机 —— 本文件的核心


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
    """★★ 全文件最重要的一条：价格持续低于阈值也只能有一条记录——
    没有状态机的话，5 分钟检查一次就是一天 288 条。"""
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
    """fired + 条件不成立 → 重新武装，但**不写记录、不发通知**：涨回阈值上方
    不是用户关心的事件，涨回那条通知也没有任何可执行的信息。"""
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
    """重新武装之后再跌破 → 新的价格事件，再触发一次（做成"触发一次就永久
    熄火"的话，第二次跌破会被静默漏掉，而用户以为规则还在看着）。"""
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


# 二、条件本身（纯函数，可以逐条测边界）


@pytest.mark.parametrize(
    ("price", "threshold", "expected"),
    [
        (99.9, 100.0, True),
        # ★ 恰好等于阈值**不算**跌破（用 `<` 而不是 `<=`）：100.00 元既
        #   不低于也不高于，边界上宁可少报一次。
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
        # ★ 涨过历史最高价（新爆料更贵）→ 跌幅是负数，不该触发
        (250.0, 200.0, 0.0, False),
    ],
)
def test_drop_pct_boundaries(price, baseline, threshold, expected):
    assert _triggered(RULE_PRICE_DROP_PCT, threshold, price, baseline) is expected


@pytest.mark.parametrize("baseline", [None, 0.0, -1.0])
def test_drop_pct_without_a_usable_baseline_is_undecidable(baseline):
    """没有基准价 → None（判不了），不是 False（检查过了、没到）——两者在
    界面上长得一样，含义却相反：False 是"规则在工作"，None 是"根本没执行"。"""
    assert _triggered(RULE_PRICE_DROP_PCT, 10.0, 150.0, baseline) is None


def test_unknown_rule_type_is_undecidable_not_false():
    """不认识的规则类型 → None，不是 False：返回 False 等于谎称"检查过了、
    条件不成立"，而代码根本不知道该怎么判它。"""
    assert _triggered("price_goes_to_the_moon", 10.0, 150.0, 200.0) is None


# 三、判不了的规则（"没触发"和"没工作"必须分开）


def test_product_without_price_is_undecidable(db, add_product, add_rule):
    """商品没有当前价（真实数据里确实存在）→ 判不了，不触发：100 条真实
    爆料里有 2 条卡片上没写价格（见 RawDeal.price_raw），诚实的回答是
    "判不了"，不是"没到阈值"。"""
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
    """库里有一行规则类型不认识（手工写的 / 将来新增的）→ 必须是 undecidable
    而不是静默跳过：跳过的规则永远不触发，界面上却显示"启用中、正在工作"。"""
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


# 四、哪些规则会被检查


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


# 五、跌幅规则的基准：必须来自 price_history，而不是 products.price_max


def test_drop_pct_uses_history_max_not_the_deal_range(db, add_product, add_rule):
    """★★ 回归测试：跌幅基准是**历史最高价**（price_history MAX），不是本次爆料的
    价格区间上限（products.price_max）。
    构造数据 price_max=1000 / 历史最高 200 / 当前 190：用 price_history 算跌 5% 不
    触发；错用 price_max 算 81% 会发假警报——真实数据上 price_max 恒等于当前价
    （每条爆料都是单值区间），"跌幅"永远是 0% 且不报错。"""
    p = add_product(sid="a-1", title="某商品", price=190.0, history=[150.0, 200.0, 180.0])
    add_rule(product=p, rule_type=RULE_PRICE_DROP_PCT, threshold=20.0)

    # 先确认这份数据确实是"两种算法结论相反"的构造
    db.execute(
        update(type(p)).where(type(p).id == p.id).values(price=190.0, price_max=1000.0)
    )
    db.commit()

    # ★ 证明这份数据**确实有区分度**：错用 price_max 时它会触发——不做这个
    #   断言的话，数据可能"两种基准都不触发"，测试照样绿，却什么也没守住。
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


# 六、写进去的那条记录长什么样


def test_records_the_price_at_trigger_time(db, add_product, add_rule):
    """`price_at_trigger` 必须冗余存下**当时**的价格：price_history 里可能有
    几万个点，事后反查"当时为什么触发"既慢又可能因数据清理而查不到。"""
    p = add_product(sid="a-1", title="安克 充电宝", price=88.5)
    add_rule(product=p, rule_type=RULE_PRICE_BELOW, threshold=100.0)

    check_alerts(db)

    alert = db.execute(select(Alert)).scalar_one()
    assert alert.price_at_trigger == pytest.approx(88.5)


def test_trigger_writes_the_rule_snapshot(db, add_product, add_rule):
    """触发时必须把**当时生效的规则**（类型 + 阈值）一起写进记录——
    `price_at_trigger` 只答"当时多少钱"，答不了"当时在比什么"。
    ★ 同时断言记录里的值和规则对象上的值：两边一致才说明写进去的不是默认值。"""
    p = add_product(sid="a-1", title="安克 充电宝", price=88.5)
    rule = add_rule(product=p, rule_type=RULE_PRICE_BELOW, threshold=100.0)

    check_alerts(db)

    alert = db.execute(select(Alert)).scalar_one()
    assert alert.rule_type_at_trigger == RULE_PRICE_BELOW
    assert alert.threshold_at_trigger == pytest.approx(100.0)
    # 前提：规则对象上的值也是 100.0（不是巧合）
    assert rule.threshold == pytest.approx(100.0)


def test_the_snapshot_survives_a_later_rule_change(db, add_product, add_rule):
    """★ 端到端复现"改阈值追溯改写历史记录"：跌到 89.9 触发 → 阈值改成 50 →
    那条记录仍必须说"价格低于 ¥100.0 ｜ 触发价 ¥89.9"。
    ★ 和 test_api_alerts.py 那条（读取侧单测）互补：这条从**真实触发**开始走完整
      路径——"触发时没写快照"和"读的时候没读快照"各由其中一条抓到。"""
    p = add_product(sid="a-1", title="安克 充电宝", price=89.9)
    rule = add_rule(product=p, rule_type=RULE_PRICE_BELOW, threshold=100.0)
    check_alerts(db)
    alert_id = db.execute(select(Alert.id)).scalar_one()

    # 模拟 PATCH：就地改规则（接口层做的事就是这两行赋值）
    rule.threshold = 50.0
    db.commit()

    # ★ expire_all() 不能省：会话是 expire_on_commit=False，不 expire 读到的
    #   就是**内存里那份**——而这条要证明的恰恰是"库里那行没被改"（16.10）。
    db.expire_all()
    stored = db.get(Alert, alert_id)
    assert stored.threshold_at_trigger == pytest.approx(100.0), (
        "阈值被追溯改写了：记录现在会说『价格低于 ¥50 ｜ 触发价 ¥89.9』，"
        "而 89.9 < 50 是假的"
    )
    assert stored.price_at_trigger == pytest.approx(89.9)


def test_notified_stays_false_because_there_is_no_push_channel(db, add_product, add_rule):
    """★ 锁定一个**当前不成立的功能**：本项目没有推送渠道（不发邮件/webhook，
    列表都是【拉】的），所以这一列恒为 False——将来真接了推送，这条会红，正是
    要回来重新定义这一列语义的时刻。"""
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


# 七、并发与事务：WHERE 子句才是权威


def test_the_where_clause_wins_over_another_process(db, add_product, add_rule):
    """★ 模拟"另一个进程已经把它触发了"：另一个会话改库，本会话里的对象还停在
    armed——检查器必须**不**写第二条记录，因为权威是 UPDATE 的 WHERE 子句。"""
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
    """★ check_alerts 返回后，同一会话里必须能读到**新的**状态：批量 UPDATE
    不会把新值同步到会话里已有的对象（16.10），不 expire 的话调用方随后读
    `rule.state` 会拿到旧值——"刚触发的规则看起来还是 armed"。"""
    p = add_product(sid="a-1", title="安克 充电宝", price=88.0)
    rule = add_rule(product=p, rule_type=RULE_PRICE_BELOW, threshold=100.0)

    check_alerts(db)

    # ★ 这里故意**不用** state_of()（它会先 expire_all）——
    #   要测的就是"调用方什么都不做时，读到的值对不对"。
    assert rule.state == "fired"


def test_a_crash_mid_way_can_be_rolled_back_cleanly(db, add_product, add_rule, monkeypatch):
    """崩在检查途中 → 回滚之后不留半截状态（规则仍是 armed，没有记录）。
    ★ 检查器**自己不吞异常**：吞掉的话"崩了"和"没触发"在报告里长得一样，而处置
      完全相反；回滚是调用方（worker，见 scraper/worker.py）。"""
    p = add_product(sid="a-1", title="安克 充电宝", price=88.0)
    rule = add_rule(product=p, rule_type=RULE_PRICE_BELOW, threshold=100.0)

    # ★ 先把夹具建的数据提交掉：夹具只 flush 不 commit，不提交的话回滚会把
    #   **夹具自己建的规则**一起抹掉，测的就成了"夹具的数据还在不在"。
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


# 八、报告本身


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
    """★ "判不了"必须带个记号：否则"我的告警怎么一直没响"会被答成"价格还没
    到"，而真相是那条规则从来没有被真正执行过。"""
    p = add_product(sid="a-1", title="某商品", price=None)
    add_rule(product=p, rule_type=RULE_PRICE_BELOW, threshold=100.0)

    assert "★" in check_alerts(db).as_message()


# 九、和 worker 的接缝（集成：告警逻辑再对，worker 里那两行没接上，功能就是
# 没生效的——而"没生效"和"生效了但没触发"在日志里长得一样）


def test_check_alerts_once_swallows_errors_and_leaves_the_session_usable(
    db, add_product, add_rule, monkeypatch
):
    """★ 告警检查崩了不能拖垮 worker，而且必须回滚：告警是循环里的**附带动作**，
    不回滚的话会话后续每次操作都会在出错的事务里跑，报的错离真正原因越来越远。"""
    import scraper.worker as worker

    p = add_product(sid="a-1", title="安克 充电宝", price=88.0)
    rule = add_rule(product=p, rule_type=RULE_PRICE_BELOW, threshold=100.0)
    # ★ 先提交夹具建的数据（理由同上文回滚用例）：不提交的话回滚会把
    #   **夹具建的规则**一起抹掉，表现是 rules_checked=0。
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
    """★ `--once` 也要跑一次告警检查：否则"手动跑一次 worker 试试告警"什么
    也不发生，而人会去怀疑规则、阈值、商品，而不是"这一步没被调用"。"""
    import scraper.worker as worker

    p = add_product(sid="a-1", title="安克 充电宝", price=88.0)
    add_rule(product=p, rule_type=RULE_PRICE_BELOW, threshold=100.0)

    # ★ 让 main() 落在**测试库**上：它正常用 SessionLocal（开发库
    #   data/ecom.db），而测试绝不能碰它。
    monkeypatch.setattr(worker, "init_database", lambda: None)
    monkeypatch.setattr(worker, "SessionLocal", lambda: db)

    exit_code = worker.main(["--once"])

    assert exit_code == 0
    assert count_alerts(db) == 1


class _StopLoop(Exception):
    """从 worker 真实的 `while True` 里跳出来——用自己的异常，不用
    `KeyboardInterrupt`（那个会被 main() 捕获变成 `return 130`，分不清中断和正常退出）。"""


def test_the_loop_checks_alerts_right_after_startup(db, monkeypatch):
    """★ 启动后的**第一次**告警检查，不能取决于"这台机器开了多久"。
    修前：`last_alert_check` 初始化成 `0.0`，而 `time.monotonic()` 的原点是**开机
    时刻**，于是 `now - 0.0 >= 300` 悄悄变成了「开机时长 ≥ 300 秒」。实测：开机
    0.0 / 120.0 / 299.0 秒后启动都不查，300.0 秒才查。
    ★ 和 `--once` 那条是两种启动方式，都要守。"""
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
        ★ 整块替换 worker 的 `time` 引用而不是改属性——后者改的是**全局 time
          模块**，连 pytest 自己的计时一起改掉。"""

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
