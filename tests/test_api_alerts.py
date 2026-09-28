"""告警接口：建规则、改规则、看记录、手动查一轮。

================================================================================
这份测试守的是什么
================================================================================
`tests/test_alerts.py` 守的是**状态机**（判断对不对、状态推得对不对）。
这一份守的是**接口**，而接口层坏起来是另一种形状——它不会算错数，
它**放不该放的东西进来，或者把该说的话说漏**：

  · `rule_type` 校验松了 → 库里出现一条谁也不认识的规则，
    而它每 5 分钟被判一次"判不了"，界面上安安静静
  · 跌幅阈值 150% 放进来 → 永远不会触发的规则，看起来像"还没到"
  · 重复规则没挡住 → 一个价格事件写两条 alerts
  · 改规则时**不重置状态** → 改了阈值却永远不响（文件顶部【3】那个静默空洞）
  · 改规则时**无条件重置状态**（哪怕什么都没改）→ 保存一下就把 fired 清掉了
  · 删规则时 `delete(Alert)` **漏了 where** → 删一条规则，全库的触发记录都没了
  · 错误信息只说"不合法" → 调用方得去翻源码才知道合法值是什么

共同点：**全都不报错**，只是数据库里多了一条或少了全部。

================================================================================
★ 两处刻意的写法
================================================================================
1. **`MAX_RULES` 用 monkeypatch 改小**，不真建 100 条规则。
   真建 100 条要发 100 次请求，慢，而且测的是"100 这个数字"——
   而这里要测的是"到上限时会拒绝、且告诉调用方上限是多少"。

2. **每条测试自己断言"前提成立"再断言"结果"**。
   比如测"重新武装"之前先断言规则确实是 fired——
   否则一个 constant 实现（永远 armed）也能让测试变绿。
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
    """把接口的数据库依赖换成测试会话。

    ★ 和 test_tasks.py 里那份是同一个写法，包括 finally 里的 clear()。
    """
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


# ===========================================================================
# 小工具
#
# ★ `state_of` / `set_state` / `set_price` / `add_alert` / `count_alerts`
#   都住在 tests/factories.py。test_alerts.py 用的是同一批函数——
#   各写一份的话，某天修好了其中一份的取数方式，另一份就继续按老写法"通过"。
# ===========================================================================


def create_rule(client, product_id: int, **kw) -> dict:
    """建一条规则并返回响应体（断言 2xx）。"""
    body = {"product_id": product_id, "rule_type": "price_below", "threshold": 100.0}
    body.update(kw)
    resp = client.post("/api/alerts/rules", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ===========================================================================
# 一、建规则
# ===========================================================================


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
    """★ 新规则必须是 armed。

    ★ 这一条不只在测"初始值填对了"，它决定了**新建即触发**的行为：
      armed 意味着下一轮检查就会判它。如果哪天有人为了"别吵到用户"
      把它改成 fired，用户配的规则就会永远不响，
      而界面上一切正常（enabled=True、价格也确实低于阈值）。
    """
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
    """★ 报错要说清楚**有哪些合法值**。

    ★ 只断言 422 是不够的：一个把 detail 写成"参数错误"的实现也能过。
      而真正让人少绕弯的是那份合法值清单——调用方（前端开发者、curl 的人）
      不该为了知道能填什么去翻源码。
    """
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
    """★ 边界：跌 100% 是"价格掉到 0"，是可能事件（真实数据里就有 0 元的）。

    上界是闭区间，写成 `>= 100 拒绝` 就把这条合法规则挡掉了。
    """
    product, _ = make
    data = create_rule(client, product.id, rule_type="price_drop_pct", threshold=100.0)
    assert data["threshold"] == 100.0


def test_price_below_may_exceed_100(client, db, make):
    """★ 100 这个上界只对**跌幅**成立，不是给所有阈值设的。

    价格 800 元的商品配一条"低于 500 提醒我"完全正常。
    如果实现把校验写成了"阈值不能超过 100"，这个用例会红。
    """
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
    """★ 去重是**精确匹配**，不是"差不多"。

    ★ 引入容差就要回答"多接近算一样"，那是个需要解释的魔法数字，
      而两条阈值相近的规则本来就是用户自己配的，不算错误——
      真正要防的是"连点两次提交建出两条一模一样的"。
    """
    product, _ = make
    create_rule(client, product.id, threshold=100.0)
    create_rule(client, product.id, threshold=100.001)

    assert client.get("/api/alerts/rules").json()["total"] == 2


# ---- 建规则时的"它响不了"提醒（★ 来自实测数据，不是假想的问题）------------


def test_price_below_never_warns(client, db, make):
    """只有跌幅规则会踩到基准问题，价格规则不受影响。"""
    product, _ = make
    assert create_rule(client, product.id, threshold=100.0)["warning"] is None


def test_drop_pct_without_history_warns_undecidable(client, db, make):
    """★ 夹具默认不造价格历史点 → 跌幅规则每轮都会"判不了"。

    ★ 库里 100 个真实商品里**有 2 个**是这个状态（实测），
      而它在界面上和"价格还没到"长得一样。
    """
    product, _ = make
    data = create_rule(client, product.id, rule_type="price_drop_pct", threshold=20.0)

    assert data["warning"] is not None
    assert "判不了" in data["warning"]
    # ★ 规则照样建出来了——警告不是拒绝。
    assert client.get("/api/alerts/rules").json()["total"] == 1


def test_drop_pct_with_one_point_warns_it_cannot_fire(client, db, make):
    """★ 本组最重要的一条：98% 的真实商品就是这个状态。

    只有 1 个价格点时，跌幅的基准 = 那唯一的价 = 当前价 → 跌幅恒为 0 →
    **这条规则永远不会触发，而且它不报错、不算"判不了"、界面上安安静静**。
    （实测：100 个真实商品里 98 个只有 1 个价格点，2 个没有，0 个有 2 个以上。）

    ★ 断言里要出现"不会触发"这个意思：只断言 `warning is not None`
      的话，一个提示"记得多喝水"的实现也能过。
    """
    product, _ = make
    add_point(db, product, 89.9)  # 只有一个价格点

    data = create_rule(client, product.id, rule_type="price_drop_pct", threshold=20.0)
    assert data["warning"] is not None
    assert "不会触发" in data["warning"]


def test_drop_pct_with_two_points_does_not_warn(client, db, make):
    """★ 反证：有 2 个点时**不能**发提醒。

    ★ 少了这条，一个"跌幅规则一律提示"的实现也能让上面两条变绿，
      而那样的提醒很快就会变成谁都不看的噪音。
    """
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


# ===========================================================================
# 二、改规则（★ 本文件最重要的一组）
# ===========================================================================


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
    """★ 本文件最重要的一条。

    少了它会出现一个**静默的空洞**：

        规则"低于 100" → 跌到 99 → 触发（state=fired）
        → 用户把阈值改成 200
        → 当前价 99 < 200，条件成立，但 state 还是 fired → 跳过
        → **用户以为改完就会响，实际它永远不会响**

    ★ 反过来说，那条断言 `state == "armed"` 之所以有意义，
      是因为**先断言了它原来是 fired**——否则一个"永远不动 state"
      的实现也能让测试变绿。
    """
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
    """★ 一个空 PATCH 不该改变状态机的状态。

    "保存了一下但什么都没改，于是重新武装了"是个很难查的怪事：
    界面上用户只是点了个保存，然后收到一条他早已看过的通知。
    """
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
    """★ 改成跌幅规则、同时给一个 150 的阈值 → 必须拦下。

    ★ 校验要对着**改完之后**的类型做，不是改之前那个。
      实现里如果先赋值再校验，或者拿旧类型去校验，
      这条会红——而库里会多出一条永远不触发的规则。
    """
    product, _ = make
    rule_id = create_rule(client, product.id)["id"]

    resp = client.patch(
        f"/api/alerts/rules/{rule_id}",
        json={"rule_type": "price_drop_pct", "threshold": 150.0},
    )
    assert resp.status_code == 422


def test_patch_validates_threshold_when_type_is_already_drop_pct(client, db, make):
    """★ 另一条路径：类型本来就是跌幅，只改阈值。

    "payload 里没传 rule_type"和"规则本来就是跌幅"是两种走法，
    实现里少写一个分支就会漏掉其中一条。
    """
    product, _ = make
    rule_id = create_rule(
        client, product.id, rule_type="price_drop_pct", threshold=20.0
    )["id"]

    resp = client.patch(f"/api/alerts/rules/{rule_id}", json={"threshold": 150.0})
    assert resp.status_code == 422


def test_rejected_patch_leaves_the_rule_untouched(client, db, make):
    """★ 422 之后规则必须原样不动。

    ★ 如果实现是"先赋值、再校验"，被拒的这次请求已经改掉了内存里的对象，
      而 commit 可能由下一个请求带出去——于是"报错了但值变了"。
      这里断言的是**库里的值**（先 expire_all 再读）。
    """
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
    """★ PATCH 必须和 POST 走同一套去重。理由见 _assert_not_duplicate。

    只挡建成的话，防线可以被绕过：
      规则 A（低于 100）、规则 B（低于 150）→ 把 A 改成 150
      → 两条一模一样的规则，一个价格事件写两条 alerts。
    """
    product, _ = make
    a = create_rule(client, product.id, threshold=100.0)["id"]
    b = create_rule(client, product.id, threshold=150.0)["id"]

    resp = client.patch(f"/api/alerts/rules/{a}", json={"threshold": 150.0})
    assert resp.status_code == 409
    assert f"#{b}" in resp.json()["detail"]


def test_patch_to_its_own_current_value_is_not_a_duplicate(client, db, make):
    """★ 去重要排除**自己**。

    否则"把阈值改成它现在这个值"会被判成和自己重复——一个荒谬的 409，
    而它只在"改别的字段时顺带传上了阈值"这种正常操作里出现。
    """
    product, _ = make
    rule_id = create_rule(client, product.id, threshold=100.0)["id"]

    resp = client.patch(
        f"/api/alerts/rules/{rule_id}", json={"threshold": 100.0, "enabled": False}
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


# ===========================================================================
# 三、规则列表
# ===========================================================================


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
    """★ 计数不能串台：A 的触发次数不能记到 B 头上。

    ★ 一个"忘了 where 或忘了 group by"的实现会让两条规则都显示总数。
    """
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
    """★ 停用的规则也要出现在列表里。

    ★ 过滤掉的话，它在界面上就消失了，用户**再也没法把它打开**——
      而 enabled=False 的全部意义就是"暂时关掉，以后还能开"。
    """
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
    """★ `?source=` 在告警页同样可用（它就是 apply_source 的第二种合法形状）。

    ★ 这条同时验证了 deps.py 里写的那个约定：**主表不是 Product 也能用**，
      前提是每条结果恰好对应一个商品。断言两侧都要有——
      只断言"过滤后剩 1 条"的话，一个把全部数据都过滤掉的实现也能过。
    """
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
    """★ 手动查一轮是**动作**，不是查询——它永远检查全部规则。

    ★ 跟着界面上的数据源开关走的话，会出现"我在 mock 视图下点了检查，
      结果我那条真实商品的规则没被查到"，而且没有任何提示。
    """
    product, _ = make
    real = product_factory(db)(sid="s-1", title="真实商品", source="smzdm", channel="jingxin")
    create_rule(client, product.id, threshold=999.0)   # mock，条件成立
    create_rule(client, real.id, threshold=999.0)      # 真实，条件成立

    body = client.post("/api/alerts/check?source=mock").json()
    assert body["rules_checked"] == 2
    assert body["fired"] == 2


# ===========================================================================
# 四、删规则
# ===========================================================================


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

    ★ 这条断言的形态是刻意选的：不是"剩下的记录数对不对"，
      而是**具体哪一条**还在。数量对得上但删错行的实现是存在的
      （比如 where 写成 `Alert.id == rule_id`，恰好数量也能对上）。

    ★ 先 commit 再删：夹具只 flush，见 tests/factories.py 的模块说明。
    """
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


