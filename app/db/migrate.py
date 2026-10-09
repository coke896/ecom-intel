"""表结构补齐 —— 这个项目没有 Alembic，这是它的最小替代品。

`Base.metadata.create_all()` 只建不存在的表，**不给已存在的表加列**——所以
models.py 每加一个字段，对跑过一轮的库都不可见，症状是运行时抛
"no such column: alerts.threshold_at_trigger"，而它不会告诉你"少跑了一次迁移"。

只支持**加可空列**；改列类型 / 加约束 / 删列 / 数据回填一律不支持，需要时该换
Alembic。（不回填是故意的：老数据没有快照值就是没有，填一个"当前值"进去只会把
"不知道"伪装成"知道"。）

★ 幂等 + 挂在两个进程都会走的启动路径（init_database()）上是刻意的：
  "忘了跑迁移"是一种很难查的失败，让它在启动时自动补齐，就没有"忘"这个状态。
"""

from __future__ import annotations

from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

__all__ = ["migrate", "MIGRATIONS"]


# 每一项是 (表名, 列名, 列类型)。**全部是字面量**，不接受外部输入——
# 下面的实现是拼 SQL，不是参数化查询（DDL 不支持参数绑定）。
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("alerts", "rule_type_at_trigger", "VARCHAR(32)"),
    ("alerts", "threshold_at_trigger", "FLOAT"),
)


def _add_column_if_missing(engine: Engine, table: str, column: str, ddl_type: str) -> bool:
    """给表加一个可空列，已经有了就什么都不做。

    返回"这次真的加了吗"——调用方要靠它写日志和断言幂等。
    ★ 不用 create_all 的 checkfirst：那个参数管"表存不存在"，它对已存在的表
      整个跳过，所以正是它把缺列这件事藏了起来。
    """
    with engine.begin() as conn:
        # PRAGMA 不能用参数绑定，表名只能是字面量（见上面 MIGRATIONS 的说明）
        existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
        if column in existing:
            return False
        try:
            conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
        except OperationalError as exc:
            # ★ 多进程同时启动时会撞上：uvicorn 和 worker 各自跑一遍
            #   init_database()，两个都发现缺列、都去 ALTER，后一个拿到
            #   "duplicate column name"。那不是错误——"列已经存在"正是想要的结果。
            #   但只吞这一种，别的（库被锁死、磁盘满）必须照抛：
            #   吞掉全部异常会让"迁移根本没跑成"变成一件静默的事。
            if "duplicate column name" not in str(exc).lower():
                raise
            return False
    return True


def migrate(engine: Engine) -> list[str]:
    """把库里缺的列补齐，返回**这次真的做了**的动作（幂等）。

    ★ 返回列表而不是 None：启动日志要写清"这次启动改了什么"，测试也要能断言
      "第二次调用什么都没做"。一个静默的迁移函数，和一个静默失效的缓存
      是同一种东西。
    """
    applied: list[str] = []
    for table, column, ddl_type in MIGRATIONS:
        if _add_column_if_missing(engine, table, column, ddl_type):
            applied.append(f"{table}.{column}")
    return applied
