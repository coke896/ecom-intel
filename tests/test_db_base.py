"""数据库基础设施的测试：PRAGMA 与时间约定。

这些 PRAGMA 都是 **per-connection** 的（只有 journal_mode 写进文件头），靠模块级
注册的 `connect` 监听器每次触发——挂错地方、丢掉 register 调用都会**静默失效**
（外键不校验 / busy_timeout 没了 / WAL 退回 DELETE），都不报错。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from app.db import base as base_mod
from app.db.base import SQLITE_PRAGMAS, register_sqlite_pragmas, utcnow

# ★ 在模块顶层导入模型：模型靠被导入注册进 Base.metadata，若在 create_all()
#   之后才 import，INSERT 时报 "no such table"（只在单独跑本文件时暴露）。
from app.db.models import PriceHistory


# 一、PRAGMA 真的生效（用真实文件库，因为 WAL 在内存库上不适用）


def test_pragmas_are_applied_to_a_file_database(tmp_path) -> None:
    """★ 用**真实文件库**逐项断言 PRAGMA 生效：`:memory:` 不支持 WAL，`PRAGMA
    journal_mode=WAL` 会静默返回 "memory"，用内存库这个断言就是假的。"""
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
    """★ PRAGMA 必须对**每条新连接**都生效：SQLite 这几项是**连接级**状态，
    池子关掉一条再开一条，新连接是"白纸"——监听器漏挂一次，池里的新连接就都
    没外键校验（旧连接还是好的），表现为"有时候报错有时候不报"。"""
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
    """★ 不满足于"PRAGMA 值对了"，要验证**它真的在管事**。PRAGMA 读出来是 1，
    但约束本身没建时校验照样不会发生——这里直接插一条指向不存在商品的价格
    历史断言被拒，把"外键开着"变成"脏数据插不进来"。"""
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


# 二、生产 engine 的接线（不打开真实数据库就能检查）


def test_app_engine_has_the_pragma_listener_registered() -> None:
    """★ 断言 PRAGMA 监听器确实挂在 app 的 engine 上——用 event.contains 而不是
    连上去读 PRAGMA 值：读值要打开 data/ecom.db（有副作用），且测的是"这一次
    连接"而不是接线对不对。"""
    assert event.contains(base_mod.engine, "connect", base_mod._apply_pragmas), (
        "app.db.base 的模块级 PRAGMA 注册丢了——生产库会以 SQLite 默认值运行"
        "（外键不校验、没有 busy_timeout、journal_mode 退回 DELETE）"
    )


def test_listener_is_registered_on_the_instance_not_the_class() -> None:
    """★ 监听器必须挂在 engine **实例**上，不能挂在 Engine 类上：挂到类上 PRAGMA 照样
    生效，但**会波及同进程里所有引擎**（测试用的内存库、第三方库自建的引擎都会被塞
    进我们的 PRAGMA）。
    ★ 判别语义（第一版曾写错，变异检验实得）：`event.contains(实例, ...)` 看不见类级
      注册，"对实例断言 contains 为假"判别力为零；只有 `event.contains(Engine, ...)`
      对**类级**注册返回 True，故用它做精确判别，再取新引擎的实际 PRAGMA 值做行为验证。"""
    # ① 精确判别：类级注册表里不该有我们的处理函数
    assert not event.contains(Engine, "connect", base_mod._apply_pragmas), (
        "监听器挂到 Engine 类上了——它会污染同进程内所有其他引擎"
    )

    # ② 行为验证：新建引擎的 foreign_keys 默认是 0，被污染会变成 1
    from sqlalchemy import text

    other = create_engine("sqlite://")
    with other.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 0, (
            "新建的引擎被塞进了我们的 PRAGMA——监听器挂到类上了"
        )
    other.dispose()


def test_pragma_list_covers_the_four_documented_items() -> None:
    """清单本身的内容也要钉住：四条各对应一个失败场景（见 app/db/base.py
    里的注释），删掉任何一条都会静默降低可靠性。"""
    joined = " ".join(SQLITE_PRAGMAS).lower()
    for key in ("journal_mode=wal", "synchronous=normal",
                "foreign_keys=on", "busy_timeout=30000"):
        assert key in joined, f"PRAGMA 清单里少了 {key}"


# 二之补、busy_timeout 的**到期路径**：上面几条只验证了"配置写进去了"，"等不到时
# 会怎样"从没被构造过。实测：到期抛 OperationalError: database is locked，不会永
# 远挂住；真实等待 ~32.8 秒而不是 30 秒（原因见下面那段实测记录）。


def test_a_blocked_writer_gives_up_instead_of_hanging(tmp_path) -> None:
    """拿住写锁不放，另一个连接必须**抛错**，而不是永远等下去。
    ★ 超时改短到 300 毫秒来跑（真等 30 秒会让套件慢 33 秒）——验证的是**机制**
      （到期会抛、不会挂死），不是"30 秒"这个数字（数字由上面那条钉住）。"""
    # ★ 抓的是 `sqlite3.OperationalError`，**不是** SQLAlchemy 那个：这里的
    #   连接来自 raw_connection()，是裸的 DBAPI 连接，异常不会被包装。
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


# ★ 实测（2026-09-29，跑一次 33 秒，不进套件）：用生产那份 SQLITE_PRAGMAS，
#   配置 30000ms，连接 B 等了 32.80/32.85 秒后抛错——busy_timeout 是下界。


# 三、时间约定


def test_utcnow_is_timezone_naive() -> None:
    """★ utcnow() 必须返回**不带时区**的 UTC：SQLAlchemy 把 datetime 存成
    "YYYY-MM-DD HH:MM:SS.ffffff" 字符串，其**字典序等于时间序**，ORDER BY 才对；
    混进带时区的值会多出 "...+00:00"，而 "+" 的 ASCII 码小于数字，排序会悄悄排错。"""
    now = utcnow()
    assert now.tzinfo is None, "utcnow() 返回了带时区的值，会让 ORDER BY 排错"


def test_utcnow_is_actually_utc() -> None:
    """也不能是本地时间伪装成 naive：naive 的本地时间与 UTC 在国内差 8 小时，
    用"和真正的 UTC 相差在 1 分钟内"判断，一眼可辨。"""
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
    """★ 用真实的字符串比较验证"字典序 == 时间序"这个前提——上面两条测返回值，
    这条测它带来的性质；性质被破坏时（比如给 datetime 加上 tzinfo）它会失败。"""
    times = [
        datetime(2026, 9, 28, 12, 0, 0),
        datetime(2026, 9, 28, 13, 0, 0),
        datetime(2026, 10, 1, 0, 0, 0),
        datetime(2026, 12, 31, 23, 59, 59),
    ]
    as_strings = [t.isoformat(sep=" ") for t in times]

    assert sorted(as_strings) == as_strings, "naive UTC 的字符串序不等于时间序"