# ===========================================================================
# 五、触发记录
# ===========================================================================


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
    """★ 恒为 False——本项目没有外部推送渠道。

    ★ 这条断言的作用不是"验证一个字段的值"，而是**把这个事实钉住**：
      哪天接了推送，会有人来改它，那时他会看到这里写着
      "现在恒为 False"以及为什么。界面上不要把 False 画成"推送失败"。
    """
    product, _ = make
    rule_id = create_rule(client, product.id, threshold=88.0)["id"]
    add_alert(db, rule_id)

    assert client.get("/api/alerts").json()["items"][0]["notified"] is False


def test_list_alerts_is_newest_first(client, db, make):
    """★ 两条记录的 `triggered_at` 几乎相同（同一次测试里连着插的）。

    ★ 所以这条实际测的是**兜底排序列**：只按 triggered_at 排的话，
      同一秒内触发的多条记录顺序由数据库自己决定（SQLite 通常按 rowid，
      但那是实现细节）。少了 `Alert.id.desc()`，翻页时记录会重复或漏掉，
      而单看一次请求完全正常。
    """
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
    """改了规则之后，**历史记录**仍然说自己触发时的那个条件。

    ★ 这条守的是一句话：**事件的字段不该随配置变**。

      `PATCH /rules/{id}` 是**就地**改 `rule_type` / `threshold` 的，
      它不碰 alerts 表（只有 DELETE 规则才连带删记录）。所以历史记录
      如果去读规则表的当前值，就会出现：

          价格低于 ¥50 ｜ 触发价 ¥89.9     ← 89.9 < 50 是假

      这条记录在**宣称一条条件不成立的规则触发过**。而前端那一列
      （frontend/alerts.html 的 ruleText）就是拿这两个值渲染的，
      矛盾会直接显示给人看。

    ★ 用真实触发路径之外的工厂造记录是有意的：
      这里要测的是**读取侧**怎么取这两个值，不是触发侧怎么写。
      触发侧由 test_alerts.py::test_trigger_writes_the_rule_snapshot 守。
    """
    product, _ = make
    rule_id = create_rule(client, product.id, threshold=100.0)["id"]
    add_alert(db, rule_id, price=89.9)

    # 前提：改之前是自洽的
    before = client.get("/api/alerts").json()["items"][0]
    assert before["threshold"] == 100.0
    assert before["price_at_trigger"] < before["threshold"]

    resp = client.patch(f"/api/alerts/rules/{rule_id}", json={"threshold": 50.0})
    assert resp.status_code == 200, resp.text

    # 规则本身确实被改了——不先断言这个的话，一个"PATCH 根本没生效"的
    # 实现也能让下面的断言全绿（那就成了空转）
    assert resp.json()["threshold"] == 50.0

    after = client.get("/api/alerts").json()["items"][0]
    assert after["threshold"] == 100.0, (
        "历史记录跟着规则一起变了——它现在会显示成"
        f"『价格低于 ¥{after['threshold']} ｜ 触发价 ¥{after['price_at_trigger']}』，"
        "而这句话里的不等式是假的"
    )
    assert after["rule_type"] == "price_below"


