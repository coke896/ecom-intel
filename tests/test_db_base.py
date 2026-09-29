"""数据库基础设施的测试：PRAGMA 与时间约定。

================================================================================
为什么这些"配置项"值得测试
================================================================================
它们全都是 **per-connection** 的：除了 `journal_mode` 会写进数据库文件头，
其余三项每条新连接都要重新设置。也就是说它们**不是**"设一次就一劳永逸"的
配置，而是依赖那个 `connect` 事件监听器每次都被正确触发。

所以下面几种改动都会让它们**静默失效**：

  · 把 `@event.listens_for(engine, ...)` 改成 `@event.listens_for(Engine, ...)`
    （挂到类上——是社区反复出现的 bug，但 PRAGMA 本身还是会生效，
      只是会污染同进程里别人的引擎，所以更难发现）
  · 重构时把 `register_sqlite_pragmas(engine)` 那行模块级调用丢了
  · 测试夹具自己抄一份 PRAGMA 清单，然后两边分叉

失效后果：
  · `foreign_keys` 关掉 → 插入指向不存在商品的价格历史会**静默成功**
  · `busy_timeout` 没设 → worker 写入时 Web 查询立刻抛 "database is locked"
  · `journal_mode` 退回 DELETE → 读写互相阻塞，偶发超时

三种都没有报错，只是行为变差。所以必须有测试守着。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from app.db import base as base_mod
from app.db.base import SQLITE_PRAGMAS, register_sqlite_pragmas, utcnow

# ★ 在模块顶层导入模型，不要在测试函数内部导入。
#
#   表的定义是"通过被导入"注册到 Base.metadata 上的。
#   如果在测试函数里、create_all() **之后**才 import，metadata 还是空的，
#   create_all 什么也不建，然后 INSERT 时报 "no such table"。
#
#   而且这个错误有极强的迷惑性：跑整个 tests/ 时会通过
#   （别的测试文件先导入了 models），只有单独跑本文件才失败。
from app.db.models import PriceHistory


# ===========================================================================
# 一、PRAGMA 真的生效（用真实文件库，因为 WAL 在内存库上不适用）
# ===========================================================================


def test_pragmas_are_applied_to_a_file_database(tmp_path) -> None:
    """★ 在**真实文件库**上逐项断言 PRAGMA 生效。

    ★ 为什么必须用文件库而不是内存库：
      `:memory:` 不支持 WAL，`PRAGMA journal_mode=WAL` 会静默返回 "memory"。
      用内存库测的话，这个断言要么写不了，要么写了也是假的
      （这正是"测试通过但线上不一样"的经典来源）。
    """
    eng = create_engine(f"sqlite:///{(tmp_path / 't.db').as_posix()}")
    register_sqlite_pragmas(eng)

    with eng.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        # synchronous：0=OFF 1=NORMAL 2=FULL。我们要的是 NORMAL
        assert conn.execute(text("PRAGMA synchronous")).scalar() == 1
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 30000

    eng.dispose()


def test_pragmas_reapply_on_every_new_connection(tmp_path) -> None:
    """★ PRAGMA 必须对**每条新连接**都生效，不能只在第一条上生效。

    ★ 这是这套机制最容易理解错的地方：
      SQLite 的这几项 PRAGMA 是**连接级**状态，不是数据库级配置。
      连接池关掉一条再开一条，新连接是"白纸"。
      所以监听器漏挂一次，池子里的新连接就全部没有外键校验——
      而旧连接还是好的，于是表现为"有时候报错有时候不报"。

    做法：把池子清空（模拟连接被回收），再取一条新连接检查。
    """
    eng = create_engine(
        f"sqlite:///{(tmp_path / 't.db').as_posix()}",
        # 池子大小为 1，方便精确控制"换一条连接"
        pool_size=1,
        max_overflow=0,
    )
    register_sqlite_pragmas(eng)

    with eng.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1

    eng.dispose()          # 丢掉池子里所有连接

    with eng.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1, (
            "只有第一条连接设了 PRAGMA——新连接是白纸，外键校验又关掉了"
        )

    eng.dispose()


def test_foreign_keys_actually_reject_bad_rows(tmp_path) -> None:
    """★ 不满足于"PRAGMA 值对了"，要验证**它真的在管事**。

    PRAGMA 读出来是 1，但约束本身如果没建（比如 ForeignKey 写成
    ForeignKey[str] 而没指向表），校验照样不会发生。
    所以这里直接插一条指向不存在商品的价格历史，断言它被拒绝。

    ★ 这条测试的意义在于：它把"外键开着"这个配置
      变成了"脏数据插不进来"这个**可观测的行为**。
    """
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import sessionmaker

    from app.db.base import Base

    eng = create_engine(f"sqlite:///{(tmp_path / 't.db').as_posix()}")
    register_sqlite_pragmas(eng)
    Base.metadata.create_all(eng)

    Session_ = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    with Session_() as session:
        session.add(
            PriceHistory(product_id=99999, price=1.0, recorded_at=utcnow())
        )
        with pytest.raises(IntegrityError):
            session.commit()

    eng.dispose()


# ===========================================================================
# 二、生产 engine 的接线（不打开真实数据库就能检查）
# ===========================================================================


def test_app_engine_has_the_pragma_listener_registered() -> None:
    """★ 断言 PRAGMA 监听器确实挂在了 app 的 engine 上。

    ★ 为什么用 event.contains 而不是连上去读 PRAGMA 值：
      读 PRAGMA 需要打开 data/ecom.db，那会让"跑测试"产生副作用
      （文件不存在时会被创建），而且测的是"这一次连接"而不是"接线对不对"。

      event.contains(engine, "connect", handler) 直接检查事件注册表，
      没有副作用，而且**精确地**测到了我们关心的那件事：
      模块级的 `register_sqlite_pragmas(engine)` 那行调用有没有被重构弄丢。
    """
    assert event.contains(base_mod.engine, "connect", base_mod._apply_pragmas), (
        "app.db.base 的模块级 PRAGMA 注册丢了——生产库会以 SQLite 默认值运行"
        "（外键不校验、没有 busy_timeout、journal_mode 退回 DELETE）"
    )


def test_listener_is_registered_on_the_instance_not_the_class() -> None:
    """★ 监听器必须挂在 engine **实例** 上，不能挂在 Engine 类上。

    ★ 挂到类上的危害不是"PRAGMA 不生效"——它照样生效，
      而是**会波及同进程里所有引擎**：测试用的内存库、任何第三方库
      自己创建的引擎，都会被塞进我们的 PRAGMA。

    ★★ 这条测试的第一版写错了，在这里记一笔，因为它是个很典型的陷阱：

      第一版写的是
          other = create_engine("sqlite://")
          assert not event.contains(other, "connect", handler)

      看起来对，但**判别力为零**：实测 `event.contains(实例, ...)`
      根本看不见类级注册——类级注册时它返回 False，
      于是这个断言永远成立，无论监听器挂在哪。
      （变异检验发现的：把 listens_for(engine) 改成 listens_for(Engine)，
        这条测试**照样通过**。）

      实测出的语义是：
          event.contains(Engine, ...)   → 只对【类级】注册返回 True
          event.contains(实例,  ...)    → 只对【该实例】的注册返回 True
      所以下面用第一条做精确判别 + 第二条做行为验证。
    """
    # ① 精确判别：类级注册表里不该有我们的处理函数
    assert not event.contains(Engine, "connect", base_mod._apply_pragmas), (
        "监听器挂到 Engine 类上了——它会污染同进程内所有其他引擎"
    )

    # ② 行为验证：新建的引擎不该被我们的 PRAGMA 污染
    #    ★ 不满足于检查事件注册表，还要看它造成的实际后果。
    #      SQLite 的 foreign_keys 默认是 0（关），被污染会变成 1。
    from sqlalchemy import text

    other = create_engine("sqlite://")
    with other.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 0, (
            "新建的引擎被塞进了我们的 PRAGMA——监听器挂到类上了"
        )
    other.dispose()


def test_pragma_list_covers_the_four_documented_items() -> None:
    """清单本身的内容也要钉住。

    ★ 防止"重构时顺手删掉一条"：这四条各自对应一个具体的失败场景
      （见 app/db/base.py 里的注释），删掉任何一条都会静默降低可靠性。
    """
    joined = " ".join(SQLITE_PRAGMAS).lower()
    for key in ("journal_mode=wal", "synchronous=normal",
                "foreign_keys=on", "busy_timeout=30000"):
        assert key in joined, f"PRAGMA 清单里少了 {key}"


# ===========================================================================
# 二之补、busy_timeout 的**到期路径**
#
# ★ 上面几条只验证了"配置写进去了"。`busy_timeout` 是一条**承诺**：
#   "后到的写会等，而不是立刻抛 database is locked"。
#   而它的另一半 —— "等不到的时候会怎样" —— 在 2026-09-29 之前
#   **从没被构造过**：配置是 30 秒，但没人真的去等那 30 秒。
#   承诺的一半没验证，等于没有承诺。
#
# ★ 实测结论（手工构造，数字见下面那条测试的 docstring）：
#   到期后抛 `OperationalError: database is locked`，**不会永远挂住**。
#   但真正的等待时长是 ~32.8 秒，不是 30 秒 —— 原因也已查清。详见下。
# ===========================================================================


def test_a_blocked_writer_gives_up_instead_of_hanging(tmp_path) -> None:
    """拿住写锁不放，另一个连接必须**抛错**，而不是永远等下去。

    ★ 这条把超时改短到 300 毫秒来跑（设完覆盖一次 PRAGMA），
      因为真的等 30 秒会让整个测试套件慢 33 秒 ——
      那是一条没人愿意跑的测试，等于没有测试。
      代价是：它验证的是**机制**（到期会抛、不会挂死），
      不是"30 秒这个具体数字"。那个数字由上面
      `test_pragma_list_covers_the_four_documented_items` 单独钉住。
      两条合起来才等于"配置正确 + 行为正确"。
    """
    # ★ 抓的是 `sqlite3.OperationalError`，**不是** SQLAlchemy 那个。
    #   这里的连接来自 raw_connection()，是裸的 DBAPI 连接，
    #   异常不会被 SQLAlchemy 包装。第一版就是在这里红的。
    import sqlite3

    eng = create_engine(
        f"sqlite:///{(tmp_path / 'busy.db').as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    register_sqlite_pragmas(eng)
    with eng.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)")
        conn.exec_driver_sql("INSERT INTO t (id, v) VALUES (1, 0)")

    a = eng.raw_connection()
    b = eng.raw_connection()
    ra, rb = a.driver_connection, b.driver_connection
    try:
        ra.execute("BEGIN IMMEDIATE")          # A 占住写锁
        ra.execute("UPDATE t SET v = v + 1 WHERE id = 1")
        rb.execute("PRAGMA busy_timeout=300")  # 覆盖成短超时，理由见 docstring

        with pytest.raises(sqlite3.OperationalError, match="locked"):
            rb.execute("BEGIN IMMEDIATE")
    finally:
        ra.rollback()
        a.close()
        try:
            rb.rollback()
        except Exception:
            pass
        b.close()
        eng.dispose()


# ★ 下面这段是**实测记录**，不是测试（跑一次要 33 秒，不适合进套件）。
#
#   2026-09-29 手工构造，用**生产那一份** SQLITE_PRAGMAS 挂在临时库的引擎上：
#
#     PRAGMA busy_timeout = 30000 ms       ← 配置确实是 30 秒
#     PRAGMA journal_mode = wal
#     连接 B 等了 32.80 秒 / 32.85 秒（两次）后抛
#       OperationalError: database is locked
#
#   ★ 32.8 ≠ 30。多出来的 2.8 秒一开始看着像测量误差，但两次复测
#     只差 50 毫秒，是**系统性的**。短超时对照实验给出了规律：
#
#       500 ms → 实测  611 ms  (+22.3%)
#      1000 ms → 实测 1203 ms  (+20.3%)
#      2000 ms → 实测 2275 ms  (+13.8%)
#      4000 ms → 实测 4451 ms  (+11.3%)
#     30000 ms → 实测 32800 ms (+9.4%)
#
#   ★ 根因（已验证，不是猜的）：
#       SQLite 的默认 busy handler 是**按 100 毫秒一档睡**的，
#       每睡一档检查一次是否已超过 busy_timeout。
#       而 Windows 的 `Sleep(100)` 实际会睡多久？
#       实测（kernel32.Sleep 连打 50 次取平均）：
#           Sleep(10)  → 15.5 ms  (+54.7%)
#           Sleep(50)  → 62.4 ms  (+24.9%)
#           Sleep(100) → 109.4 ms (+9.4%)   ← 对上 30 秒那次的 +9.4%
#       原因是 Windows 默认计时器精度约 15.6 ms，Sleep 会向上取整到刻度。
#
#       于是：30000 ms ÷ 100 × 109.4 ms ≈ 32.8 秒  ← 与实测吻合。
#
#   ★ 这条实测的用处不是"30 秒变成了 32.8 秒"，而是它说明了一件事：
#     **busy_timeout 是个下界，不是精确的墙钟承诺。**
#     写文档/写注释时说"等 30 秒"是可以说得更准的。
# ===========================================================================


# ===========================================================================
# 三、时间约定
# ===========================================================================


def test_utcnow_is_timezone_naive() -> None:
    """★ utcnow() 必须返回**不带时区**的 UTC。

    这不是风格偏好，是 SQLite 的存储格式决定的：
    SQLAlchemy 把 datetime 存成 "YYYY-MM-DD HH:MM:SS.ffffff" 字符串，
    这个格式的**字典序等于时间序**，所以 ORDER BY / BETWEEN 都是对的。

    一旦混进带时区的值，字符串会变成 "...+00:00"，
    而 "+" 的 ASCII 码小于数字——比较
        "2026-09-28 12:00:00+00:00"  和  "2026-09-28 13:00:00"
    会得出前者更小的错误结论。于是 ORDER BY 在跨时区数据上悄悄排错。

    → 由这条测试守住"全库一致"这个前提。展示时在前端转本地时区。
    """
    now = utcnow()
    assert now.tzinfo is None, "utcnow() 返回了带时区的值，会让 ORDER BY 排错"


def test_utcnow_is_actually_utc() -> None:
    """也不能是本地时间伪装成 naive。

    ★ 用"和真正的 UTC 相差在 1 分钟内"来判断：
      naive 的本地时间与 naive 的 UTC 在国内差 8 小时，一眼可辨。
    """
    import time

    # 取两次，避免刚好跨秒边界
    before = time.time()
    now = utcnow()
    after = time.time()

    as_ts = now.replace(tzinfo=timezone.utc).timestamp()
    # 允许 5 秒余量（测试机器可能很慢）
    assert before - 5 <= as_ts <= after + 5, (
        f"utcnow() 返回的 {now} 不是 UTC（与当前 UTC 时间相差 {as_ts - (before+after)/2:.0f} 秒），"
        "在国内会差 8 小时"
    )


def test_naive_utc_strings_sort_correctly() -> None:
    """★ 用真实的字符串比较验证"字典序 == 时间序"这个前提。

    上面两条测的是 utcnow() 的返回值，这条测的是**它带来的性质**——
    即 SQLite 里 ORDER BY 确实按时间排。
    性质被破坏时（比如有人给 datetime 加上了 tzinfo），这条会更有说服力地失败。
    """
    times = [
        datetime(2026, 9, 28, 12, 0, 0),
        datetime(2026, 9, 28, 13, 0, 0),
        datetime(2026, 10, 1, 0, 0, 0),
        datetime(2026, 12, 31, 23, 59, 59),
    ]
    as_strings = [t.isoformat(sep=" ") for t in times]

    assert sorted(as_strings) == as_strings, "naive UTC 的字符串序不等于时间序"
