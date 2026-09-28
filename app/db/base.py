"""数据库基础设施：engine、PRAGMA、会话工厂、时间约定。

这个文件里的每一行几乎都对应一个踩过的坑，注释比代码重要。
"""

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

    用 SQLAlchemy 2.0 的 DeclarativeBase 风格（而不是 1.x 的 declarative_base()）。
    配套的是 Mapped[] + mapped_column() 的类型化写法——
    好处是 mypy / IDE 能真正推断出字段类型，写错属性名当场报错。
    """


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------

engine = create_engine(
    settings.database_url,
    echo=False,
    # check_same_thread=False：
    #   SQLite 默认禁止跨线程复用连接。FastAPI 的同步依赖（get_db）可能在线程池
    #   的不同线程里执行，所以必须关掉这个检查。真正的并发安全由
    #   WAL + busy_timeout 保证（见下面的 PRAGMA），而不是靠这个开关。
    #
    # timeout=30：
    #   拿到连接后等待写锁的最长时间（秒）。和下面的 busy_timeout 是两层保险。
    connect_args={"check_same_thread": False, "timeout": 30},
)


# ---------------------------------------------------------------------------
# PRAGMA
# ---------------------------------------------------------------------------

# ★ 这份清单是【唯一一份】，生产 engine 和测试夹具都用它。
#
#   为什么要抽出来：这几项 PRAGMA 都是 **per-connection** 的
#   （只有 journal_mode 会写进文件头持久化，其余三项每条新连接都要重设）。
#   如果测试夹具自己抄一份，两边就会悄悄分叉——
#   典型症状是"测试全绿，线上外键不生效"。
#
#   验证方式：tests/test_db_base.py 在**真实文件库**上逐项断言这四条。
#   （不能用内存库验证 journal_mode：`:memory:` 不支持 WAL，会静默退回 memory 模式。）
SQLITE_PRAGMAS: tuple[str, ...] = (
    # WAL（Write-Ahead Logging）：读写互不阻塞。
    # 默认的 DELETE 模式下，一个写事务会锁住整个库，读请求全部排队。
    # 本项目有「worker 持续写入 + Web 端持续查询」的并发场景，必须开 WAL。
    "PRAGMA journal_mode=WAL",

    # WAL 模式下的安全/性能平衡点。
    # FULL 每次提交都 fsync（慢）；OFF 可能丢数据；NORMAL 在 WAL 下
    # 只在 checkpoint 时 fsync，崩溃最多丢最近几个事务，
    # 对"采集到的商品数据"这个量级的损失完全可以接受。
    "PRAGMA synchronous=NORMAL",

    # ★ SQLite 默认 **不校验外键**！这是个历史遗留的兼容性决定。
    #   不显式打开的话，REFERENCES 只是注释——插入一个指向不存在商品的
    #   price_history 会静默成功。必须打开。
    "PRAGMA foreign_keys=ON",

    # 等锁超时（毫秒）。两个进程同时写时，后到的会等而不是立刻
    # 抛 "database is locked"。30 秒足够覆盖本项目最长的批量写入。
    "PRAGMA busy_timeout=30000",
)


def register_sqlite_pragmas(target: Engine) -> Callable[..., None]:
    """把 PRAGMA 清单挂到 **engine 实例** 的 connect 事件上，返回处理函数。

    ★ 两个容易踩的点：

    坑 1：监听器必须挂在 **engine 实例** 上，不能挂 Engine 类。
        写成 @event.listens_for(Engine, "connect") 会波及同一进程里
        所有 SQLAlchemy 引擎——包括测试用的内存库、以及任何第三方库
        自己创建的引擎。症状是测试里出现莫名其妙的 WAL 文件，
        或者别人的连接被塞进了我们的 PRAGMA。这是社区反复出现的 bug。

    坑 2：必须在建立任何连接 **之前** 注册。
        connect 事件只在新建物理连接时触发；如果 engine 已经连过数据库，
        后续的连接走的是连接池，不会再触发这个回调。
        所以调用它的代码必须放在模块顶层、紧跟 engine 定义之后。

    ★ 返回处理函数是为了让测试能断言"监听器确实挂上了"：
        `event.contains(engine, "connect", handler)`。
      不返回的话，就没法在不打开真实数据库的前提下检查这件事。
    """

    @event.listens_for(target, "connect")
    def _apply_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
        cur = dbapi_connection.cursor()
        for statement in SQLITE_PRAGMAS:
            cur.execute(statement)
        cur.close()

    return _apply_pragmas


# 模块顶层立刻注册（坑 2：必须在任何连接建立之前）
_apply_pragmas = register_sqlite_pragmas(engine)


# ---------------------------------------------------------------------------
# 会话工厂
# ---------------------------------------------------------------------------

SessionLocal = sessionmaker(
    bind=engine,
    # autoflush=False：
    #   默认行为是「每次查询前先把待写入的对象 flush 到数据库」。
    #   在多线程/异步混杂的场景里，这会在你意想不到的时刻触发 UPDATE，
    #   产生难以复现的竞态。我们显式控制 flush 时机。
    autoflush=False,
    # expire_on_commit=False：
    #   默认行为是「commit 后让所有对象属性过期」，下次访问时重新查库。
    #   在 FastAPI 里表现为：把 ORM 对象转成响应 JSON 时触发额外查询，
    #   如果会话已经关闭，直接抛 DetachedInstanceError。
    #   关掉它，commit 后对象仍然可用。
    expire_on_commit=False,
)


def get_db() -> Iterator[Session]:
    """FastAPI 依赖注入：每个请求一个会话，请求结束自动关闭。

    写成生成器而不是返回 Session，是为了让 FastAPI 能在请求结束时
    执行 finally 里的 close()——包括请求抛异常的情况。
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 时间约定
# ---------------------------------------------------------------------------


