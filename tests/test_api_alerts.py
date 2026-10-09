"""告警接口：建规则、改规则、看记录、手动查一轮。

test_alerts.py 守状态机，这一份守**接口**——接口层坏起来不会算错数，而是
"放不该放的东西进来，或者把该说的话说漏"（校验松、重复没挡、改规则重置
状态、删规则漏 where），而且**全都不报错**。
两处刻意写法：MAX_RULES 用 monkeypatch 改小、不真建 100 条；每条测试先断言
"前提成立"再断言"结果"，否则 constant 实现也能绿。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import alerts as alerts_api
from app.db.base import get_db
from app.db.models import Alert, AlertRule
from app.main import app
from tests.factories import (
    add_alert,
    add_point,
    alert_rule_factory,
    count_alerts,
    product_factory,
    set_price,
    set_state,
    state_of,
)


@pytest.fixture()
def client(db):
    """把接口的数据库依赖换成测试会话（同 test_tasks.py，含 finally 里的 clear()）。"""
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def make(db):
    """一个商品 + 一条规则 + 一个 `add_rule`。"""
    add_product = product_factory(db)
    add_rule = alert_rule_factory(db)
    product = add_product(sid="a-1", title="安克 充电宝 20000mAh", price=199.0)
    return product, add_rule


# 小工具（state_of / set_state / set_price / add_alert / count_alerts 都住在
# tests/factories.py：test_alerts.py 用的是同一批函数，避免两份实现各自漂移）


def create_rule(client, product_id: int, **kw) -> dict:
    """建一条规则并返回响应体（断言 2xx）。"""
    body = {"product_id": product_id, "rule_type": "price_below", "threshold": 100.0}
    body.update(kw)
    resp = client.post("/api/alerts/rules", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


# 一、建规则


def test_create_rule_returns_it(client, db, make):
    product, _ = make
    data = create_rule(client, product.id, threshold=88.0)

    assert data["id"] > 0
    assert data["product_id"] == product.id
    assert data["product_title"] == product.title
    assert data["rule_type"] == "price_below"
    assert data["threshold"] == 88.0
    assert data["enabled"] is True
    assert data["fired_count"] == 0


def test_new_rule_starts_armed(client, db, make):
    """★ 新规则必须是 armed：它决定了**新建即触发**的行为——初始值若被
    改成 fired，用户配的规则会永远不响，而界面上一切正常（enabled=True）。"""
    product, _ = make
    data = create_rule(client, product.id, threshold=250.0)
    assert data["state"] == "armed"
    assert state_of(db, data["id"]) == "armed"


def test_create_rule_for_unknown_product_is_404(client, db, make):
    resp = client.post(
        "/api/alerts/rules",
        json={"product_id": 99999, "rule_type": "price_below", "threshold": 10.0},
    )
    assert resp.status_code == 404
    assert "99999" in resp.json()["detail"]


def test_create_rule_with_unknown_type_is_422(client, db, make):
    product, _ = make
    resp = client.post(
        "/api/alerts/rules",
        json={"product_id": product.id, "rule_type": "price_goes_up", "threshold": 1.0},
    )
    assert resp.status_code == 422


def test_type_error_lists_every_valid_value(client, db, make):
    """★ 报错要说清楚**有哪些合法值**：只断言 422 的话，一个把 detail 写成
    "参数错误"的实现也能过——而合法值清单正是让人少绕弯的东西。"""
    product, _ = make
    resp = client.post(
        "/api/alerts/rules",
        json={"product_id": product.id, "rule_type": "nope", "threshold": 1.0},
    )
    detail = resp.json()["detail"]
    for valid in ("price_below", "price_drop_pct"):
        assert valid in detail, f"合法值 {valid} 没出现在报错里：{detail}"


@pytest.mark.parametrize("bad", [0, -1, -0.5])
def test_non_positive_threshold_is_rejected(client, db, make, bad):
    """★ 阈值 0 是"价格跌破 0 元"——永远不会成立，却看着像条正常规则。"""
    product, _ = make
    resp = client.post(
        "/api/alerts/rules",
        json={"product_id": product.id, "rule_type": "price_below", "threshold": bad},
    )
    assert resp.status_code == 422


def test_drop_pct_above_100_is_rejected(client, db, make):
    product, _ = make
    resp = client.post(
        "/api/alerts/rules",
        json={
            "product_id": product.id,
            "rule_type": "price_drop_pct",
            "threshold": 150.0,
        },
    )
    assert resp.status_code == 422
    assert "100" in resp.json()["detail"]


def test_drop_pct_exactly_100_is_allowed(client, db, make):
    """★ 边界：跌 100% 是"价格掉到 0"，是可能事件（真实数据里就有 0 元的）；
    上界是闭区间，"达到 100 就拒绝"的实现会把这条合法规则挡掉。"""
    product, _ = make
    data = create_rule(client, product.id, rule_type="price_drop_pct", threshold=100.0)
    assert data["threshold"] == 100.0


def test_price_below_may_exceed_100(client, db, make):
    """★ 100 这个上界只对**跌幅**成立：价格 800 元的商品配一条"低于 500
    提醒我"完全正常——实现写成"阈值不能超过 100"的话这条会红。"""
    product, _ = make
    data = create_rule(client, product.id, threshold=500.0)
    assert data["threshold"] == 500.0


