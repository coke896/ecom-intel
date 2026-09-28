"""pytest 共享夹具。

★ 为什么测试要用【独立的数据库】而不是 data/ecom.db：

  测试会调用 seed()，而 seed() 的第一件事是**清空 source='mock' 的数据**。
  如果测试连的是生产库，跑一次 pytest 就把你手工灌的演示数据全删了，
  然后你会在浏览器里看到一个空页面，还以为是前端坏了。

  这类"测试污染开发数据"的问题很隐蔽，因为它不报错，只是让数据消失。
  从第一天就把测试隔离到独立库，比事后排查便宜得多。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base


@pytest.fixture()
def db() -> Session:
    """一个干净的、独立于生产库的会话。

    ★ 用内存库 + StaticPool：
      SQLite 的 `:memory:` 每个连接都是一个独立的数据库，
      默认连接池会为每次取连接新建一个空库，导致"建完表就找不到表"。
      StaticPool 让所有操作复用同一个连接，内存库才能正常工作。

    ★ PRAGMA 与生产环境保持一致（尤其是 foreign_keys=ON）：
      不打开外键校验的话，测试里插进去的脏数据不会被发现，
      而生产环境会直接报错——"测试通过但线上失败"的最常见来源。
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
