"""API 冒烟测试：灌少量数据，断言每个接口的结构与语义。

================================================================================
为什么这些测试值得写（而不是"跑一遍看没报错就行"）
================================================================================
几个接口的行为是**容易被无意改坏、而且改坏了不报错**的：

  · 数据源过滤 —— 漏掉一处，Mock 和真实数据就在某张图里混算了
  · 空态返回 —— 顺手改成 404 或抛异常，前端直接白屏
  · 排序兜底列 —— 删掉之后翻页会丢数据，但单次请求看起来完全正常
  · NULL 的排序位置 —— 没有 nullslast 的话"没有价格的商品"会排在**最前面**
  · 饼图占比 —— 少了"未知店铺"这一档，各扇区之和悄悄不到 100%

共同点是：都不会报错、不会有异常日志，只是数字错了。
所以每一个都用测试把**可观测的行为**钉住。

================================================================================
★ 两件必须知道的事
================================================================================
1. **测试用的是内存库**，通过 `app.dependency_overrides[get_db]` 换掉。
   生产库 `data/ecom.db` 一个字节都不会被碰。

2. **`TestClient(app)` 故意不用 `with`。**
   实测（见下）不带 `with` 时 Starlette **不会触发 lifespan**，
   于是 `main.init_database()` 不会跑——正好避免了"跑一次测试
   顺手在开发库里建表"这种副作用。
   改成 `with TestClient(app) as client:` 就会触发，别改。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from app.db.base import get_db, utcnow
from app.db.models import PriceHistory, Product, Shop
from app.main import app

# ★ 不用 with，理由见模块 docstring 第 2 条。
#   （实测：不带 with 时 lifespan 不运行；带 with 时会运行。）


@pytest.fixture()
def client(db):
    """把接口的数据库依赖换成测试会话。"""
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        # ★ 必须清理。dependency_overrides 是挂在**全局 app 对象**上的，
        #   不清理的话下一个测试文件会拿到这个已经关闭的会话，
        #   然后报一堆 "Session is closed"——而报错位置在别的文件里，
        #   排查方向完全被带偏。
        app.dependency_overrides.clear()


# ===========================================================================
# 造数据的小工具
# ===========================================================================


# ★ 实现已经搬到 conftest.py 的 `product_factory` 里，
#   因为阶段 3 的 test_reporter_cache.py 需要造同一批字段的商品。
#   这里保留 `_add(db, ...)` / `_shop(db, ...)` 的调用形式
#   （本文件 40 多个用例都在用它们），只是接到共享实现上。
#
#   为什么必须共用一份：两份"造商品"的代码会悄悄分叉，
#   比如一边给 price_max 填了值、另一边留空，
#   于是"某个接口的行为"在两组测试里其实测的是两种数据。
#   这类分叉不报错，只在某天表现为"改了 A 却让 B 的测试变红"。
from tests.factories import product_factory


def _add(db, **kwargs) -> Product:
    """插一个商品。签名和字段含义见 conftest.product_factory。"""
    return product_factory(db)(**kwargs)


def _shop(db, name: str, platform: str = "京东") -> Shop:
    """取或建一家店铺。"""
    return product_factory(db).shop(name, platform)


def _seed_both_sources(db) -> None:
    """3 个 Mock + 2 个真实采集（真实数据没有销量，这是本项目的前提）。"""
    _add(db, sid="mock-1", title="罗马仕 充电宝 20000毫安 双向 包邮", price=99.9,
         sales=5000, shop="京东自营", points=3)
    _add(db, sid="mock-2", title="绿联 充电宝 10000毫安 轻薄 正品", price=59.9,
         sales=3000, shop="京东自营")
    _add(db, sid="mock-3", title="安克 氮化镓充电器 120W 三口", price=199.9,
         sales=800, shop="安克官方旗舰店", category="充电器")

    # ★ 真实采集的商品：sales 一律为 NULL
    #   —— SMZDM 好价页整页 HTML 里"已售/销量"出现 0 次（实测）。
    _add(db, sid="183089533", title="今日必买：京觅 猕猴桃 24枚 礼盒装", source="smzdm",
         price=17.5, sales=None, shop=None, channel="jingxuan", brand="京觅",
         category="食品生鲜")
    _add(db, sid="182950953", title="绿联 磁吸手机指环扣 金属 超薄 深空灰", source="smzdm",
         price=29.9, sales=None, shop=None, channel="jingxuan", brand="UGREEN/绿联",
         category="手机配件", points=1)
    db.commit()


def _get(client, path: str, **params):
    r = client.get(path, params=params or None)
    assert r.status_code == 200, f"{path} 返回 {r.status_code}：{r.text[:300]}"
    return r.json()


# ===========================================================================
# 一、健康检查
# ===========================================================================


def test_health_ok(client) -> None:
    body = _get(client, "/api/health")
    assert body["status"] == "ok"
    assert isinstance(body["llm_configured"], bool)


def test_health_never_echoes_the_api_key(client, monkeypatch) -> None:
    """★ 健康检查只回答"配没配"，绝不能把 key 回显出来。

    理由：健康检查的返回值最容易被随手贴进 issue、截图、聊天记录里。
    一旦它带了 key，泄露就发生在"为了排查问题而分享信息"的过程中，
    而且当事人完全没有感觉。

    ★ 这里用一个假 key 来验：真 key 绝不能出现在测试里（测试文件会进 git）。
    """
    from app.config import settings

    fake = "fake-secret-key-for-testing-0123456789"
    monkeypatch.setattr(settings, "glm_api_key", fake)

    r = client.get("/api/health")
    assert r.status_code == 200
    assert fake not in r.text, "健康检查把 API key 回显出来了"
    assert r.json()["llm_configured"] is True


def test_health_rejects_a_key_with_non_ascii_characters(client, monkeypatch) -> None:
    """★ 非 ASCII 的 key 要判为"未配置"。

    从网页上复制 key 时经常会带进全角空格或中文标点。
    症状是 401 或者 `UnicodeEncodeError`，而报错信息指向的是请求头，
    完全不会提示"你的 key 里混了个全角空格"。
    与其让用户去猜，不如在健康检查里直接说没配好。
    """
    from app.config import settings

    monkeypatch.setattr(settings, "glm_api_key", "abc１２３")
    assert _get(client, "/api/health")["llm_configured"] is False


def test_health_rejects_placeholder_keys(client, monkeypatch) -> None:
    """有人会把 .env.example 里的示例值当真的填进去。"""
    from app.config import settings

    monkeypatch.setattr(settings, "glm_api_key", "your_key_here")
    assert _get(client, "/api/health")["llm_configured"] is False


# ===========================================================================
# 二、★ 数据源隔离（本项目最核心的一个字段）
# ===========================================================================


def test_sources_endpoint_lists_both(client, db) -> None:
    _seed_both_sources(db)
    body = _get(client, "/api/stats/sources")

    by_source = {s["source"]: s for s in body["sources"]}
    assert set(by_source) == {"mock", "smzdm"}
    assert by_source["mock"]["product_count"] == 3
    assert by_source["smzdm"]["product_count"] == 2
    assert by_source["mock"]["price_point_count"] == 3
    assert by_source["smzdm"]["price_point_count"] == 1

    # ★ 销量是"真实数据没有、Mock 才有"的那一项
    assert by_source["mock"]["has_sales"] is True
    assert by_source["smzdm"]["has_sales"] is False


def test_source_filter_isolates_the_two_datasets(client, db) -> None:
    """★★ 双数据源隔离的正面验证。

    同一个库、同一张表、同一段查询代码，靠一个过滤参数分成两批。
    这三条断言就是"隔离确实生效"的全部证据。
    """
    _seed_both_sources(db)

    mock = _get(client, "/api/stats/overview", source="mock")
    smzdm = _get(client, "/api/stats/overview", source="smzdm")
    both = _get(client, "/api/stats/overview", source="all")

    assert mock["product_count"] == 3
    assert smzdm["product_count"] == 2
    assert both["product_count"] == 5
    # ★ all 必须正好是两者之和——这条最容易在加了第三个数据源时失效
    assert mock["product_count"] + smzdm["product_count"] == both["product_count"]


def test_source_filter_applies_to_price_history_too(client, db) -> None:
    """★ 价格历史表自己没有 source 列，要靠 join 回 products 才过滤得到。

    这是"过滤只写一处"这个设计最容易出漏洞的地方：
    stats 里的价格历史、销量、店铺三个聚合都要 join，
    漏掉任何一个，那一项就会混算。
    """
    _seed_both_sources(db)

    assert _get(client, "/api/stats/overview", source="mock")["price_point_count"] == 3
    assert _get(client, "/api/stats/overview", source="smzdm")["price_point_count"] == 1
    assert _get(client, "/api/stats/overview", source="all")["price_point_count"] == 4


def test_invalid_source_is_rejected_with_422(client) -> None:
    """非法取值要被 FastAPI 挡在业务代码之前，而不是当成"没有数据"。"""
    assert client.get("/api/stats/overview", params={"source": "jd"}).status_code == 422


# ===========================================================================
# 三、★ 空态：没有数据时返回 200 + 空数组 + 一句解释
# ===========================================================================


def test_sales_top_returns_empty_array_not_an_error_for_a_source_without_sales(
    client, db
) -> None:
    """★★ plan 里点名的验证点。

    真实电商的销量不在网页端公开，所以 `?source=smzdm` 下销量必然为空。
    接口必须返回 **200 + 空数组 + 解释**，而不是 404 或者 500——
    前端才有机会显示"这不代表系统坏了，而是这个数据源本来就没有销量"。
    """
    _seed_both_sources(db)

    body = _get(client, "/api/stats/sales-top", source="smzdm")
    assert body["items"] == []
    assert body["has_sales"] is False
    assert body["notice"], "空态必须带一句解释，否则前端只能显示一片空白"
    assert "销量" in body["notice"]

    # 对照组：Mock 有销量，就不该出现空态文案
    mock = _get(client, "/api/stats/sales-top", source="mock")
    assert mock["has_sales"] is True
    assert mock["notice"] is None
    assert len(mock["items"]) == 3


def test_empty_source_says_there_is_no_data_not_no_sales(client, db) -> None:
    """★ 空态文案的**优先级**。

    一个还没采集过的数据源，说它"没有销量"是误导——它是什么都没有。
    这两句话指向完全不同的行动（"去采集" vs "这个字段本来就采不到"），
    所以必须分开。

    ★ 用"先只灌 mock、再灌 smzdm"来构造两种状态，
      而不是灌完再删——删商品会撞 price_history 的外键。
      （外键校验是我们自己开的 PRAGMA，它在这里正确地挡住了一次
        会产生孤儿价格记录的删除。测试写错了也能被它拦住，挺好。）
    """
    # ① 只有 mock 有数据 → 查 smzdm 是"什么都没有"
    _add(db, sid="m1", title="mock 商品", price=99.9, sales=100)
    db.commit()

    body = _get(client, "/api/stats/sales-top", source="smzdm")
    assert body["items"] == []
    assert "暂无数据" in body["notice"], (
        f"没有商品的数据源应该提示「暂无数据」，实际是 {body['notice']!r}"
    )

    # ② 灌入 smzdm 数据之后 → 语义变成"有商品，但没有销量"
    _add(db, sid="183089533", title="今日必买：京觅 猕猴桃 24枚", source="smzdm",
         price=17.5, sales=None, channel="jingxuan")
    db.commit()

    body = _get(client, "/api/stats/sales-top", source="smzdm")
    assert body["items"] == []
    assert "销量" in body["notice"]
    assert "暂无数据" not in body["notice"], (
        "有商品的数据源不该再说「暂无数据」——那会让用户以为采集失败了"
    )


def test_all_empty_endpoints_return_200_for_a_source_with_nothing(client) -> None:
    """★ 一次性把**所有**接口在空库上的行为钉住。

    单独测一个接口不够：这个项目里"忘了处理空表"是逐个端点漏的，
    每漏一个就是前端的一块白屏。
    """
    for path in (
        "/api/stats/overview",
        "/api/stats/price-distribution",
        "/api/stats/sales-top",
        "/api/stats/shop-share",
        "/api/stats/keywords",
    ):
        body = _get(client, path, source="smzdm")
        assert body["source"] == "smzdm"
        assert body["notice"], f"{path} 在空数据源上没有给出解释"

    # 商品列表在空库上是"0 条 / 1 页"，不是错误
    page = _get(client, "/api/products", source="smzdm")
    assert page["total"] == 0 and page["items"] == []


# ===========================================================================
# 四、总览
# ===========================================================================


def test_overview_price_summary(client, db) -> None:
    _seed_both_sources(db)
    body = _get(client, "/api/stats/overview", source="mock")

    price = body["price"]
    assert price["min"] == 59.9
    assert price["max"] == 199.9
    # 中位数取排序后中间那个：59.9 / 99.9 / 199.9 → 99.9
    assert price["median"] == 99.9
    assert price["p25"] <= price["median"] <= price["p75"]
    assert body["price_missing"] == 0
    assert body["has_sales"] is True
    assert body["sales"]["total"] == 5000 + 3000 + 800
    assert body["sales"]["max"] == 5000


def test_overview_counts_products_without_a_price_separately(client, db) -> None:
    """★ 价格为空是**真实采集的常态**（卡片上没写价格），
    不能让这些商品悄悄从价格统计里消失——那会让"平均价"偏高，
    而且没人知道少了几个。所以单独计数并返回。"""
    _add(db, sid="m1", title="有价商品", price=100.0)
    _add(db, sid="m2", title="无价商品", price=None)
    db.commit()

    body = _get(client, "/api/stats/overview", source="mock")
    assert body["product_count"] == 2
    assert body["price_missing"] == 1
    assert body["price"]["median"] == 100.0


def test_overview_counts_unknown_source_is_zero_not_an_error(client, db) -> None:
    """★ all 之外的过滤条件没有匹配行时，聚合函数返回的是 NULL。
    `func.sum(sales)` 返回 None，直接 `int()` 会崩。"""
    body = _get(client, "/api/stats/overview", source="all")
    assert body["product_count"] == 0
    assert body["price"] is None
    assert body["sales"] is None
    assert body["has_sales"] is False


# ===========================================================================
# 五、价格分布
# ===========================================================================


def test_price_distribution_buckets_are_contiguous_and_complete(client, db) -> None:
    """★ 直方图的两个不变量：桶之间无缝无重叠，且总数等于样本数。

    少了任何一个，图都会"看起来对但是错的"：
      · 有空隙 → 柱子的 x 轴位置错位，读者按坐标轴读数会读错
      · 总数对不上 → 有些商品没被统计进去，而图上完全看不出来
    """
    for i in range(40):
        _add(db, sid=f"p{i:03d}", title=f"商品{i}", price=30.0 + i * 10, sales=i)
    db.commit()

    body = _get(client, "/api/stats/price-distribution", source="mock")

    assert body["basis"] == "current_price_per_product"
    assert body["total"] == 40
    assert sum(b["count"] for b in body["buckets"]) == 40

    for prev, nxt in zip(body["buckets"], body["buckets"][1:]):
        assert prev["upper"] == nxt["lower"], f"桶之间有缝：{prev} → {nxt}"

    # ★ 步长必须落在"好看"的阶梯上（app/api/stats.py 的 _NICE_STEPS）。
    #   注意不能写成 `% 10 == 0`：阶梯里有 25 这个步长，
    #   而 25 是完全合理的（100/125/150 的坐标轴比 100.0/138.4/176.9 好读得多）。
    #   第一版就是这么写错的，被这条断言自己抓了出来。
    assert body["bucket_step"] in (5, 10, 20, 25, 50, 100, 200, 250, 500, 1000), (
        f"桶步长 {body['bucket_step']} 不在好看阶梯上，坐标轴会出现小数"
    )
    # 边界必须是整数，否则标签会出现 "100.0-150.0" 这种又长又难读的形式
    for b in body["buckets"]:
        assert b["lower"] == int(b["lower"]) and b["upper"] == int(b["upper"])
        assert b["count"] >= 0


def test_price_distribution_puts_the_maximum_into_the_last_bucket(client, db) -> None:
    """★ 最大值恰好等于最后一个桶的上界时，下标会越界。

    这是分桶代码每次都会写错一次的地方，所以单独钉一条。
    """
    _add(db, sid="only", title="唯一商品", price=300.0)
    db.commit()

    body = _get(client, "/api/stats/price-distribution", source="mock")
    assert sum(b["count"] for b in body["buckets"]) == 1


def test_price_distribution_on_identical_prices_does_not_divides_by_zero(client, db) -> None:
    """所有价格相同时 range=0，步长算法会除以零（或算出 0 个桶）。"""
    for i in range(5):
        _add(db, sid=f"s{i}", title=f"同价{i}", price=88.0)
    db.commit()

    body = _get(client, "/api/stats/price-distribution", source="mock")
    assert sum(b["count"] for b in body["buckets"]) == 5


def test_price_distribution_discloses_the_products_it_cannot_bucket(client, db) -> None:
    """★ 没有价格的商品**没法放进任何一根柱子**，所以它们必须被说出来。

    ★ 这条是照着同一个文件里的店铺占比立的规矩
      （`app/api/stats.py` 的 UNKNOWN_SHOP）：
      饼图把"没有店铺信息的商品"显式归到一档，理由是
      "不能让它们悄悄消失……否则图上看起来少了 15%，而没人知道那 15% 去哪了"。
      直方图面对的是同一件事，却把那个数字**算了之后丢掉**
      （`prices, _ = _fetch_current_prices(...)`）。

    ★ 它为什么比饼图更隐蔽：各柱之和**是**等于 total 的，
      图自身毫无破绽。出问题的是读者对百分比的理解——
      前端拿 total 当分母（frontend/static/js/charts.js），
      于是"这个价位占 X%"被读成占全部商品，而分母里根本没有那些无价格的商品。
      实测偏差：真实采集源 2.0%（100 个里 2 个）、all 0.2%、mock 0.0%。
      **这个数今天不大**，所以这条不是数据错误，是"没说清楚"——
      但它没有下限，且 `total` 是对外公开的接口字段，调用方无从知道这件事。
    """
    for i in range(3):
        _add(db, sid=f"ok{i}", title=f"有价{i}", price=100.0 + i * 50)
    _add(db, sid="np1", title="暂无报价的商品", price=None)
    _add(db, sid="np2", title="另一个没价格的", price=None)
    db.commit()

    body = _get(client, "/api/stats/price-distribution", source="mock")

    assert body["total"] == 3, "total 是「能进桶的商品数」，不含无价格的"
    assert body["price_missing"] == 2
    # ★ 先断言"有那句话"再断言"那句话里有数字"：
    #   反过来写的话，notice 为 None 时会抛
    #   TypeError: argument of type 'NoneType' is not iterable——
    #   读起来像测试自己写错了，而不像"后端一个字都没说"。
    assert body["notice"], "有商品没能进桶却一句话都没有，图上看不出来"
    # 两个数字都要出现在那句话里，否则读者算不出分母是多少
    assert "2" in body["notice"] and "3" in body["notice"], (
        f"提醒里必须同时给出「没价格的数量」和「分母」，实际是：{body['notice']!r}"
    )


def test_price_distribution_does_not_say_no_data_when_products_exist(client, db) -> None:
    """★★ 有商品、但一个价格都没有时，**不能说「暂无数据（尚未采集）」**。

    ★ 这是同一份文件里已经为销量图表立过的规矩（见
      `test_empty_source_notice_semantics` 上方那条断言的注释）：
      "有商品的数据源不该再说「暂无数据」——那会让用户以为采集失败了"。
      可它俩指向的动作完全相反：
        · 尚未采集   → 去跑一次采集
        · 有商品无价格 → 页面结构变了 / 解析器坏了 / 这个源不公开价格

    ★ 触发路径不是假设：smzdm 的商品价格来自列表页的 `z-highlight`，
      解析器一旦不匹配，所有价格都会变成 None——
      而那种情况下这张图会说「尚未采集」，
      同时上面的卡片写着「商品数 100」，两块屏幕自相矛盾。
    """
    for i in range(3):
        _add(db, sid=f"np{i}", title=f"没价格{i}", price=None)
    db.commit()

    body = _get(client, "/api/stats/price-distribution", source="mock")

    assert body["total"] == 0
    # ★ 先断言用户读到的那句话，再断言数字：这句话才是这个用例要防的东西，
    #   而且失败信息里会带上它的原文，省得为了看一眼文案再跑一次。
    assert body["notice"], "必须给一句解释，否则前端只能画一张白图"
    assert "暂无数据" not in body["notice"], (
        f"有 3 个商品的数据源说了「暂无数据」——会把排查方向带跑：{body['notice']!r}"
    )
    assert "3" in body["notice"]
    assert body["price_missing"] == 3


# ===========================================================================
# 六、销量排行
# ===========================================================================


def test_sales_top_is_sorted_descending_and_limited(client, db) -> None:
    for i in range(10):
        _add(db, sid=f"s{i}", title=f"商品{i}", price=100.0, sales=(i + 1) * 100)
    db.commit()

    body = _get(client, "/api/stats/sales-top", source="mock", limit=4)
    sales = [it["sales"] for it in body["items"]]
    assert sales == sorted(sales, reverse=True)
    assert len(sales) == 4
    assert sales[0] == 1000


def test_sales_top_keeps_the_tail_so_skus_stay_distinguishable(client, db) -> None:
    """★ 我们自己的数据形状下（`品牌 品类 规格 颜色`），尾部的颜色会保住。

    裸的 `t[:30]` 会把尾部的颜色切掉，于是图上三个不同的 SKU
    看起来一模一样——这是项目早期真实发生过的事故，
    详见 app/textutil.py 的模块注释。
    """
    _add(db, sid="a", title="绿联 磁吸手机指环扣 金属 超薄 深空灰", price=29.9, sales=900)
    _add(db, sid="b", title="绿联 磁吸手机指环扣 金属 超薄 午夜黑", price=29.9, sales=800)
    db.commit()

    items = _get(client, "/api/stats/sales-top", source="mock")["items"]
    shorts = [it["title_short"] for it in items]
    assert len(set(shorts)) == 2, f"两个不同 SKU 截断后无法区分：{shorts}"
    for s in shorts:
        assert s.endswith(("深空灰", "午夜黑")), f"颜色没被保住：{s}"


def test_sales_top_always_returns_the_full_title_too(client, db) -> None:
    """★★ 这条记录了一个**已经被证伪的想法**，是本文件里最重要的注释。

    我最初的判断是："保尾部就能让不同 SKU 在图例上可区分"。
    给上面那条测试加了一个**真实 SMZDM 形状**的用例之后，它红了：

        绿联 磁吸手机指环扣 金属 超薄 深空灰 商务款 送礼佳品
        绿联 磁吸手机指环扣 金属 超薄 午夜黑 商务款 送礼佳品

    两条在 budget=26/30/40 下截出来的结果**完全相同**——
    因为颜色后面还跟着"商务款 送礼佳品"，尾部分到的预算够不到颜色。
    加大预算也没用，只有完全不截断才可区分。

    → **根因不是预算不够，是方向错了**：
      没有任何固定截断策略能保证"不同 SKU 一定长得不一样"。

    → 真正的修法是：`title_short` 只负责让图例放得下，
      **它绝不能是标题出现的唯一位置**。所以接口必须同时返回完整 title，
      前端把它放进 tooltip——鼠标移上去就能看到全文，信息一点没丢。

    这条测试就是守住那个修法：只要有人把 `title` 删掉、
    只留 `title_short`，它立刻变红。
    """
    full = "绿联 磁吸手机指环扣 金属 超薄 深空灰 商务款 送礼佳品"
    _add(db, sid="a", title=full, price=29.9, sales=900)
    db.commit()

    item = _get(client, "/api/stats/sales-top", source="mock")["items"][0]
    assert item["title"] == full, "接口必须返回完整标题，图表 tooltip 靠它才能显示全文"
    assert len(item["title_short"]) <= len(full)
    assert item["title_short"] != item["title"], "这条标题够长，应该被截断（否则这条测试失去意义）"


def test_sales_top_rejects_an_out_of_range_limit(client) -> None:
    assert client.get("/api/stats/sales-top", params={"limit": 0}).status_code == 422
    assert client.get("/api/stats/sales-top", params={"limit": 500}).status_code == 422


# ===========================================================================
# 七、店铺占比
# ===========================================================================


def test_shop_share_ratios_sum_to_one(client, db) -> None:
    """★ 饼图各扇区之和必须等于 100%。"""
    for i in range(4):
        _add(db, sid=f"a{i}", title=f"A{i}", price=100.0, shop="京东自营")
    for i in range(6):
        _add(db, sid=f"b{i}", title=f"B{i}", price=100.0, shop="安克官方旗舰店")
    db.commit()

    body = _get(client, "/api/stats/shop-share", source="mock")
    assert body["total"] == 10
    assert abs(sum(it["share"] for it in body["items"]) - 1.0) < 1e-6

    top = body["items"][0]
    assert top["shop"] == "安克官方旗舰店"
    assert top["product_count"] == 6
    assert abs(top["share"] - 0.6) < 1e-6


def test_shop_share_sends_a_group_by_that_covers_platform(client, db, engine) -> None:
    r"""★ 店铺占比**真正发给数据库的那条 SQL**，GROUP BY 里必须有 platform。

    ==========================================================================
    为什么这条测试只能查 SQL 形状，不能查行为
    ==========================================================================
    它守的是一个**在 SQLite 上完全观察不到**的问题。

    原来那条语句是：

        SELECT coalesce(shops.name, '未知店铺'), shops.platform, count(*), ...
        ... GROUP BY products.shop_id        ← 只按 shop_id 分组

    `shops.platform` 是个**裸列**（不在 GROUP BY 里、也没套聚合函数）。
    SQLite 对此是宽容的：它从组里**静默**挑一个值给你。

    ★ 挑错了吗？——**没有**。这一点我在真实库快照上实测过：
      按 `products.shop_id` 分成 18 组，逐组数 `COUNT(DISTINCT shops.platform)`，
      **最大值为 1**。因为 join 条件 `shops.id = products.shop_id` 打在
      shops 的**主键**上，platform 被 shop_id 函数决定，组内只有唯一取值。

    ★ 所以这个 bug **没有可观测的行为差异**——写一条行为测试去"验证修复"，
      只能是自欺欺人：那条测试在修复前后都会是绿的。
      修它修的是**可移植性**。PostgreSQL 对裸列是硬错误：
          column "shops.platform" must appear in the GROUP BY clause
          or be used in an aggregate function
      （PG 的功能依赖识别只认"分组列里包含该表主键"；这里分组列是
        products.shop_id，是指向 shops.id 的**外键**而不是那个主键，所以不认。
        —— 这是 PG 的文档行为，本机没有 PG 实例，**我没有实测**，如实标注。）

    ==========================================================================
    ★★ 这条测试的第一版是【假的】，写在这里因为它是个必须记住的陷阱
    ==========================================================================
    第一版是这么写的：

        stmt = (select(shop_label, Shop.platform, ...)
                .join(Shop, ...)
                .group_by(Product.shop_id, Shop.platform))   # ← 在测试里
        grouped = {c for c in stmt._group_by_clauses}
        assert any("platform" in g for g in grouped)

    它**在变异测试下没有红**——把 `app/api/stats.py` 退回
    `.group_by(Product.shop_id)`，这条断言照样通过。

    原因很简单也很难看：**我在测试里把那条查询重新写了一遍**，
    于是它验证的是**我抄的那份副本**，不是 `stats.py` 里真正跑的那条。
    被测对象从头到尾没进过这条测试。

    ★ 这与 `tests/test_config.py` 模块注释里警告的是同一件事：
      "在进程内 monkeypatch 一个假的 settings 再断言，测的是**测试自己写的替身**，
       不是真实行为——那种测试会在实现坏掉时照样变绿。"

    → 现在的做法：**挂一个 SQL 监听器，抓真实发出去的那条语句。**
      被测对象是"经由 endpoint 打到数据库的那行 SQL"，
      不是任何我在测试里构造的东西。变异一下立刻红。
    """
    # 造点数据，保证这条查询真的会被执行（空库时它照样跑，但这里求稳）
    for i in range(3):
        _add(db, sid=f"g{i}", title=f"G{i}", price=100.0, shop="京东自营")
    _add(db, sid="g9", title="G9", price=100.0, shop=None)
    db.commit()

    captured: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, parameters, context, executemany):
        # 只捞店铺占比那条：它同时含 shops 和 GROUP BY
        up = statement.upper()
        if "GROUP BY" in up and "SHOPS" in up:
            captured.append(statement)

    try:
        _get(client, "/api/stats/shop-share", source="mock")
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert captured, "没抓到店铺占比的 SQL——监听器挂错了，这条测试没测到东西"
    sql = captured[0]
    normalized = " ".join(sql.split())          # 折叠换行和多余空格，便于定位
    # 取出 GROUP BY 那一段做断言，避免误判 SELECT 列表里的 platform
    group_clause = normalized.upper().split("GROUP BY", 1)[1]

    assert "PLATFORM" in group_clause, (
        "店铺占比的 GROUP BY 里没有 shops.platform —— "
        "SELECT 里有它，GROUP BY 里就必须有它。\n"
        f"  实际 SQL：\n    {normalized}\n"
        "SQLite 会静默挑一个值让你发现不了；换成 PostgreSQL 直接报错。"
    )
    # ★ 反向确认：分组列里也不能少掉 shop_id。
    #   为什么不用 `group_by(shop_label)`（按 coalesce 出来的别名分组）：
    #   那会把 shop_id 不同、但名字恰好都为 NULL 的两个真实店铺并成一组。
    #   按 shop_id 分组才是"一家店一组"的正确语义。
    assert "SHOP_ID" in group_clause, (
        f"GROUP BY 里没有 products.shop_id。\n  实际 SQL：\n    {normalized}"
    )


def test_shop_share_keeps_products_without_a_shop(client, db) -> None:
    """★ 没有店铺信息的商品必须出现在"未知店铺"里，不能消失。

    用 INNER JOIN 的话它们会被整个丢掉，饼图上看起来一切正常，
    但各扇区之和不到 100%——而饼图的百分比是由扇区**相对大小**决定的，
    读者根本看不出少了东西。
    真实采集早期全是这种情况（SMZDM 好价卡片上就没有店铺）。
    """
    _add(db, sid="a", title="有店的", price=100.0, shop="京东自营")
    _add(db, sid="b", title="没店的1", price=100.0, shop=None)
    _add(db, sid="c", title="没店的2", price=100.0, shop=None)
    db.commit()

    body = _get(client, "/api/stats/shop-share", source="mock")
    assert body["total"] == 3
    by_shop = {it["shop"]: it for it in body["items"]}
    assert "未知店铺" in by_shop
    assert by_shop["未知店铺"]["product_count"] == 2
    assert abs(sum(it["share"] for it in body["items"]) - 1.0) < 1e-6


def test_shop_share_folds_the_tail_into_others_instead_of_dropping_it(client, db) -> None:
    """★ 店铺多的时候折叠成"其他"，而不是截断。

    截断会让饼图少几块，而**百分比会被静默重新归一化**，
    于是每一块的占比都是错的——这是"图上完全看不出来"的典型。
    """
    for i in range(8):
        _add(db, sid=f"many{i}", title=f"多{i}", price=100.0, shop=f"店铺{i:02d}")
    db.commit()

    body = _get(client, "/api/stats/shop-share", source="mock", limit=3)
    assert len(body["items"]) == 4  # 3 家 + 其他
    assert body["others_count"] == 5
    assert body["items"][-1]["shop"].startswith("其他")
    # 各扇区之和仍然是 100%
    assert abs(sum(it["share"] for it in body["items"]) - 1.0) < 1e-6
    assert sum(it["product_count"] for it in body["items"]) == 8


def test_others_avg_price_weights_by_priced_products_not_by_all_products(client, db) -> None:
    """★ "其他"那一块的均价，权重必须是**有价商品数**。

    ★ 这条不能用"手算一个数写进断言"来测：手算的数只能证明今天的实现
      跟我的心算一致，证明不了它算的是对的东西。所以期望值由**一条 SQL**
      给出——直接对尾部所有有价商品求平均。能这么做是因为

          Σ(avgᵢ · nᵢ) / Σnᵢ  ≡  ΣSUMᵢ / Σnᵢ        （avgᵢ = SUMᵢ/nᵢ）

      权重取对有价商品数 nᵢ 时，加权平均与"直接平均"**严格相等**；
      取错权重（含无价商品的 count）时就不等了。
      换句话说：**这条等式是唯一能自动区分正确与错误权重的办法。**

    实测这组数据：
      小店0：3 件商品，1 件有价 ¥300，2 件无价  → SQL 均价 300，权重应为 1
      小店1：2 件商品，2 件有价 ¥100          → SQL 均价 100，权重应为 2
      正确 (300·1 + 100·2)/3 = 166.67
      错误 (300·3 + 100·2)/5 = 220.00
    ★ 差 32%，而两个数看起来都"像个均价"——这种错没有任何外在症状，
      只有把口径算一遍才发现。

    ★ 缺价格不是假想情况：真实采集源今天就是 100 条里 2 条解析不出价格，
      而这个比例没有下限（见 ShopShareOut.total 的 docstring）。
    """
    # 大店 3 家各 4 件商品，全部有价 → 按 count 降序排时稳定占住 head
    for i in range(3):
        for j in range(4):
            _add(db, sid=f"big{i}-{j}", title=f"大店{i} 商品{j}", price=100.0, shop=f"大店{i}")
    # 尾部两家小店，其中"小店0"只有 1 件有价
    _add(db, sid="s0-priced", title="小店0 唯一有价的", price=300.0, shop="小店0")
    _add(db, sid="s0-np1", title="小店0 无价1", price=None, shop="小店0")
    _add(db, sid="s0-np2", title="小店0 无价2", price=None, shop="小店0")
    _add(db, sid="s1-p1", title="小店1 有价1", price=100.0, shop="小店1")
    _add(db, sid="s1-p2", title="小店1 有价2", price=100.0, shop="小店1")
    db.commit()

    body = _get(client, "/api/stats/shop-share", source="mock", limit=3)
    others = body["items"][-1]

    # ★ 先确认"尾部确实是我以为的那两家"，再断言均价。
    #   反过来的话，一旦排序规则变了（比如并列时的兜底列改了），
    #   这条用例会拿另一个数去比，失败信息完全指不出原因。
    assert others["shop"].startswith("其他"), "尾部没落在两家小店上，这条用例没测到东西"
    assert (others["product_count"], others["priced_count"]) == (5, 3)

    tail_ids = [
        s.id for s in db.query(Shop).filter(Shop.name.in_(["小店0", "小店1"])).all()
    ]
    expected = db.execute(
        select(func.avg(Product.price)).where(Product.shop_id.in_(tail_ids))
    ).scalar_one()

    assert others["avg_price"] == pytest.approx(round(float(expected), 2))


def test_every_item_says_how_many_products_its_avg_price_covers(client, db) -> None:
    """★ 均价的**分母**必须对调用方可见，而且加起来要能对上账。

    ★ 这是"口径对不上时，把差额一并交给调用方"那条规矩的兑现点——
      同一个文件里的 `/price-distribution` 就是这么做的（它返回 price_missing）。
      `avg_price` 只能对算得出来的价格求平均，所以它覆盖的范围
      **必然**小于等于 product_count；不给出分母的话，界面上
      "商品数 100 ｜ 均价 ¥99"会被理所当然地读成"100 件商品的均价"。

    ★ 断言的是**恒等式**而不是具体数字：
      Σ priced_count 必须等于该数据源里有价商品的**总数**。
      折叠成"其他"之后这个和仍然成立（"其他"块的 priced_count 是尾部之和），
      所以这条能挡住"折叠时漏掉某一家的分母"这类错误——
      那种错会让账面上凭空多出几件"不知道有没有价格"的商品。
    """
    _add(db, sid="a1", title="甲1", price=10.0, shop="甲")
    _add(db, sid="a2", title="甲2", price=None, shop="甲")
    _add(db, sid="b1", title="乙1", price=20.0, shop="乙")
    _add(db, sid="b2", title="乙2", price=30.0, shop="乙")
    _add(db, sid="c1", title="丙1", price=None, shop="丙")
    _add(db, sid="d1", title="丁1", price=40.0, shop="丁")
    _add(db, sid="e1", title="戊1", price=50.0, shop="戊")
    db.commit()

    # 5 家店、limit=2 → 2 家进 head，3 家折叠成"其他"
    body = _get(client, "/api/stats/shop-share", source="mock", limit=2)
    assert body["others_count"] == 3

    for it in body["items"]:
        assert it["priced_count"] <= it["product_count"]
        # 均价的三个状态必须自洽：有均价 ⟺ 有分母；没均价 ⟺ 没有任何有价商品
        assert (it["avg_price"] is not None) == (it["priced_count"] > 0)

    priced_in_db = db.execute(
        select(func.count(Product.price)).where(Product.source == "mock")
    ).scalar_one()
    assert sum(it["priced_count"] for it in body["items"]) == priced_in_db
    assert sum(it["product_count"] for it in body["items"]) == 7


def test_shop_share_total_and_price_distribution_total_reconcile(client, db) -> None:
    """★ 两个接口的 `total` **口径不同**，但差额必须对得上账。

    ★ 口径不同的理由是画法不同（详见 ShopShareOut.total 的 docstring）：
      饼图的每一块是"这个店铺占多少商品"，无价商品**同样占地方**，
      排除掉会让扇区凭空变小；而直方图的每一根柱子是
      "多少商品落在这个价位"，无价商品**落不进任何一根柱子**。

    ★ 但"口径不同"不能变成"对不上"。等式

          shop-share.total == price-distribution.total + price-distribution.price_missing

      把两者的关系变成一条**可核对的账**——这正是 priced_count 和
      price_missing 这两个"差额字段"存在的意义：
      让调用方不必读文档、也不必相信文档，就能确认手里两个数是什么关系。

    ★ 这条用例同时也是那两个字段的**存在理由**：谁删掉任一个，
      等式就写不出来，这里会红。
    ★ 实测（真实库，source=smzdm）：饼图 total=100，直方图 total=98 +
      price_missing=2 —— 今天的偏差就在界面上，不是假想。
    """
    _add(db, sid="p1", title="有价的", price=100.0, shop="甲")
    _add(db, sid="p2", title="有价的2", price=200.0, shop="甲")
    _add(db, sid="n1", title="无价的", price=None, shop="乙")
    db.commit()

    pie = _get(client, "/api/stats/shop-share", source="mock")
    hist = _get(client, "/api/stats/price-distribution", source="mock")

    assert pie["total"] == 3, "饼图的口径是含无价商品"
    assert hist["total"] == 2, "直方图的口径是只含有价商品"
    assert hist["price_missing"] == 1
    assert pie["total"] == hist["total"] + hist["price_missing"]


# ===========================================================================
# 九、接口契约：/docs 顶部那句关于 ?source= 的承诺，从接口定义里核对
# ===========================================================================


def test_the_docs_claim_about_source_filter_matches_the_actual_endpoints() -> None:
    """★ `/docs` 顶部的说明是一句**对外承诺**，它必须和接口定义一致。

    ★ 这条守的是一个已经发生过的形态：那句话原本写的是
      "**所有**统计接口都支持 `?source=`"，而 `/api/stats/sources`
      并不接受这个参数——它返回的就是"有哪些 source"，再按 source
      过滤它没有意义。于是 /docs 上的一句承诺，被它自己列出的端点否掉。

    ★ 为什么这类错值得单写一条测试：
      · 它是**纯文档**的错，没有任何运行时会报错；
      · 读的人会相信"全部"这种强断言，不会再去数一遍；
      · 而它恰好出现在面试时最可能被点开的那一页上。
      这个项目已经反复记录过"文档/配置说的和实现做的不一样"
      （docs 16.10），这是它的又一个落点。

    ★ 断言方式刻意**从接口定义推导**，而不是维护一张手写清单：
      手写清单会在下次加接口时忘记更新，而路由表不会。
      具体做两件事：
        1. 数出 `/api/stats/*` 里**不接受** `?source=` 的端点，
           断言它恰好是 `/api/stats/sources` 一个；
        2. 断言这个例外**被那句话点名写出来了**——
           只做第 1 步的话，将来新增一个例外、描述却仍说"除 X 外全部支持"，
           测试照样绿。

    ★ 诚实说明这条测试**管不到**的地方：它检查"描述里有没有提到这个例外"，
      不检查那句话的措辞是否精确。所以有人把描述改成
      "全部支持"却仍然在别处提到 `/api/stats/sources` 时，它不会红。
      要挡住那种情况只能去断言具体的措辞，而那会让改一句话就变红——
      权衡之下选择现在这样：**钉住事实，不钉住文字**。
    """
    spec = app.openapi()

    stats_paths = {p for p in spec["paths"] if p.startswith("/api/stats/")}
    assert stats_paths, "一个 /api/stats/* 端点都没找到，这条用例没测到东西"

    no_source = set()
    for path in stats_paths:
        for op in spec["paths"][path].values():
            params = {
                p["name"] for p in op.get("parameters", []) if p.get("in") == "query"
            }
            if "source" not in params:
                no_source.add(path)

    assert no_source == {"/api/stats/sources"}, (
        f"`?source=` 的例外集合变了：{sorted(no_source)}。"
        "新增/删除例外时，请同时改 /docs 顶部那段话——"
        "它承诺的是『统计接口除 X 外都支持』。"
    )

    description = spec["info"]["description"]
    for path in no_source:
        assert path in description, (
            f"{path} 不接受 ?source=，但 /docs 顶部的说明里没有点名它——"
            "读者会以为这个接口也支持数据源过滤。"
        )


def test_proxy_image_is_not_the_only_endpoint_without_source_filter() -> None:
    """★ 守 `app/main.py` 里图片代理那段注释的**前提**。

      那段注释原本写的是"本项目**唯一一个**不接受 `?source=` 的接口"。
      核实后发现不成立：单条资源、任务、健康检查、告警配置……
      它们都不按数据源过滤。于是那句话改成只陈述"本接口不接受"。

      这条测试守的就是那个改动的成立条件：只要不接受这个参数的端点
      **不止一个**，"唯一一个"这种说法就不能再写回去。
      集合真的缩小到只剩它一个的那天，这条会红——那时要同步改注释。

    ★ 为什么这条**不**去数一个具体个数：
      写那段注释时数了一遍，是 **11**；写 docs 第十九节时又数了一遍，
      是 **12**——**同一份代码**，差别只在我手工列举时看漏了一行。
      数字会随端点增减而变，钉住它等于每加一个接口就要改一次测试，
      而它并不表达任何设计意图。
      所以和上面那条一样：**钉住事实，不钉住文字。**
    """
    spec = app.openapi()

    no_source = set()
    for path, item in spec["paths"].items():
        # ★ 参数既可能写在单个操作上，也可能写在 path 级共享——
        #   两个地方都要看，否则会漏掉把参数上提过的端点。
        shared = {
            p["name"] for p in item.get("parameters", []) if p.get("in") == "query"
        }
        for method, op in item.items():
            if method == "parameters":
                continue
            params = shared | {
                p["name"] for p in op.get("parameters", []) if p.get("in") == "query"
            }
            if "source" not in params:
                no_source.add(path)

    assert "/api/proxy-image" in no_source, (
        "`/api/proxy-image` 现在接受 `?source=` 了？"
        "那段注释和 app/api/images.py 顶部都是照『它不接受』写的，"
        "接口定义变了就要一起改。"
    )
    assert len(no_source) > 1, (
        "不接受 `?source=` 的端点只剩 "
        f"{sorted(no_source)} 一个了——"
        "app/main.py 里那段注释的前提（『不止一个』）不再成立，请同步改掉。"
    )


# ===========================================================================
# 八、词云
# ===========================================================================


def test_keywords_respect_the_custom_dictionary_and_stopwords(client, db) -> None:
    """★ 这条同时验证了**两个数据文件真的被加载了**。

    userdict.txt 和 stopwords.txt 的路径是配置项，
    写错路径不会报错——只是词云变得很难看，而"难看"不是测试能发现的。
    所以这里断言具体的词在不在，等于给文件加载加了一道保险。

      · "充电宝" 必须作为**一个词**出现：不在自定义词典里的话
        jieba 会切成"充电" + "宝"，词云上就散了
      · "包邮"/"正品"/"毫安" 必须被停用词过滤掉：
        它们是营销噪声和单位，会挤掉有信息量的词
    """
    _add(db, sid="k1", title="罗马仕 充电宝 20000毫安 双向 包邮", price=99.9)
    _add(db, sid="k2", title="绿联 充电宝 10000毫安 轻薄 正品", price=59.9)
    db.commit()

    body = _get(client, "/api/stats/keywords", source="mock")
    names = {it["name"]: it["value"] for it in body["items"]}

    assert names.get("充电宝") == 2, f"「充电宝」没被当成一个词：{list(names)}"

    # ★ 只列**真正的**噪声。第一版把"轻薄"也写进这个列表了，它被报错拦下——
    #   而错的是测试：轻薄是描述产品本身的词，是有信息量的，
    #   它本来就不该进停用词表。停用词表要滤的是"营销动作"（包邮/正品）
    #   和"单位量词"（毫安），不是产品属性。
    for noise in ("包邮", "正品", "毫安"):
        assert noise not in names, f"停用词「{noise}」漏进了词云"

    assert body["total_titles"] == 2


def test_keywords_fold_ascii_case_and_display_the_human_form(client, db) -> None:
    """★ 大小写归一 + 显示回人类写法。

    不归一的话 iPhone / iphone / IPHONE 会在词云上各占一块，
    同一个概念被拆成三份（而且三块都很小，都可能被 limit 挤掉）。
    归一之后还要**显示成 iPhone**——全小写的词云一眼看去像没处理过的数据。
    """
    _add(db, sid="a", title="iPhone 保护壳 透明")
    _add(db, sid="b", title="iPhone 保护壳 磨砂")
    _add(db, sid="c", title="IPHONE 保护壳 防摔")
    db.commit()

    names = {it["name"]: it["value"] for it in _get(client, "/api/stats/keywords", source="mock")["items"]}

    # 归一到同一个词，计数是 3
    assert names.get("iPhone") == 3, f"大小写没归一：{list(names)}"
    assert "iphone" not in names and "IPHONE" not in names


def test_keywords_keep_short_ascii_model_names(client, db) -> None:
    """★★ 这一条守的是一个**差一点就写错**的过滤规则。

    早期设计里写的是"丢弃纯 ASCII 词"——听起来很合理（中文品类，
    英文词大概是噪声），但它会亲手杀掉 iPhone / WiFi / Type-C / USB，
    而它们恰恰是数码品类里信息量最高的词。

    正确规则是"ASCII 词长度 ≥ 3 才保留"：留下 USB、Type-C，
    把真正的噪声（GB、LED）交给停用词表按需处理。
    """
    _add(db, sid="a", title="USB 数据线 Type-C 快充")
    db.commit()

    names = {it["name"] for it in _get(client, "/api/stats/keywords", source="mock")["items"]}
    # ★ 这三个词如果被过滤掉，说明有人把规则改回了"丢弃纯 ASCII 词"
    assert "usb" in names or "USB" in names, f"USB 被过滤掉了：{names}"
    assert any(n.lower() == "type-c" for n in names), f"Type-C 被过滤掉了：{names}"


def test_keywords_are_ordered_deterministically(client, db) -> None:
    """★ 同频词之间必须有确定的顺序。

    `Counter.most_common()` 在计数相同时按**字典插入顺序**返回，
    而插入顺序取决于 SQL 返回标题的顺序。结果就是"给词云加个排序"
    这种无关改动会让输出悄悄变样，前端看到的词云每次刷新都在跳。
    """
    for i in range(3):
        _add(db, sid=f"a{i}", title=f"透明 磨砂 防摔 商品{i}")
    db.commit()

    first = [it["name"] for it in _get(client, "/api/stats/keywords", source="mock")["items"]]
    second = [it["name"] for it in _get(client, "/api/stats/keywords", source="mock")["items"]]
    assert first == second


# ===========================================================================
# 九、商品列表与分页
# ===========================================================================


def test_product_list_pagination_covers_every_row_exactly_once(client, db) -> None:
    """★ 翻完所有页，应该正好覆盖每一行一次：不多、不少、不重。"""
    for i in range(25):
        _add(db, sid=f"p{i:02d}", title=f"商品{i:02d}", price=100.0 + i, sales=i)
    db.commit()

    seen: list[int] = []
    for page in range(1, 4):
        body = _get(client, "/api/products", page=page, size=10, sort="id", order="asc")
        seen += [it["id"] for it in body["items"]]

    assert len(seen) == 25
    assert len(set(seen)) == 25, "同一个商品在多个页面上重复出现"


def test_filtered_pagination_total_matches_the_filter(client, db) -> None:
    """★ 计数查询的 where 条件必须和取数查询**完全一致**。

    不一致的典型症状是"总共 3 条，但翻到第 2 页是空的"，
    或者反过来——总数说有 100 条，翻到第 4 页就没了。
    这里用一个只有 5 条匹配的搜索词，断言 total 和实际能翻到的行数一致。
    """
    for i in range(20):
        _add(db, sid=f"p{i:02d}", title=f"普通商品{i:02d}", price=100.0)
    for i in range(5):
        _add(db, sid=f"k{i}", title=f"充电宝 特价{i}", price=99.0)
    db.commit()

    body = _get(client, "/api/products", q="充电宝", size=2, page=1)
    assert body["total"] == 5, f"过滤后的总数不对：{body['total']}"
    assert body["pages"] == 3

    collected: list[int] = []
    for page in range(1, 4):
        collected += [
            it["id"] for it in _get(client, "/api/products", q="充电宝", size=2, page=page)["items"]
        ]
    assert len(collected) == 5 and len(set(collected)) == 5


def test_pagination_with_tied_sort_keys_is_stable(client, db) -> None:
    """★ 排序键全部相同时，翻页结果仍然必须确定。

    ★ 关于这条测试的**诚实说明**（写在这里免得后人高估它）：

      它验证的是"可观测的契约"——同价商品在翻页时按 id 升序出现。
      但在 SQLite 上，即使**删掉**兜底排序列 `Product.id`，
      这条测试多半**照样是绿的**：SQLite 在小表上做排序时
      恰好按 rowid 顺序扫描，而 rowid 就等于 id。

      也就是说，它不是一条好的变异测试目标。

      那为什么还要留着？因为兜底列解决的是**换数据库之后**的问题：
      PostgreSQL / MySQL 会并行扫描、会选择不同的执行计划，
      同一批同值行的顺序在两次查询之间**没有任何保证**。
      到那时这条测试会立刻变成红的——它是为那次迁移准备的哨兵，
      而不是为今天的 SQLite。
      （"顺序确定"这件事，SQLite 现在只是碰巧满足，并没有承诺。）
    """
    for i in range(30):
        _add(db, sid=f"tie{i:02d}", title=f"同价商品{i:02d}", price=99.9, sales=i)
    db.commit()

    ids: list[int] = []
    for page in range(1, 4):
        ids += [
            it["id"]
            for it in _get(client, "/api/products", sort="price", order="asc", size=10, page=page)[
                "items"
            ]
        ]

    assert len(set(ids)) == 30
    # 同价时按 id 升序 —— 这就是兜底排序列带来的确定性
    assert ids == sorted(ids)


def test_products_without_a_price_sort_last_in_both_directions(client, db) -> None:
    """★★ 这条是**真正有判别力**的：没有 `nullslast` 它就会红。

    SQLite（以及所有 SQL 实现）把 NULL 当成"比任何值都小"。
    所以：
      · 升序 → 没有价格的商品排在**最前面**
      · 降序 → 排在最末尾（看起来"对了"，但只是碰巧）

    升序那条是真正的坑：用户点"价格从低到高"，
    看到的第一屏全是"价格未知"的商品，会以为价格全都没采到。
    """
    _add(db, sid="cheap", title="便宜但有价", price=10.0)
    _add(db, sid="noprice", title="没有价格", price=None)
    _add(db, sid="expensive", title="贵但有价", price=1000.0)
    db.commit()

    ascending = [it["title"] for it in _get(client, "/api/products", sort="price", order="asc")["items"]]
    assert ascending == ["便宜但有价", "贵但有价", "没有价格"], (
        f"价格升序时「没有价格」的商品应该排在最后，实际顺序是 {ascending}"
    )

    descending = [
        it["title"] for it in _get(client, "/api/products", sort="price", order="desc")["items"]
    ]
    assert descending == ["贵但有价", "便宜但有价", "没有价格"]


def test_search_box_treats_percent_as_a_literal(client, db) -> None:
    """★ LIKE 的通配符必须转义。

    用户在搜索框里输入 `%` 是很自然的事（想搜"50% 折扣"）。
    不转义的话 `%` 被 SQL 当成"匹配任意字符"，
    于是**输入一个字符就返回全表**——不报错，只是结果莫名其妙地多。
    """
    for i in range(5):
        _add(db, sid=f"p{i}", title=f"普通商品{i}", price=100.0)
    db.commit()

    assert _get(client, "/api/products", q="%")["total"] == 0, (
        "输入 % 返回了全表——LIKE 通配符没转义"
    )
    # 下划线同理（LIKE 里的单字符通配符）
    assert _get(client, "/api/products", q="_")["total"] == 0

    # 但正常搜索仍然要工作
    assert _get(client, "/api/products", q="普通商品")["total"] == 5


def test_product_list_shows_the_price_point_count(client, db) -> None:
    """★ "双数据源"叙事在界面上的落点。

    Mock 商品有 90 个价格点，真实采集的商品可能只有 1 个
    （被爆料过一次）。并排看到这两个数字，
    才看得懂"事件归并"要解决的问题是什么。
    """
    _add(db, sid="rich", title="价格点多的", price=100.0, points=5)
    _add(db, sid="poor", title="价格点少的", price=100.0, points=0)
    db.commit()

    by_title = {it["title"]: it for it in _get(client, "/api/products", sort="id")["items"]}
    assert by_title["价格点多的"]["price_point_count"] == 5
    assert by_title["价格点少的"]["price_point_count"] == 0


def test_invalid_sort_field_is_rejected(client) -> None:
    """白名单之外的排序字段一律 422，不进入 SQL。"""
    assert client.get("/api/products", params={"sort": "price; DROP TABLE products"}).status_code == 422


# ===========================================================================
# 十、商品详情与价格历史
# ===========================================================================


def test_product_detail_returns_404_for_a_missing_product(client) -> None:
    r = client.get("/api/products/99999")
    assert r.status_code == 404
    assert "不存在" in r.json()["detail"]


def test_product_detail_reports_historical_extremes_from_price_history(client, db) -> None:
    """★★ `price_max` 和"历史最高价"是两回事——这是被捕获的一个真实缺陷。

    `price_max` 是**本次成交的价格区间上限**（来自 cleaner.parse_price），
    不是历史最高价。历史最高/最低价只能从 price_history 现算。

    混用过的症状：`?source=mock` 下 price_max 是 90 天最高价，
    `?source=smzdm` 下是区间上限，于是同一张「价格区间宽度」的图
    在两批数据上算出来的是两种东西，而且不报错。
    """
    product = _add(db, sid="x", title="有历史的商品", price=100.0, points=4)
    # _add 造的点是 100/101/102/103
    db.commit()

    body = _get(client, f"/api/products/{product.id}")
    assert body["price"] == 100.0
    assert body["price_max"] == 100.0  # 区间上限，不是历史最高
    assert body["historical_min"] == 100.0
    assert body["historical_max"] == 103.0
    assert body["price_point_count"] == 4


def test_price_history_is_ordered_and_labeled(client, db) -> None:
    """★ 价格历史要按时间升序，并且**明确说出 x 轴画的是什么时间**。

    `recorded_at` 的语义随 source_channel 变化：
    精选页是爆料发布时间，类目页是采集时间，Mock 是模拟时间。
    把三种时间混在一张图上画是不诚实的，所以接口必须把它标出来，
    前端才有机会如实标注坐标轴。
    """
    product = _add(db, sid="x", title="有历史的商品", price=100.0, points=5, channel="jingxuan",
                   source="smzdm", sales=None)
    db.commit()

    body = _get(client, f"/api/products/{product.id}/price-history")
    assert body["total"] == 5
    assert body["x_axis_label"] == "爆料发布时间"

    times = [p["recorded_at"] for p in body["points"]]
    assert times == sorted(times), "价格历史没有按时间升序"

    prices = [p["price"] for p in body["points"]]
    assert body["min_price"] == min(prices)
    assert body["max_price"] == max(prices)
    assert body["notice"] is None


def test_price_history_labels_the_mock_channel(client, db) -> None:
    product = _add(db, sid="m", title="mock 商品", price=100.0, points=3, channel="mock")
    db.commit()
    assert _get(client, f"/api/products/{product.id}/price-history")["x_axis_label"] == "模拟时间"


def test_single_point_price_history_explains_itself(client, db) -> None:
    """★ 只有一个价格点时必须有解释。

    真实采集来的商品绝大多数只被爆料过一次，折线图上就是**一个孤零零的点**。
    不给解释的话，用户会以为图画坏了——而这恰恰是"事件归并"要解决的问题，
    是叙事的一部分，不能让它看起来像 bug。
    """
    product = _add(db, sid="one", title="只被爆料过一次", source="smzdm", sales=None,
                   channel="jingxuan", points=1)
    db.commit()

    body = _get(client, f"/api/products/{product.id}/price-history")
    assert body["total"] == 1
    assert body["notice"], "只有一个点时应该给出解释"
    assert "趋势" in body["notice"]


def test_price_history_truncation_keeps_the_newest_and_stays_consistent(client, db) -> None:
    """★ limit 截断时：取**最新**的点，且 total / 极值走全量口径，并且要说出来。

    这条守的是一个"同一个弹窗里两个接口自相矛盾"的缺陷。
    前端 openDetail 用 Promise.all 同时取 `/api/products/{id}` 和
    `/api/products/{id}/price-history`，渲染在**同一屏**上：

      · 前者（price_point_count / historical_min / historical_max）
        走的是 SQL 聚合，**全量**，不受 limit 影响；
      · 后者如果算在截断后的列表上，两个接口就会给出不同答案——
        KPI 卡写"价格点数 600 / 历史最高 699"，
        折线图的"最高"标记却停在 499。各自都没写错，并排才矛盾。

    原来还有两个错：排序写的是 asc 配 limit（于是取到的是**最旧**的 400 个，
    把最新的点全丢了），以及截断时 `notice` 为 None（静默）。

    600 个点这个规模是刻意的：默认 limit 是 400，必须真的超过它才算测到。
    """
    product = _add(db, sid="many", title="价格点很多", price=699.0, points=600)
    db.commit()

    detail = _get(client, f"/api/products/{product.id}")
    body = _get(client, f"/api/products/{product.id}/price-history")

    # ---- 全量口径：两个接口必须一致 ----
    assert body["total"] == detail["price_point_count"] == 600
    assert body["max_price"] == detail["historical_max"] == 699.0
    assert body["min_price"] == detail["historical_min"] == 100.0
    assert body["last_recorded_at"] == detail["last_recorded_at"]

    # ---- 窗口：默认返回 400 个，而且是**最新**的那批 ----
    assert len(body["points"]) == 400
    assert body["points"][-1]["recorded_at"] == detail["last_recorded_at"], \
        "最新的那个价格点被截掉了——趋势图丢掉近期数据等于没有用"

    # ---- 截断了就必须说 ----
    assert body["notice"], "截断是静默的：图上少一截，而没人知道"
    assert "600" in body["notice"]


def test_price_history_does_not_warn_when_nothing_was_truncated(client, db) -> None:
    """★ 上一条的对照面：没截断就不能提"截断"。

    只写上一半的话，把 notice 写成常量也能通过——那种"永远为真"的断言
    在缺陷反向变形时不会变红（这类假 GREEN 本项目栽过）。
    """
    product = _add(db, sid="few", title="价格点不多", price=100.0, points=5)
    db.commit()

    body = _get(client, f"/api/products/{product.id}/price-history")
    assert body["total"] == 5
    assert len(body["points"]) == 5
    assert body["notice"] is None


def test_price_history_returns_404_for_a_missing_product(client) -> None:
    assert client.get("/api/products/99999/price-history").status_code == 404


# ===========================================================================
# 十一、静态托管
# ===========================================================================


def test_index_page_is_served_at_root(client) -> None:
    """★ 静态挂载必须生效，而且是**在 API 路由之后**注册的。

    顺序搞反的话 `/api/...` 会被静态处理器吃掉，返回一个找不到文件的 404；
    而这个测试的顺序是反过来的——它先证明 API 能用（上面的测试），
    再证明前端也能用。
    """
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_api_routes_are_not_shadowed_by_the_static_mount(client) -> None:
    """★ 这条是上一条的配对：静态挂载在 `/`，但绝不能盖住 `/api`。

    如果哪天有人把 `app.mount("/", ...)` 挪到了 `include_router` 前面，
    这个请求会走进静态处理器，返回 404（找不到名为 "api/health" 的文件）——
    报错信息完全不会提示是挂载顺序的问题。
    """
    assert client.get("/api/health").status_code == 200