def init_database() -> None:
    """确保运行时目录存在并建表（幂等）。

    ★ 这个函数放在 db 层而不是 app/main.py 里，是因为**采集 worker 也要用**。

      它原来长在 main.py 里，而 main.py 是 FastAPI 应用的入口——
      `python -m scraper.worker` 要是为了建表去 import 它，
      就会把 FastAPI、全部 APIRouter、CORS 中间件一起拖进采集进程。
      那正好和"采集层独立于 Web 层"的设计相反，而且很难被发现：
      程序照样能跑，只是采集进程莫名其妙地加载了半个 Web 框架。

    ★ `import app.db.models` 必须是【函数内】的延迟导入。
      因为 models.py 要 `from app.db.base import Base`，
      在 base.py 顶层反过来 import 它就是一个循环导入。
      延迟到调用时再导，循环自然解开。

    ★ 这一行不能删，也不能挪。表的定义写在 app/db/models.py，
      它是**通过被导入**才把表注册到 Base.metadata 上的；
      没人导入它，create_all() 就是个空操作，
      然后在第一条 INSERT 时报 "no such table: price_history"。
      同一个陷阱在 tests/conftest.py 里也记了一笔——
      那次的表现更刁钻："单独跑某个测试文件才失败"。
    """
    import app.db.models  # noqa: F401  （导入即注册，见上面的说明）

    settings.ensure_dirs()
    Base.metadata.create_all(engine)

    # ★ 光有 create_all 不够：它只建不存在的**表**，不给已存在的表加**列**。
    #   所以 models.py 每加一个字段，老库都需要这里补一刀（见 app/db/migrate.py）。
    #   放在这个函数里，是因为它**两个入口都会走**：
    #   Web 进程（app/main.py）和采集 worker（scraper/worker.py）——
    #   只挂在其中一个上的话，另一个先启动就会报"no such column"。
    applied = migrate(engine)
    if applied:
        logger.warning("已补齐库结构：%s", "、".join(applied))


def utcnow() -> datetime:
    """全库统一的时间函数：不带时区的 UTC。

    ★ 为什么去掉 tzinfo：
      SQLite 没有原生的日期类型，SQLAlchemy 把 DateTime 存成
      "YYYY-MM-DD HH:MM:SS.ffffff" 字符串。这个格式有个很好的性质——
      字典序 == 时间序，所以 ORDER BY 和 BETWEEN 都是对的。

      但如果存的是带时区的 datetime，字符串会变成
      "2026-09-28 05:32:11.123456+00:00"，多出的 "+00:00" 后缀
      会让字典序和时间序脱钩：比较 "2026-09-28 12:00:00+00:00"
      和 "2026-09-28 13:00:00" 时，前者反而更小。
      结果就是 ORDER BY 在跨时区数据上悄悄排错。

      所以：**入库一律不带时区的 UTC，展示时在前端转本地时区。**
      这个约定必须全项目一致，混用会出上面那种隐蔽 bug。

    命名成 utcnow 而不是 now，是为了让调用处一眼看出拿到的是 UTC 而非本地时间。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