def test_editing_the_rule_type_does_not_rewrite_its_past_records(client, db, make):
    """改成跌幅规则后，历史记录仍然说"价格低于"，不说"跌了百分之多少"。

    ★ 和上一条是同一个病，但形状不同，所以单独测：
      改的是**类型**而不是阈值，症状从"不等式是假的"变成
      **"触发价"这一列对百分比规则根本没有意义**——
      跌幅规则里没有"触发价低于阈值"这回事，阈值是百分比。
    """
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

    ★ 为什么这个回退是对的，而不是"编一个值"：
      这些记录本来就没有快照，回退到当前值 = **回到加列之前的行为**。
      另一条路（迁移时把"当前值"回填进去）才是编造——它把"不知道"
      伪装成"知道"，而且伪装得很好：界面上看不出任何区别。
      见 app/db/migrate.py 里"故意不做数据回填"那段。

    ★ `snapshot=False` 必须显式写出来，这是工厂的默认值刻意的反面
      （默认造真实形状，老形状要明说）。
    """
    product, _ = make
    rule_id = create_rule(client, product.id, threshold=100.0)["id"]
    add_alert(db, rule_id, price=89.9, snapshot=False)

    # 前提：这一行**真的**没有快照。不先断言这个的话，
    # 一个"工厂忘了写 NULL"的实现会让这条用例测到别的路径却照样绿。
    stored = db.get(Alert, client.get("/api/alerts").json()["items"][0]["id"])
    assert stored.rule_type_at_trigger is None
    assert stored.threshold_at_trigger is None

    item = client.get("/api/alerts").json()["items"][0]
    assert item["threshold"] == 100.0
    assert item["rule_type"] == "price_below"

    # ★ 反着再走一步：改成 50 之后，这条**没有快照**的记录是会跟着变的
    #   （它的值只能来自规则表）。把它断言出来，是为了让"回退"这件事
    #   的代价是可见的——而不是让人以为老记录也被保护了。
    client.patch(f"/api/alerts/rules/{rule_id}", json={"threshold": 50.0})
    assert client.get("/api/alerts").json()["items"][0]["threshold"] == 50.0


# ===========================================================================
# 六、手动查一轮
# ===========================================================================


def test_check_with_no_rules_says_so(client, db):
    body = client.post("/api/alerts/check").json()
    assert body["rules_checked"] == 0
    assert body["message"] == "没有启用中的告警规则"


def test_check_fires_a_rule_that_is_already_true(client, db, make):
    """★ 新建即触发：价格 199 已经低于阈值 250，查一轮就应该响。

    ★ 这是"用户配了一条当下就成立的规则"的正常情形——
      worker 每 5 分钟查一次，所以现实里最多等 5 分钟；
      手动查一轮让演示和调试不必等。
    """
    product, _ = make
    rule_id = create_rule(client, product.id, threshold=250.0)["id"]

    body = client.post("/api/alerts/check").json()
    assert body["rules_checked"] == 1
    assert body["fired"] == 1
    assert "触发 1" in body["message"]
    assert state_of(db, rule_id) == "fired"
    assert count_alerts(db) == 1


def test_check_twice_fires_once(client, db, make):
    """★ 走完整 HTTP 路径的三态验证：第二次必须什么都不发生。

    ★ test_alerts.py 里已经测过状态机本身；这里测的是
      "接口调的就是那个状态机"，而不是又写了一遍判断。
    """
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
    """★ 判不了要和"没触发"分开报，界面上也是两种颜色。

    ★ 跌幅规则 + 商品没有任何历史价 → 算不出基准 → 判不了。
      如果实现把这种情况并进"没触发"，用户会以为"规则在工作、价格还没到"。
    """
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
    """★ 四个计数必须在响应里齐全。

    ★ 少一个（尤其是 undecidable）不会报错——前端拿 undefined 当 0 渲染，
      于是"3 条规则判不了"在界面上就是"今天没有任何异常"。
    """
    product, _ = make
    create_rule(client, product.id, threshold=250.0)

    body = client.post("/api/alerts/check").json()
    for key in ("rules_checked", "fired", "rearmed", "undecidable", "message"):
        assert key in body, f"响应里少了 {key}"