def test_duplicate_rule_is_409_and_names_the_existing_one(client, db, make):
    product, _ = make
    first = create_rule(client, product.id, threshold=100.0)

    resp = client.post(
        "/api/alerts/rules",
        json={"product_id": product.id, "rule_type": "price_below", "threshold": 100.0},
    )
    assert resp.status_code == 409
    assert f"#{first['id']}" in resp.json()["detail"]


def test_same_product_different_threshold_is_fine(client, db, make):
    """去重挡的是"一模一样"，不是"同一个商品多配几条"。"""
    product, _ = make
    create_rule(client, product.id, threshold=100.0)
    create_rule(client, product.id, threshold=150.0)

    resp = client.get("/api/alerts/rules")
    assert resp.json()["total"] == 2


def test_same_threshold_other_product_is_fine(client, db, make):
    product, add_rule = make
    other = product_factory(db)(sid="a-2", title="别的商品", price=50.0)
    create_rule(client, product.id, threshold=100.0)
    create_rule(client, other.id, threshold=100.0)

    assert client.get("/api/alerts/rules").json()["total"] == 2


def test_same_threshold_other_type_is_fine(client, db, make):
    """阈值数字相同但含义不同（100 元 vs 100%），不是重复。"""
    product, _ = make
    create_rule(client, product.id, rule_type="price_below", threshold=100.0)
    create_rule(client, product.id, rule_type="price_drop_pct", threshold=100.0)

    assert client.get("/api/alerts/rules").json()["total"] == 2


def test_nearly_the_same_threshold_is_a_different_rule(client, db, make):
    """★ 去重是**精确匹配**，不是"差不多"：引入容差就要回答"多接近算一样"
    这个魔法数字——要防的是"连点两次提交建出两条一模一样的"。"""
    product, _ = make
    create_rule(client, product.id, threshold=100.0)
    create_rule(client, product.id, threshold=100.001)

    assert client.get("/api/alerts/rules").json()["total"] == 2


# ---- 建规则时的"它响不了"提醒（来自实测数据）----


def test_price_below_never_warns(client, db, make):
    """只有跌幅规则会踩到基准问题，价格规则不受影响。"""
    product, _ = make
    assert create_rule(client, product.id, threshold=100.0)["warning"] is None


def test_drop_pct_without_history_warns_undecidable(client, db, make):
    """★ 夹具默认不造价格历史点 → 跌幅规则每轮都会"判不了"（实测：100 个
    真实商品里有 2 个是这个状态，而它在界面上和"价格还没到"长得一样）。"""
    product, _ = make
    data = create_rule(client, product.id, rule_type="price_drop_pct", threshold=20.0)

    assert data["warning"] is not None
    assert "判不了" in data["warning"]
    # ★ 规则照样建出来了——警告不是拒绝。
    assert client.get("/api/alerts/rules").json()["total"] == 1


