"""并发下的两条不变量：入队去重、规则去重。

★ 本文件不能复用 conftest 的内存库：StaticPool 让所有会话共用一条连接，等于把
  操作串行化，竞态复现率为 0。所以这里自建**真实文件库**，每个请求各拿一条连接。
★ 50ms 窗口是**撑开**的（引擎级事件 `_widen`），没有 patch 被测代码、没有改判定
  条件；判据是修复前红、修复后绿（下面每条都做过变异验证）。
★ 三处同一形状，原来都是"先查后写"，修法都是把判断塞进写语句自己的 WHERE 里：
  app/api/tasks.py enqueue_crawl / app/api/alerts.py create_rule / update_rule。
  worker.py 的 claim_next_task 本来就是这个写法，从无此问题。
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

# 把语句之间的缝隙撑到多宽：50ms 够 4 个线程全部挤进同一窗口，
# 又不至于让整个测试慢到没人愿意跑（两条加起来约 0.5 秒）。
REQUEST_GAP = 0.05

# 并发几个请求。4 个就够说明问题，又比 2 个更不像偶然。
CONCURRENCY = 4


@pytest.fixture()
def race(tmp_path):
    """真实文件库 + 每请求一条新会话 + 撑宽每条语句之间的缝隙。
    ★ 必须有 `finally` 里的 `clear()`：`dependency_overrides` 挂在**全局 app
      对象**上，漏了它后面所有测试都会指向已删除的临时库（症状是别的测试文件
      莫名其妙 `no such table`）。"""
    db_path = tmp_path / "race.db"
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    # ★ 走项目自己的 PRAGMA 清单，不自己抄一份（理由见 app/db/base.py）；
    #   特别是 busy_timeout=30s——并发的写要排队，等锁的时间不能太短。
    register_sqlite_pragmas(engine)
    Base.metadata.create_all(engine)

    # 数一下撑了几次，防止测试**悄悄变成空测试**：哪天取数不再发 SELECT
    # （比如改成从缓存读），撑窗口就落空，而测试照样绿——成了假绿。
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
    ★ barrier 是必须的：不加的话线程池里第一个请求可能已跑完第 4 个才开始，
      四个请求排成一队，什么竞态都不会发生。"""
    barrier = threading.Barrier(n)

    def run(i):
        barrier.wait()
        return call(i)

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(run, range(n)))


# 入队去重


def test_concurrent_enqueue_creates_exactly_one_task(race):
    """4 个并发 POST /api/tasks/crawl → 库里只能有 1 个任务。
    ★ 修复前实测：4 个请求全部返回 200 且各有 id，库里 4 行。后果不是"多跑
      一遍"：worker 会把 4 个任务依次跑一遍，4 倍请求量，而 SMZDM 实测约
      50~100 次请求后开始拦截——连点几下采集就能把额度用光，界面上却看不出
      异常。"""
    factory, client, widened = race

    responses = _concurrently(lambda i: client.post("/api/tasks/crawl?source=smzdm"))

    assert [r.status_code for r in responses] == [200] * CONCURRENCY
    assert widened["count"] > 0, "撑窗口没生效——这条测试可能已经变成空测试"

    with factory() as db:
        rows = db.execute(select(CrawlTask.id, CrawlTask.status)).all()

    assert len(rows) == 1, f"入队去重失效：库里出现了 {len(rows)} 个任务 → {rows}"

    # ★ 断言 id 比断言行数更强：要求"没抢到的请求被告知了队列里那一个"，
    #   而不是各自返回自己编的 id；"插不进去返回 0"的实现过不了这一关。
    returned = {r.json()["id"] for r in responses}
    assert returned == {rows[0].id}, (
        f"有请求返回了别的 id：{returned}，而库里只有 #{rows[0].id}——"
        "也就是说它没有被告知'已经在队列里了'"
    )


# 规则去重


def test_concurrent_rule_creation_creates_exactly_one_rule(race):
    """4 个并发 POST 同一条规则 → 库里只能有 1 条。
    ★ 修复前实测：4 条一模一样的规则，id 1/2/3/4。
    ★ 后果：两条相同规则会对同一个价格事件**各写一条 alerts**——状态机只能防
      一条规则重复触发、防不住两条规则同时触发，而先查后写的去重恰恰制造了两条
      规则，防线自己产出了它要防的东西。"""
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

    # ★ 409 的报错必须**点名**是哪一条（和单线程那条测试同一个要求）：
    #   并发路径上很容易退化成一个"泛泛的 409"，用户不知道该去改哪一条。
    for resp in responses:
        if resp.status_code == 409:
            assert f"#{ids[0]}" in resp.json()["detail"]


def test_concurrent_edits_cannot_both_land_on_the_same_rule(race):
    """两条不同的规则被并发改成一样 → 只能有一条改成功。
    ★ 守的是**改**的路径：只挡"建成"的话，两条本来不同的规则各自改一下阈值就
      变成重复的一对——建成和改成都必须走同一处判据。
    ★ 用例形状：A（低于 99）和 B（低于 77）并发都改成"低于 88"，两者都可能各自
      查到"没有重复"（对方还没提交），然后各自改成功。"""
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
