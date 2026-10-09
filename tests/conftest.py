"""pytest 共享夹具。

测试一律用独立的内存库，不连 data/ecom.db：测试会调用 seed()，而 seed() 第一
件事就是清空 source='mock' 的数据——连生产库跑一次 pytest 会把手工灌的演示
数据全删掉，且不报任何错。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base, register_sqlite_pragmas
from tests.factories import product_factory

# 必须在模块顶层导入：models.py 靠被导入把表注册到 Base.metadata，少了它
# create_all() 什么也不建，第一条 INSERT 就报 "no such table: price_history"。
# 这个坑只在单独跑某个测试文件时出现（跑整个 tests/ 时别的模块会顺带导入 models）。
import app.db.models  # noqa: F401  （导入即注册）


@pytest.fixture()
def engine() -> Iterator[Engine]:
    """一个干净的、独立于生产库的引擎。
    ★ 必须用内存库 + StaticPool：SQLite 的 `:memory:` 每个连接都是独立数据库，
      默认连接池会新建空库，导致"建完表就找不到表"。
    ★ PRAGMA 复用 app/db/base.py 的 register_sqlite_pragmas，不自己抄一份：
      抄一份两边会悄悄分叉，症状是"测试全绿但线上外键不生效"。"""
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


# 造数据的共享工厂：实现放在 tests/factories.py（conftest 夹具与直接 import 两种用法共用）。


@pytest.fixture()
def make_product(db: Session):
    """夹具形式，转发到 tests/factories.py 的 product_factory。"""
    return product_factory(db)