def test_drop_pct_with_one_point_warns_it_cannot_fire(client, db, make):
    """★ 本组最重要的一条：只有 1 个价格点时，跌幅基准 = 当前价 → 跌幅恒为 0，
    **规则永远不会触发，且不报错、不算"判不了"、界面上安安静静**（实测：
    100 个真实商品里 98 个只有 1 个价格点）。断言必须出现"不会触发"——
    否则一个提示"记得多喝水"的实现也能过。
    """
    product, _ = make
    add_point(db, product, 89.9)  # 只有一个价格点

    data = create_rule(client, product.id, rule_type="price_drop_pct", threshold=20.0)
    assert data["warning"] is not None
    assert "不会触发" in data["warning"]


def test_drop_pct_with_two_points_does_not_warn(client, db, make):
    """★ 反证：有 2 个点时**不能**发提醒——否则"跌幅规则一律提示"的实现
    也能让上面两条变绿，而那样的提醒很快会变成谁都不看的噪音。"""
    product, _ = make
    add_point(db, product, 200.0)
    add_point(db, product, 89.9)

    data = create_rule(client, product.id, rule_type="price_drop_pct", threshold=20.0)
    assert data["warning"] is None


def test_patch_to_drop_pct_also_warns(client, db, make):
    """★ 改成跌幅规则是另一条路径，同样要提示。"""
    product, _ = make
    rule_id = create_rule(client, product.id, threshold=100.0)["id"]

    resp = client.patch(f"/api/alerts/rules/{rule_id}", json={"rule_type": "price_drop_pct"})
    assert resp.status_code == 200
    assert resp.json()["warning"] is not None


def test_patch_to_price_below_drops_the_warning(client, db, make):
    """改成价格规则之后提醒必须消失——否则它会挂在那儿永远不走。"""
    product, _ = make
    rule_id = create_rule(
        client, product.id, rule_type="price_drop_pct", threshold=20.0
    )["id"]

    resp = client.patch(f"/api/alerts/rules/{rule_id}", json={"rule_type": "price_below"})
    assert resp.json()["warning"] is None


def test_rule_limit_is_enforced(client, db, make, monkeypatch):
    """★ 上限用 monkeypatch 改成 2，不真建 100 条。理由见模块 docstring。"""
    monkeypatch.setattr(alerts_api, "MAX_RULES", 2)
    product, _ = make
    create_rule(client, product.id, threshold=100.0)
    create_rule(client, product.id, threshold=200.0)

    resp = client.post(
        "/api/alerts/rules",
        json={"product_id": product.id, "rule_type": "price_below", "threshold": 300.0},
    )
    assert resp.status_code == 422
    assert "2" in resp.json()["detail"]


def test_rejected_rule_is_not_written(client, db, make):
    """被拒的请求不能在库里留下半条规则。"""
    product, _ = make
    client.post(
        "/api/alerts/rules",
        json={"product_id": product.id, "rule_type": "nope", "threshold": 1.0},
    )
    assert db.query(AlertRule).count() == 0


# 二、改规则（★ 本文件最重要的一组）


