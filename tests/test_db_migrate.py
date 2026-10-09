"""表结构补齐：`app/db/migrate.py`。

`Base.metadata.create_all()` **只建表，不加列**：models.py 每加一个字段，旧库都
缺列，运行时抛 `no such column: alerts.threshold_at_trigger`，而报错不会提示
"少跑了一次迁移"。这份测试钉三件事：迁移真能把老库补成新库、幂等（挂在启动路径
上，Web 与 worker 都会反复跑）、missing 的判定看的是库里有没有这一列。
★ 用**真实文件库**而不是内存库：内存库每次都是全新的，用它写这条测试就变成在
  测"create_all 建不建列"，会因为错误的理由变绿。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.db.migrate import MIGRATIONS, migrate
from tests.factories import add_alert, alert_rule_factory


@pytest.fixture()
def legacy_engine(tmp_path):
    """一个"老库"：alerts 表存在，但**没有**快照那两列。
    ★ 手工写 DDL 照抄老 schema：SQLite 不随便支持 DROP COLUMN，而 create_all
      建出来的表**恰恰带着新列**，那是"新库"不是"老库"。"""
    path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE alerts (
                    id INTEGER NOT NULL PRIMARY KEY,
                    rule_id INTEGER NOT NULL,
                    triggered_at DATETIME NOT NULL,
                    price_at_trigger FLOAT,
                    notified BOOLEAN NOT NULL
                )
                """
            )
        )
    return engine


def columns_of(engine, table: str) -> set[str]:
    """读一张表真实有哪些列（走 PRAGMA，和 migrate.py 用的是同一个来源）。"""
    with engine.begin() as conn:
        return {
            row[1]
            for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")
        }


def test_a_fresh_database_needs_no_migration(db) -> None:
    """全新建的库（测试夹具那种）**没有任何东西要补**。
    ★ 守一个边界：migrate 判定写错（比如把列名拼错）会**每次都返回一堆
      applied**——日志里全在说"我改了库结构"，实际什么都没改。"""
    assert migrate(db.get_bind()) == []


def test_migrating_a_legacy_database_adds_the_missing_columns(legacy_engine) -> None:
    """老库缺的两列被补齐，且**顺序和内容**都对得上 MIGRATIONS。"""
    before = columns_of(legacy_engine, "alerts")
    assert "threshold_at_trigger" not in before, "夹具没造出老库，这条用例在空转"

    applied = migrate(legacy_engine)

    after = columns_of(legacy_engine, "alerts")
    expected = {col for table, col, _ in MIGRATIONS if table == "alerts"}
    assert expected <= after, (
        f"迁移跑完了但列还是没有：缺 {sorted(expected - after)}"
    )
    # ★ 断言"这次真的加了哪些"，不只是"最后有没有"——后者在一个
    #   "每次都全量重建"的实现下也成立，而那会丢掉数据。
    assert sorted(applied) == sorted(f"alerts.{col}" for col in expected)


def test_migrating_twice_is_a_no_op(legacy_engine) -> None:
    """★ 幂等：第二次必须返回空列表。
    ★ `migrate()` 挂在 `init_database()` 上，**Web 和 worker 两个进程都会走**
      且被反复重启；不幂等会在每次启动撞 "duplicate column name"，若异常被
      吞掉，日志里永远看不出这次启动到底改没改库。"""
    first = migrate(legacy_engine)
    assert first, "第一次就没做事，这条用例没有测到幂等"

    assert migrate(legacy_engine) == []


def test_migration_keeps_the_existing_rows(legacy_engine) -> None:
    """补列**不能动数据**——这就是"为什么不做重建式迁移"的全部理由："drop 掉
    重建"会把实际触发过的记录全删掉，而且删除是静默的，跑完看起来一切正常，
    只是历史没了。"""
    with legacy_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO alerts (id, rule_id, triggered_at, price_at_trigger, notified)"
                " VALUES (1, 7, '2026-09-28 00:00:00', 89.9, 0)"
            )
        )

    migrate(legacy_engine)

    with legacy_engine.begin() as conn:
        row = conn.execute(
            text("SELECT rule_id, price_at_trigger, threshold_at_trigger FROM alerts WHERE id = 1")
        ).one()
    assert row[0] == 7
    assert row[1] == pytest.approx(89.9)
    # ★ 新列是 NULL 而不是某个默认值——迁移**故意不做数据回填**，
    #   填"当前值"只是把"不知道"伪装成"知道"（见 migrate.py 的边界说明）。
    assert row[2] is None


def test_adding_a_column_to_a_database_that_already_has_it(db, make_product) -> None:
    """已经有的列不会被重复加、更不会**清空**。
    ★ 守着一种很难看的失败：把"列存在"当成"列不存在"去 ALTER，或用 rebuild 的
      路子（拷数据、删表、重建）重来一遍，都会让**已有数据消失**——对真库来说
      消失的是实际触发过的历史。
    ★ 走真实的写库路径（工厂 → 触发记录）而不是手工 INSERT。"""
    db.expire_all()
    product = make_product(sid="m-1", title="安克 充电宝", price=89.9)
    rule = alert_rule_factory(db)(product=product, threshold=100.0)
    alert = add_alert(db, rule, price=89.9)
    assert alert.threshold_at_trigger is not None, "工厂没写快照，这条用例测不到东西"

    migrate(db.get_bind())
    db.expire_all()

    with Session(db.get_bind()) as fresh:
        row = fresh.execute(
            text(
                "SELECT threshold_at_trigger, rule_type_at_trigger"
                " FROM alerts WHERE id = :i"
            ),
            {"i": alert.id},
        ).one()
    assert row[0] == pytest.approx(100.0)
    assert row[1] == "price_below"
