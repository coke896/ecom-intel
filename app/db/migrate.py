"""表结构补齐 —— 这个项目没有 Alembic，这是它的最小替代品。

================================================================================
为什么需要这个文件
================================================================================

`Base.metadata.create_all()` 只做一件事：**建不存在的表**。

它**不会**给已经存在的表加列。所以 `models.py` 里每加一个字段，
对已经跑过一轮的库都是不可见的——症状是运行时抛

    sqlite3.OperationalError: no such column: alerts.threshold_at_trigger

而这句话**不会**告诉你"少跑了一次迁移"。它会让你去查模型定义、
查导入顺序、查是不是有两个 models 文件（这类排查我在这项目里干过不止一次），
最后才发现：库还是旧的。

================================================================================
为什么不是 Alembic
================================================================================

因为只有**一次**真实的表结构变更，和**一个**部署（本机开发库）。
为此引入 Alembic 的代价是：多一个依赖、多一个 `alembic/` 目录、
每次改字段多一道 `revision --autogenerate` 的工序，
而它在面试里能讲的只有"我知道有这个东西"。

但这个文件本身能讲的事更实在：
**"我意识到 create_all 不迁移，而我的库里有真实采集数据不能重建——
所以我给'加可空列'这一类变更写了一条幂等路径，并写明了它管不了哪些变更。"**

================================================================================
边界的划法（这个文件**不**做什么）
================================================================================

只支持 **加可空列**。以下一律不支持，一旦需要就该换 Alembic：

  · 改列类型 / 加约束 / 改非空 —— SQLite 改列要重建整张表，
    而重建意味着"拷数据、换名、删旧表"，那是一整套逻辑，
    不是这个文件该长成的东西
  · 删列
  · 数据回填（**故意不做**：老数据没有快照值就是没有，
    填一个"当前值"进去只会把"不知道"伪装成"知道"——
    见 models.Alert 里那段关于 NULL 的说明）

★ 幂等 + 挂在两个进程都会走的启动路径上（`init_database()`），这两点是刻意的：
  "忘了跑迁移"是一种很难查的失败（报的是缺列，不是缺迁移），
  而让它在启动时自动补齐，就没有"忘"这个状态。
"""

from __future__ import annotations

from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

__all__ = ["migrate", "MIGRATIONS"]


# 每一项是 (表名, 列名, 列类型)。**全部是字面量**，不接受外部输入——
# 下面的实现是拼 SQL，不是参数化查询（DDL 不支持参数绑定）。
#
# ★ 这张表同时也是"这个库被改动过几次结构"的清单。
#   它只有两项，因为项目只做过这一次结构变更——
#   而这个数字本身就是"为什么还没上 Alembic"的答案。
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("alerts", "rule_type_at_trigger", "VARCHAR(32)"),
    ("alerts", "threshold_at_trigger", "FLOAT"),
)


def _add_column_if_missing(engine: Engine, table: str, column: str, ddl_type: str) -> bool:
    """给表加一个可空列，已经有了就什么都不做。

    返回"这次真的加了吗"——调用方要靠它写日志和断言幂等。

    ★ 为什么不用 `Base.metadata.create_all()` 的 `checkfirst`：
      那个参数管的是"表存不存在"，不是"列存不存在"。
      它对已存在的表**整个跳过**，所以正是它把缺列这件事藏了起来。
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
            #   init_database()，两个都发现缺列、都去 ALTER，
            #   后一个拿到 "duplicate column name: xxx"。
            #   那不是错误——"列已经存在"正是我们想要的结果。
            #   但只吞这一种，别的（比如库被锁死、磁盘满）必须照抛：
            #   吞掉全部异常会让"迁移根本没跑成"变成一件静默的事。
            if "duplicate column name" not in str(exc).lower():
                raise
            return False
    return True


def migrate(engine: Engine) -> list[str]:
    """把库里缺的列补齐，返回**这次真的做了**的动作。

    ★ 返回列表而不是 None：启动日志要写清"这次启动改了什么"，
      测试也要能断言"第二次调用什么都没做"（幂等）。
      一个静默的迁移函数，和一个静默失效的缓存是同一种东西。
    """
    applied: list[str] = []
    for table, column, ddl_type in MIGRATIONS:
        if _add_column_if_missing(engine, table, column, ddl_type):
            applied.append(f"{table}.{column}")
    return applied