def test_disable_rule(client, db, make):
    product, _ = make
    rule_id = create_rule(client, product.id)["id"]

    resp = client.patch(f"/api/alerts/rules/{rule_id}", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    assert db.get(AlertRule, rule_id).enabled is False


def test_reenabling_a_fired_rule_rearms_it(client, db, make):
    product, _ = make
    rule_id = create_rule(client, product.id)["id"]

    client.patch(f"/api/alerts/rules/{rule_id}", json={"enabled": False})
    set_state(db, rule_id, "fired")  # 摆出"触发过、然后被关掉"的局面
    assert state_of(db, rule_id) == "fired"

    client.patch(f"/api/alerts/rules/{rule_id}", json={"enabled": True})
    assert state_of(db, rule_id) == "armed"


def test_changing_threshold_rearms_a_fired_rule(client, db, make):
    """★ 本文件最重要的一条：改了阈值却不重置状态，会出现**静默的空洞**——
    规则"低于 100"已触发 → 用户把阈值改成 200（当前价 99 < 200）→ state
    还是 fired → 跳过，而用户以为改完就会响。

    ★ 断言 `state == "armed"` 有意义，是因为**先断言了它原来是 fired**。"""
    product, _ = make
    rule_id = create_rule(client, product.id, threshold=100.0)["id"]
    set_state(db, rule_id, "fired")

    resp = client.patch(f"/api/alerts/rules/{rule_id}", json={"threshold": 200.0})
    assert resp.status_code == 200
    assert resp.json()["threshold"] == 200.0
    assert state_of(db, rule_id) == "armed"


def test_changing_rule_type_rearms(client, db, make):
    product, _ = make
    rule_id = create_rule(client, product.id)["id"]
    set_state(db, rule_id, "fired")

    client.patch(f"/api/alerts/rules/{rule_id}", json={"rule_type": "price_drop_pct"})
    assert state_of(db, rule_id) == "armed"


def test_empty_patch_leaves_state_alone(client, db, make):
    """★ 一个空 PATCH 不该改变状态机的状态：用户只是点了个保存，不该收到一条
    他早已看过的通知（"什么都没改却重新武装了"是个很难查的怪事）。"""
    product, _ = make
    rule_id = create_rule(client, product.id)["id"]
    set_state(db, rule_id, "fired")

    resp = client.patch(f"/api/alerts/rules/{rule_id}", json={})
    assert resp.status_code == 200
    assert state_of(db, rule_id) == "fired"


def test_patch_with_identical_values_leaves_state_alone(client, db, make):
    """传了值、但和当前值相同 → 同样不算"改动"。"""
    product, _ = make
    rule_id = create_rule(client, product.id, threshold=100.0)["id"]
    set_state(db, rule_id, "fired")

    resp = client.patch(f"/api/alerts/rules/{rule_id}", json={"threshold": 100.0})
    assert resp.status_code == 200
    assert state_of(db, rule_id) == "fired"


def test_patch_unknown_rule_is_404(client, db, make):
    resp = client.patch("/api/alerts/rules/99999", json={"threshold": 1.0})
    assert resp.status_code == 404


def test_patch_rejects_unknown_type(client, db, make):
    product, _ = make
    rule_id = create_rule(client, product.id)["id"]
    resp = client.patch(f"/api/alerts/rules/{rule_id}", json={"rule_type": "nope"})
    assert resp.status_code == 422


def test_patch_validates_threshold_against_the_new_type(client, db, make):
    """★ 改成跌幅规则、同时给 150 的阈值 → 必须拦下：校验要对着**改完之后**
    的类型做——拿旧类型校验的实现会放出一条永远不触发的规则。"""
    product, _ = make
    rule_id = create_rule(client, product.id)["id"]

    resp = client.patch(
        f"/api/alerts/rules/{rule_id}",
        json={"rule_type": "price_drop_pct", "threshold": 150.0},
    )
    assert resp.status_code == 422


def test_patch_validates_threshold_when_type_is_already_drop_pct(client, db, make):
    """★ 另一条路径：payload 里没传 rule_type、规则本来就是跌幅——实现里
    少写一个分支就会漏掉这条。"""
    product, _ = make
    rule_id = create_rule(
        client, product.id, rule_type="price_drop_pct", threshold=20.0
    )["id"]

    resp = client.patch(f"/api/alerts/rules/{rule_id}", json={"threshold": 150.0})
    assert resp.status_code == 422


def test_rejected_patch_leaves_the_rule_untouched(client, db, make):
    """★ 422 之后规则必须原样不动：实现若是"先赋值、再校验"，被拒的请求
    已经改掉了内存里的对象，而 commit 可能被下一个请求带出去。这里断言的
    是**库里的值**（先 expire_all 再读）。"""
    product, _ = make
    rule_id = create_rule(client, product.id, rule_type="price_drop_pct", threshold=20.0)["id"]
    set_state(db, rule_id, "fired")

    client.patch(f"/api/alerts/rules/{rule_id}", json={"threshold": 150.0})

    db.expire_all()
    rule = db.get(AlertRule, rule_id)
    assert rule.threshold == 20.0
    assert rule.rule_type == "price_drop_pct"
    assert rule.state == "fired"


def test_patch_cannot_create_a_duplicate(client, db, make):
    """★ PATCH 必须和 POST 走同一套去重（理由见 _assert_not_duplicate）：
    只挡建成的话，把 A 的阈值改成和 B 一样就绕过去了——两条一模一样的
    规则，一个价格事件写两条 alerts。"""
    product, _ = make
    a = create_rule(client, product.id, threshold=100.0)["id"]
    b = create_rule(client, product.id, threshold=150.0)["id"]

    resp = client.patch(f"/api/alerts/rules/{a}", json={"threshold": 150.0})
    assert resp.status_code == 409
    assert f"#{b}" in resp.json()["detail"]


def test_patch_to_its_own_current_value_is_not_a_duplicate(client, db, make):
    """★ 去重要排除**自己**：否则"改别的字段时顺带传上当前阈值"这种正常
    操作会被判成和自己重复，得到一个荒谬的 409。"""
    product, _ = make
    rule_id = create_rule(client, product.id, threshold=100.0)["id"]

    resp = client.patch(
        f"/api/alerts/rules/{rule_id}", json={"threshold": 100.0, "enabled": False}
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


# 三、规则列表


def test_list_rules_is_empty_initially(client, db):
    body = client.get("/api/alerts/rules").json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["max_rules"] == alerts_api.MAX_RULES


def test_list_rules_carries_product_title_and_source(client, db, make):
    """★ 列表里必须带商品信息，否则前端要为每条规则再查一次商品（N+1）。"""
    product, _ = make
    create_rule(client, product.id)

    item = client.get("/api/alerts/rules").json()["items"][0]
    assert item["product_title"] == product.title
    assert item["product_source"] == "mock"


def test_list_rules_counts_fired_alerts(client, db, make):
    product, _ = make
    rule_id = create_rule(client, product.id)["id"]
    add_alert(db, rule_id)
    add_alert(db, rule_id, price=79.0)

    assert client.get("/api/alerts/rules").json()["items"][0]["fired_count"] == 2


def test_fired_count_is_per_rule(client, db, make):
    """★ 计数不能串台：忘了 where / group by 的实现会让两条规则都显示总数。"""
    product, _ = make
    a = create_rule(client, product.id, threshold=100.0)["id"]
    b = create_rule(client, product.id, threshold=200.0)["id"]
    add_alert(db, a)
    add_alert(db, b)
    add_alert(db, b)

    counts = {
        item["id"]: item["fired_count"]
        for item in client.get("/api/alerts/rules").json()["items"]
    }
    assert counts == {a: 1, b: 2}


def test_list_rules_is_newest_first(client, db, make):
    product, _ = make
    first = create_rule(client, product.id, threshold=100.0)["id"]
    second = create_rule(client, product.id, threshold=200.0)["id"]

    ids = [item["id"] for item in client.get("/api/alerts/rules").json()["items"]]
    assert ids == [second, first]


def test_list_rules_includes_disabled_ones(client, db, make):
    """★ 停用的规则也要出现在列表里：过滤掉它就在界面上消失，用户再也
    没法把它打开——而 enabled=False 的意义就是"暂时关掉，以后还能开"。"""
    product, _ = make
    rule_id = create_rule(client, product.id)["id"]
    client.patch(f"/api/alerts/rules/{rule_id}", json={"enabled": False})

    items = client.get("/api/alerts/rules").json()["items"]
    assert [item["id"] for item in items] == [rule_id]
    assert items[0]["enabled"] is False


def test_list_rules_respects_limit(client, db, make):
    product, _ = make
    for i in range(3):
        create_rule(client, product.id, threshold=100.0 + i)

    assert client.get("/api/alerts/rules?limit=2").json()["total"] == 2


def test_list_rules_filters_by_source(client, db, make):
    """★ `?source=` 在告警页同样可用，也验证 deps.py 的约定：**主表不是
    Product 也能用**，前提是每条结果恰好对应一个商品。两侧都断言——
    只断言"过滤后剩 1 条"的话，一个把全部数据都过滤掉的实现也能过。"""
    product, _ = make  # 默认 source="mock"
    real = product_factory(db)(sid="s-1", title="真实商品", source="smzdm", channel="jingxin")
    create_rule(client, product.id, threshold=100.0)
    create_rule(client, real.id, threshold=100.0)

    assert client.get("/api/alerts/rules?source=smzdm").json()["total"] == 1
    assert client.get("/api/alerts/rules?source=mock").json()["total"] == 1
    assert client.get("/api/alerts/rules?source=all").json()["total"] == 2
    assert client.get("/api/alerts/rules").json()["total"] == 2  # 默认 all


def test_list_alerts_filters_by_source(client, db, make):
    product, _ = make
    real = product_factory(db)(sid="s-1", title="真实商品", source="smzdm", channel="jingxin")
    mock_rule = create_rule(client, product.id, threshold=100.0)["id"]
    real_rule = create_rule(client, real.id, threshold=100.0)["id"]
    add_alert(db, mock_rule)
    add_alert(db, real_rule)

    assert client.get("/api/alerts?source=smzdm").json()["total"] == 1
    assert client.get("/api/alerts?source=mock").json()["total"] == 1
    assert client.get("/api/alerts").json()["total"] == 2


def test_check_is_not_filtered_by_source(client, db, make):
    """★ 手动查一轮是**动作**，不是查询——永远检查全部规则：跟着界面上的
    数据源开关走的话，"mock 视图下点检查，真实商品的规则没被查到"且毫无提示。"""
    product, _ = make
    real = product_factory(db)(sid="s-1", title="真实商品", source="smzdm", channel="jingxin")
    create_rule(client, product.id, threshold=999.0)   # mock，条件成立
    create_rule(client, real.id, threshold=999.0)      # 真实，条件成立

    body = client.post("/api/alerts/check?source=mock").json()
    assert body["rules_checked"] == 2
    assert body["fired"] == 2


# 四、删规则


def test_delete_rule_removes_it(client, db, make):
    product, _ = make
    rule_id = create_rule(client, product.id)["id"]

    resp = client.delete(f"/api/alerts/rules/{rule_id}")
    assert resp.status_code == 200
    assert resp.json()["deleted_rule_id"] == rule_id
    assert db.get(AlertRule, rule_id) is None


def test_delete_says_how_many_alerts_went_with_it(client, db, make):
    product, _ = make
    rule_id = create_rule(client, product.id)["id"]
    add_alert(db, rule_id)
    add_alert(db, rule_id)

    body = client.delete(f"/api/alerts/rules/{rule_id}").json()
    assert body["deleted_alerts"] == 2
    assert count_alerts(db) == 0


def test_delete_does_not_touch_other_rules_alerts(client, db, make):
    """★ `delete(Alert)` 漏掉 where 就会删光全表——这条守着它。

    ★ 断言的是**具体哪一条**还在，不是数量：数量对得上但删错行的实现是
      存在的（比如 where 写成 `Alert.id == rule_id`）。
    ★ 先 commit 再删：夹具只 flush（见 tests/factories.py 模块说明）。"""
    product, _ = make
    a = create_rule(client, product.id, threshold=100.0)["id"]
    b = create_rule(client, product.id, threshold=200.0)["id"]
    alert_b = add_alert(db, b)

    client.delete(f"/api/alerts/rules/{a}")

    remaining = db.query(Alert).all()
    assert [x.id for x in remaining] == [alert_b.id]


def test_delete_unknown_rule_is_404(client, db, make):
    assert client.delete("/api/alerts/rules/99999").status_code == 404


def test_delete_mentions_the_alternative(client, db, make):
    """★ 要保留历史就停用，别删——这句话得在响应里。

    不写的话，"删掉规则发现历史也没了"只能靠踩一次学到。
    """
    product, _ = make
    rule_id = create_rule(client, product.id)["id"]
    body = client.delete(f"/api/alerts/rules/{rule_id}").json()
    assert "停用" in body["notice"]


# 五、触发记录


def test_list_alerts_is_empty_initially(client, db):
    body = client.get("/api/alerts").json()
    assert body["items"] == []
    assert body["total"] == 0


def test_list_alerts_carries_everything_the_ui_needs(client, db, make):
    product, _ = make
    rule_id = create_rule(client, product.id, threshold=88.0)["id"]
    add_alert(db, rule_id, price=77.5)

    item = client.get("/api/alerts").json()["items"][0]
    assert item["rule_id"] == rule_id
    assert item["product_id"] == product.id
    assert item["product_title"] == product.title
    assert item["rule_type"] == "price_below"
    assert item["threshold"] == 88.0
    assert item["price_at_trigger"] == 77.5


def test_notified_is_false(client, db, make):
    """★ 恒为 False——本项目没有外部推送渠道。这条的作用是**把事实钉住**：
    哪天接了推送会有人来这里改它；界面上别把 False 画成"推送失败"。"""
    product, _ = make
    rule_id = create_rule(client, product.id, threshold=88.0)["id"]
    add_alert(db, rule_id)

    assert client.get("/api/alerts").json()["items"][0]["notified"] is False


def test_list_alerts_is_newest_first(client, db, make):
    """★ 两条记录的 `triggered_at` 几乎相同，所以这条实际测的是**兜底排序列**：
    只按 triggered_at 排的话，同一秒内多条的顺序由数据库自己决定——少了
    `Alert.id.desc()`，翻页时会重复或漏掉，而单看一次请求完全正常。"""
    product, _ = make
    rule_id = create_rule(client, product.id)["id"]
    old = add_alert(db, rule_id, price=99.0)
    new = add_alert(db, rule_id, price=88.0)

    ids = [item["id"] for item in client.get("/api/alerts").json()["items"]]
    assert ids == [new.id, old.id]


def test_list_alerts_respects_limit(client, db, make):
    product, _ = make
    rule_id = create_rule(client, product.id)["id"]
    for i in range(4):
        add_alert(db, rule_id, price=float(i))

    assert client.get("/api/alerts?limit=2").json()["total"] == 2


def test_editing_a_rule_does_not_rewrite_its_past_records(client, db, make):
    """★ 改了规则之后，**历史记录**仍说自己触发时的那个条件——事件的字段
    不该随配置变。历史记录若去读规则表的当前值，就会出现"价格低于 ¥50 ｜
    触发价 ¥89.9"（89.9 < 50 是假），而前端（alerts.html 的 ruleText）
    正是拿这两个值渲染的。

    ★ 刻意用工厂而不是真实触发路径造记录：这里测**读取侧**怎么取这两个值，
      触发侧由 test_alerts.py::test_trigger_writes_the_rule_snapshot 守。"""
    product, _ = make
    rule_id = create_rule(client, product.id, threshold=100.0)["id"]
    add_alert(db, rule_id, price=89.9)

    # 前提：改之前是自洽的
    before = client.get("/api/alerts").json()["items"][0]
    assert before["threshold"] == 100.0
    assert before["price_at_trigger"] < before["threshold"]

    resp = client.patch(f"/api/alerts/rules/{rule_id}", json={"threshold": 50.0})
    assert resp.status_code == 200, resp.text

    # 规则本身确实被改了——不然一个"PATCH 根本没生效"的实现也能让下面全绿
    assert resp.json()["threshold"] == 50.0

    after = client.get("/api/alerts").json()["items"][0]
    assert after["threshold"] == 100.0, (
        "历史记录跟着规则一起变了——它现在会显示成"
        f"『价格低于 ¥{after['threshold']} ｜ 触发价 ¥{after['price_at_trigger']}』，"
        "而这句话里的不等式是假的"
    )
    assert after["rule_type"] == "price_below"


def test_editing_the_rule_type_does_not_rewrite_its_past_records(client, db, make):
    """改成跌幅规则后，历史记录仍然说"价格低于"——同一个病、另一种形状：
    改的是**类型**，症状从"不等式是假的"变成"触发价这一列对百分比规则
    根本没有意义"（跌幅的阈值是百分比）。"""
    product, _ = make
    rule_id = create_rule(client, product.id, threshold=100.0)["id"]
    add_alert(db, rule_id, price=89.9)

    resp = client.patch(
        f"/api/alerts/rules/{rule_id}",
        json={"rule_type": "price_drop_pct", "threshold": 20.0},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["rule_type"] == "price_drop_pct"

    after = client.get("/api/alerts").json()["items"][0]
    assert after["rule_type"] == "price_below", (
        f"历史记录的类型跟着规则变成了 {after['rule_type']!r}——"
        "它现在会渲染成『相对历史最高价跌 20% ｜ 触发价 ¥89.9』，"
        "而『触发价』这一列对百分比规则是句没有意义的话"
    )
    assert after["threshold"] == 100.0


def test_a_record_without_a_snapshot_falls_back_to_the_current_rule(client, db, make):
    """加列之前写下的记录（快照为 NULL）回退到规则当前值。

    ★ 回退 = **回到加列之前的行为**；另一条路（迁移时把"当前值"回填）才是
      编造——把"不知道"伪装成"知道"，还伪装得很好（见 app/db/migrate.py
      里"故意不做数据回填"那段）。
    ★ `snapshot=False` 要显式写：工厂默认造真实形状，老形状要明说。"""
    product, _ = make
    rule_id = create_rule(client, product.id, threshold=100.0)["id"]
    add_alert(db, rule_id, price=89.9, snapshot=False)

    # 前提：这一行真的没有快照——不先断言的话，会测到别的路径却照样绿
    stored = db.get(Alert, client.get("/api/alerts").json()["items"][0]["id"])
    assert stored.rule_type_at_trigger is None
    assert stored.threshold_at_trigger is None

    item = client.get("/api/alerts").json()["items"][0]
    assert item["threshold"] == 100.0
    assert item["rule_type"] == "price_below"

    # ★ 反着再走一步：没有快照的记录会跟着规则变（值只能来自规则表）——
    #   把代价断言出来，而不是让人以为老记录也被保护了
    client.patch(f"/api/alerts/rules/{rule_id}", json={"threshold": 50.0})
    assert client.get("/api/alerts").json()["items"][0]["threshold"] == 50.0


# 六、手动查一轮


def test_check_with_no_rules_says_so(client, db):
    body = client.post("/api/alerts/check").json()
    assert body["rules_checked"] == 0
    assert body["message"] == "没有启用中的告警规则"


def test_check_fires_a_rule_that_is_already_true(client, db, make):
    """★ 新建即触发：价格 199 已经低于阈值 250，查一轮就该响——worker
    每 5 分钟查一次，手动查一轮让演示和调试不必等。"""
    product, _ = make
    rule_id = create_rule(client, product.id, threshold=250.0)["id"]

    body = client.post("/api/alerts/check").json()
    assert body["rules_checked"] == 1
    assert body["fired"] == 1
    assert "触发 1" in body["message"]
    assert state_of(db, rule_id) == "fired"
    assert count_alerts(db) == 1


def test_check_twice_fires_once(client, db, make):
    """★ 走完整 HTTP 路径的三态验证：第二次必须什么都不发生——测的是
    "接口调的就是那个状态机"，状态机本身在 test_alerts.py 已测过。"""
    product, _ = make
    rule_id = create_rule(client, product.id, threshold=250.0)["id"]

    client.post("/api/alerts/check")
    second = client.post("/api/alerts/check").json()

    assert second["fired"] == 0
    assert second["rearmed"] == 0
    assert second["message"] == "检查 1 条规则，无变化"
    assert count_alerts(db) == 1
    assert state_of(db, rule_id) == "fired"


def test_check_rearms_when_price_goes_back_up(client, db, make):
    product, _ = make
    rule_id = create_rule(client, product.id, threshold=250.0)["id"]
    client.post("/api/alerts/check")

    set_price(db, product, 299.0)  # 价格涨回阈值上方

    body = client.post("/api/alerts/check").json()
    assert body["rearmed"] == 1
    assert state_of(db, rule_id) == "armed"


def test_check_reports_undecidable(client, db, make):
    """★ 判不了要和"没触发"分开报（界面上也是两种颜色）：跌幅规则 + 没有
    历史价 → 算不出基准，并进"没触发"的话用户会以为"规则在工作、价格没到"。"""
    product, _ = make
    create_rule(
        client, product.id, rule_type="price_drop_pct", threshold=20.0
    )  # 夹具默认不造历史点

    body = client.post("/api/alerts/check").json()
    assert body["undecidable"] == 1
    assert body["fired"] == 0
    assert "判不了" in body["message"]
    assert body["reasons"], "判不了必须给出原因"


def test_check_skips_disabled_rules(client, db, make):
    product, _ = make
    rule_id = create_rule(client, product.id, threshold=250.0)["id"]
    client.patch(f"/api/alerts/rules/{rule_id}", json={"enabled": False})

    body = client.post("/api/alerts/check").json()
    assert body["rules_checked"] == 0
    assert count_alerts(db) == 0


def test_check_returns_a_report_not_a_bare_number(client, db, make):
    """★ 四个计数必须在响应里齐全：少一个（尤其是 undecidable）不会报错——
    前端拿 undefined 当 0 渲染，"3 条规则判不了"就成了"今天没有任何异常"。"""
    product, _ = make
    create_rule(client, product.id, threshold=250.0)

    body = client.post("/api/alerts/check").json()
    for key in ("rules_checked", "fired", "rearmed", "undecidable", "message"):
        assert key in body, f"响应里少了 {key}"
