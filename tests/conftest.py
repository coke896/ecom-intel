"""pytest 共享夹具。

★ 为什么测试要用【独立的数据库】而不是 data/ecom.db：

  测试会调用 seed()，而 seed() 的第一件事是**清空 source='mock' 的数据**。
  如果测试连的是生产库，跑一次 pytest 就把你手工灌的演示数据全删了，
  然后你会在浏览器里看到一个空页面，还以为是前端坏了。

  这类"测试污染开发数据"的问题很隐蔽，因为它不报错，只是让数据消失。
  从第一天就把测试隔离到独立库，比事后排查便宜得多。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base, register_sqlite_pragmas

# ★ 这一行看着像"没用到所以可以删"，但删了会出事——它必须在模块顶层：
#
#   表的定义写在 app/db/models.py 里，而 models.py 是**通过被导入**
#   来把表注册到 Base.metadata 上的。没人导入它，Base.metadata 就是空的，
#   create_all() 什么也不建，然后在第一条 INSERT 时报
#   "no such table: price_history"。
#
#   ★ 这个 bug 的可怕之处是它**只在你单独跑某个测试文件时才出现**：
#     跑整个 tests/ 时，pytest 收集阶段会导入 test_price_history.py，
#     它 import 了 app.db.models，于是表被注册了，所有测试都绿。
#     而 `pytest tests/test_db_base.py` 就红——表现是"改了无关的东西
#     导致测试挂了"，你会去怀疑代码，实际是导入顺序。
#
#   放在 conftest 里，保证【任何】测试跑之前 metadata 都是完整的。
import app.db.models  # noqa: F401  （导入即注册，见上面的说明）


@pytest.fixture()
def engine() -> Iterator[Engine]:
    """一个干净的、独立于生产库的引擎。

    ★ 用内存库 + StaticPool：
      SQLite 的 `:memory:` 每个连接都是一个独立的数据库，
      默认连接池会为每次取连接新建一个空库，导致"建完表就找不到表"。
      StaticPool 让所有操作复用同一个连接，内存库才能正常工作。

    ★ PRAGMA 复用 app/db/base.py 的 register_sqlite_pragmas，不自己抄一份。
      抄一份的话两边会悄悄分叉，症状是"测试全绿但线上外键不生效"——
      "测试通过、线上失败"的最常见来源。

      （`PRAGMA journal_mode=WAL` 在 `:memory:` 上是空操作，
        SQLite 会静默保持 memory 模式，不会报错。
        所以同一份清单对内存库和文件库都是安全的。
        WAL 本身在 tests/test_db_base.py 里用真实文件库验证。）
    """
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    register_sqlite_pragmas(eng)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine: Engine) -> Iterator[Session]:
    """一个绑定到测试引擎的会话。"""
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
