"""并发下的两条不变量：入队去重、规则去重。

================================================================================
★ 这个文件为什么不能用 conftest 的内存库
================================================================================
`tests/conftest.py` 的库是 `sqlite://` + **StaticPool**——所有会话复用
**同一条连接**（理由见那里的注释：内存库只有这样才活着）。
代价是它把所有操作自动串行化了。

于是在那个环境里，**"先查后写"和"一条 SQL 搞定"表现完全一样**：
两个线程用的根本是同一条连接，第二个线程的查询必然发生在
第一个线程的写之后。竞态 100% 复现不出来。

所以这个文件自己建**真实文件库**（走 QueuePool，每个请求各拿一条连接）。

================================================================================
★ 那 50 毫秒的窗口是"撑开"的，不是"造出来"的
================================================================================
两个请求真正重叠的时间窗只有微秒级。靠运气去撞它，测试就会时红时绿——
而时红时绿的测试比没有测试更糟，因为红了会被当成"这个测试不稳定"而跳过。

这里的做法是在**每两条 SQL 之间**插 50ms（引擎级事件，见 `_widen`），
把本来就存在的那条缝隙撑到能被稳定复现。

⚠️ 必须说清楚它**不是什么**：它没有 patch 被测代码，没有改判定条件，
   也没有在两个线程之间同步。被测的还是原来那条代码路径。
   判据是这两条测试**在修复前会红、修复后绿**——
   一个永远不会红的并发测试是没有价值的，所以下面每条都做了变异验证。

================================================================================
★ 同一个形状，三处
================================================================================
      app/api/tasks.py   enqueue_crawl   "这个源已经有待办了就不入队"
      app/api/alerts.py  create_rule      "一模一样的规则不建第二条"
      app/api/alerts.py  update_rule      "改完不能和别的规则撞成一对"

三处原来都是"先查一遍，没有冲突再写"，读起来都对，并发下都不成立。
修法是同一个：**把判断塞进写语句自己的 WHERE 里**。
SQLite 的写是串行的，两条语句必然一前一后执行，后到的那条会看到
前一条刚提交的行。

（第四处 worker.py 的 `claim_next_task` **本来就是**这个写法，
所以它从来没有这个问题——120 轮 × 2 个 worker 实测双赢 0 次。
它算是这三处的样板，也是"为什么另外三处该这么写"的证据。）
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base, get_db, register_sqlite_pragmas
from app.db.models import AlertRule, CrawlTask, Product
from app.main import app

# 把语句之间的缝隙撑到多宽。50ms 足够让 4 个线程全部挤进同一个窗口，
# 又不至于让整个测试慢到没人愿意跑（两条测试加起来约 0.5 秒）。
REQUEST_GAP = 0.05

# 并发几个请求。4 个就够说明问题，而且"4 条一样的规则"比"2 条"更难
# 被误读成偶然。
CONCURRENCY = 4


@pytest.fixture()
def race(tmp_path):
    """真实文件库 + 每请求一条新会话 + 撑宽每条语句之间的缝隙。

    ★ 必须有 `finally` 里的 `clear()`：`dependency_overrides` 挂在
      **全局 app 对象**上，漏了它的话，后面所有测试的数据库依赖
      都会指向这个已经被删掉的临时库——症状是别的测试文件莫名其妙
      `no such table`。test_api_alerts.py 里那份夹具也记了一笔。
    """
    db_path = tmp_path / "race.db"
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    # ★ 走项目自己的 PRAGMA 清单，不自己抄一份（理由见 app/db/base.py）。
    #   特别是 busy_timeout=30s——并发的写要排队，等锁的时间不能太短。
    register_sqlite_pragmas(engine)
    Base.metadata.create_all(engine)

    # 数一下到底撑了几次，用来防止这个测试**悄悄变成空测试**：
    # 哪天取数逻辑不再发 SELECT 了（比如改成从缓存读），
    # 撑窗口就落空了，而测试还是会绿——那就成了一个假绿。
    widened = {"count": 0}

    @event.listens_for(engine, "after_cursor_execute")
    def _widen(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip()[:6].upper() == "SELECT":
            widened["count"] += 1
            time.sleep(REQUEST_GAP)

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _get_db():
        """每个请求一条新会话（= 每个请求从池里拿一条自己的连接）。"""
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db
    try:
        yield factory, TestClient(app), widened
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _concurrently(call, n: int = CONCURRENCY) -> list:
    """让 n 个线程尽量同时执行 `call(i)`。

    ★ barrier 是必须的：不加的话，线程池里第一个请求可能已经跑完了，
      第 4 个才开始——四个请求排成一队，什么竞态都不会发生。
    """
    barrier = threading.Barrier(n)

    def run(i):
        barrier.wait()
        return call(i)

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(run, range(n)))


# ===========================================================================
# 入队去重
# ===========================================================================


def test_concurrent_enqueue_creates_exactly_one_task(race):
    """4 个并发 POST /api/tasks/crawl → 库里只能有 1 个任务。

    ★ 修复前的实测（同一个夹具）：
        [(200, id=1), (200, id=2), (200, id=3), (200, id=4)] → 库里 4 行
      四个请求**没有一个**认为自己被挡下了。

    ★ 后果不是"多跑一遍"这么轻：worker 会把 4 个任务依次跑一遍，
      也就是 4 倍的请求量。SMZDM 实测约 50~100 次请求后开始拦截，
      所以"连点几下采集"就足以把额度一次性用光。
      而界面上看起来完全正常——只是任务多跑了几轮。
    """
    factory, client, widened = race

    responses = _concurrently(lambda i: client.post("/api/tasks/crawl?source=smzdm"))

    assert [r.status_code for r in responses] == [200] * CONCURRENCY
    assert widened["count"] > 0, "撑窗口没生效——这条测试可能已经变成空测试"

    with factory() as db:
        rows = db.execute(select(CrawlTask.id, CrawlTask.status)).all()

    assert len(rows) == 1, f"入队去重失效：库里出现了 {len(rows)} 个任务 → {rows}"

    # ★ 断言 id 比断言行数更强：它同时要求"没抢到的请求被**告知**了
    #   已经在队列里的那一个"，而不是各自返回一个自己编的 id。
    #   一个"插不进去就返回 0"的实现能过分行数那一关，过不了这一关。
    returned = {r.json()["id"] for r in responses}
    assert returned == {rows[0].id}, (
        f"有请求返回了别的 id：{returned}，而库里只有 #{rows[0].id}——"
        "也就是说它没有被告知'已经在队列里了'"
    )


# ===========================================================================
# 规则去重
# ===========================================================================


def test_concurrent_rule_creation_creates_exactly_one_rule(race):
    """4 个并发 POST 同一条规则 → 库里只能有 1 条。

    ★ 修复前的实测（同一个夹具）：4 条一模一样的规则，id 1/2/3/4。
    ★ 后果：两条相同规则会对同一个价格事件**各写一条 alerts**。
      文件里写了"状态机只能防住一条规则重复触发，防不住两条规则同时触发"，
      而先查后写的去重恰恰制造了两条规则——防线自己产出了它要防的东西。
    """
    factory, client, widened = race

    with factory() as db:
        product = Product(
            source="mock",
            source_id="race-1",
            title="并发测试商品",
            price=100.0,
            platform="京东",
        )
        db.add(product)
        db.commit()
        product_id = product.id

    body = {"product_id": product_id, "rule_type": "price_below", "threshold": 99.0}
    responses = _concurrently(lambda i: client.post("/api/alerts/rules", json=body))

    assert widened["count"] > 0, "撑窗口没生效——这条测试可能已经变成空测试"

    codes = sorted(r.status_code for r in responses)
    assert codes == [200] + [409] * (CONCURRENCY - 1), (
        f"状态码组合不对：{codes}。期望恰好一个 200，其余 409"
    )

    with factory() as db:
        count = db.execute(select(func.count()).select_from(AlertRule)).scalar_one()
        ids = db.execute(select(AlertRule.id)).scalars().all()

    assert count == 1, f"规则去重失效：库里出现了 {count} 条一模一样的规则 → id={ids}"

    # ★ 409 的报错必须**点名**是哪一条（和单线程那条测试同一个要求）。
    #   并发路径上很容易退化成一个"泛泛的 409"——反正都被挡住了，
    #   但用户拿到的信息少了一截：他不知道该去改哪一条。
    for resp in responses:
        if resp.status_code == 409:
            assert f"#{ids[0]}" in resp.json()["detail"]


def test_concurrent_edits_cannot_both_land_on_the_same_rule(race):
    """两条不同的规则被并发改成一样 → 只能有一条改成功。

    ★ 这一条守的是**改**的路径，不是建的路径。
      只挡"建成"的话，防线可以被绕过：两条本来不同的规则，
      各自改一下阈值就变成一对重复的——这正是文件顶部【2】
      说"建成和改成都必须走同一处判据"的原因。

    ★ 用例形状：规则 A（低于 99）改成"低于 88"，
      规则 B（低于 77）也改成"低于 88"。并发之下，两者都可能
      各自查到"没有重复"（因为对方还没提交），然后各自改成功。
    """
    factory, client, widened = race

    with factory() as db:
        product = Product(
            source="mock",
            source_id="race-2",
            title="并发改规则的商品",
            price=100.0,
            platform="京东",
        )
        db.add(product)
        db.commit()
        product_id = product.id

        a = AlertRule(
            product_id=product_id, rule_type="price_below", threshold=99.0, state="armed"
        )
        b = AlertRule(
            product_id=product_id, rule_type="price_below", threshold=77.0, state="armed"
        )
        db.add_all([a, b])
        db.commit()
        a_id, b_id = a.id, b.id

    responses = _concurrently(
        lambda i: client.patch(
            f"/api/alerts/rules/{a_id if i % 2 == 0 else b_id}",
            json={"threshold": 88.0},
        )
    )

    assert widened["count"] > 0, "撑窗口没生效——这条测试可能已经变成空测试"

    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 200, 409, 409], (
        f"状态码组合不对：{codes}。期望两条规则各有一个请求成功、一个被挡下"
    )

    # 库里最终只能有一条阈值 88 的规则。
    with factory() as db:
        n = db.execute(
            select(func.count()).select_from(AlertRule).where(AlertRule.threshold == 88.0)
        ).scalar_one()
        rows = db.execute(select(AlertRule.id, AlertRule.threshold)).all()

    assert n == 1, f"改规则的去重失效：出现了 {n} 条阈值相同的规则 → {rows}"
