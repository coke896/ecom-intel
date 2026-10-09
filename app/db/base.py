"""数据库基础设施：engine、PRAGMA、会话工厂、时间约定。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings
from app.db.migrate import migrate

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。

    用 SQLAlchemy 2.0 的 DeclarativeBase + Mapped[] / mapped_column() 类型化写法，
    mypy / IDE 能真正推断字段类型，写错属性名当场报错。
    """


# ---- engine ----

engine = create_engine(
    settings.database_url,
    echo=False,
    # check_same_thread=False：SQLite 默认禁止跨线程复用连接，而 FastAPI 的同步
    #   依赖（get_db）可能跑在线程池的不同线程里。真正的并发安全由下面的
    #   WAL + busy_timeout 保证，不是靠这个开关。
    # timeout=30：拿到连接后等写锁的最长秒数，和 busy_timeout 是两层保险。
    connect_args={"check_same_thread": False, "timeout": 30},
)


# ---- PRAGMA ----

# 这份清单是【唯一一份】，生产 engine 和测试夹具共用。这几项都是 per-connection
# 的（只有 journal_mode 会写进文件头持久化），各抄一份会悄悄分叉——
# 典型症状是"测试全绿，线上外键不生效"。
SQLITE_PRAGMAS: tuple[str, ...] = (
    # WAL：读写互不阻塞；本项目是 worker 持续写入 + Web 持续查询，必须开。
    "PRAGMA journal_mode=WAL",

    # WAL 下的安全/性能平衡点：崩溃最多丢最近几个事务，对本项目完全可以接受。
    "PRAGMA synchronous=NORMAL",

    # ★ SQLite 默认不校验外键，不开的话 REFERENCES 只是注释，非法引用会静默插入成功。
    "PRAGMA foreign_keys=ON",

    # 等锁超时（毫秒）：两个进程同时写时排队等，而不是立刻抛 database is locked。
    "PRAGMA busy_timeout=30000",
)


def register_sqlite_pragmas(target: Engine) -> Callable[..., None]:
    """把 PRAGMA 清单挂到 **engine 实例** 的 connect 事件上，返回处理函数。

    ★ 必须挂实例、不能挂 Engine 类（挂类会波及进程内所有引擎，包括测试库），
      且必须在建立任何连接**之前**注册（connect 事件只在新物理连接时触发，
      之后走连接池不会再触发）。返回处理函数是为了让测试能断言"确实挂上了"。
    """

    @event.listens_for(target, "connect")
    def _apply_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
        cur = dbapi_connection.cursor()
        for statement in SQLITE_PRAGMAS:
            cur.execute(statement)
        cur.close()

    return _apply_pragmas


# 模块顶层立刻注册（必须早于任何连接建立）
_apply_pragmas = register_sqlite_pragmas(engine)


# ---- 会话工厂 ----

SessionLocal = sessionmaker(
    bind=engine,
    # autoflush=False：默认「查询前先 flush 待写入对象」，会在意想不到的时刻
    #   触发 UPDATE、产生难复现的竞态。我们显式控制 flush 时机。
    autoflush=False,
    # expire_on_commit=False：默认 commit 后对象属性全部过期，转响应 JSON 时
    #   会触发额外查询，若会话已关闭则抛 DetachedInstanceError。
    expire_on_commit=False,
)


def get_db() -> Iterator[Session]:
    """FastAPI 依赖注入：每个请求一个会话，请求结束（含异常）自动关闭。

    写成生成器而不是直接返回 Session，是为了让 FastAPI 能执行 finally 里的 close()。
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---- 时间约定 ----


def init_database() -> None:
    """确保运行时目录存在并建表（幂等），顺手补齐老库缺的列。

    ★ 放在 db 层而不是 app/main.py：采集 worker 也要建表，不能为此 import
      应用入口（否则会把 FastAPI 和全部路由拖进采集进程）。
    ★ `import app.db.models` 必须在函数内延迟导入：models 依赖 base，
      顶层反过来 import 会循环导入；这一行也不能删——表靠"被导入"注册到
      Base.metadata，没人导入它，create_all() 就是空操作，第一条 INSERT
      就报 "no such table: price_history"。
    """
    import app.db.models  # noqa: F401  （导入即注册，见 docstring）

    settings.ensure_dirs()
    Base.metadata.create_all(engine)

    # create_all 只建表、不加列，所以每次启动都补一刀（见 app/db/migrate.py）。
    # 两个入口（Web / worker）都会走到这里——只挂一个的话，另一个先启动就报 no such column。
    applied = migrate(engine)
    if applied:
        logger.warning("已补齐库结构：%s", "、".join(applied))


def utcnow() -> datetime:
    """全库统一的时间函数：不含时区的 UTC。

    ★ 必须去掉 tzinfo：SQLite 把 DateTime 存成字符串，不带时区时字典序 ==
      时间序；带 "+00:00" 后缀会让两者脱钩，ORDER BY 悄悄排错。
      约定：入库一律无时区 UTC，展示时在前端转本地时区。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
