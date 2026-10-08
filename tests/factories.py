"""造测试数据的共享工厂。

================================================================================
为什么单独一个文件，而不是塞进 conftest.py
================================================================================

conftest.py 里的东西只能通过**夹具**取用，不能直接 import
（pytest 不会把 `tests/` 加进 sys.path，`from conftest import x` 会报
 ModuleNotFoundError）。而有些测试需要的是一个**普通函数**——
比如 test_api_smoke.py 里 40 多个用例都写成 `_add(db, sid=..., ...)` 的形式，
把它们全部改成夹具参数，改动面比收益大得多。

放在这里之后两条路都通：
  · `from tests.factories import product_factory`  —— 直接当函数用
  · `make_product` 夹具（在 conftest.py 里）        —— 常规 pytest 写法
两者是**同一个实现**，不会分叉。

（`from tests.factories import ...` 能成立，是因为 pytest.ini 里有
  `pythonpath = .`，再加上 tests/ 有 __init__.py 所以是个包。
  `from factories import ...` 反而不行——tests/ 本身不在 sys.path 上。）

================================================================================
为什么必须只有一份
================================================================================

"造一个商品"这件事看起来简单到不值得共享，但两份实现会悄悄分叉：

    一份给 price_max 填了值，另一份留空
    → 于是"某个接口的行为"在两组测试里其实测的是两种数据
    → 某天改了 A，B 的测试红了，而原因是两份数据本来就不一样

这类问题不报错，只是让测试越来越不可信。

================================================================================
★ 夹具只 flush，不 commit —— 测"回滚"的用例必须先自己提交
================================================================================
提交时机由被测代码决定（这是对的，见 store.py 里"不在这里 commit"的说明），
所以夹具写完只 flush。

但**测回滚语义的用例必须先自己 `db.commit()`**，否则回滚会把夹具建的数据
一起抹掉，于是测出来的是"夹具的数据还在不在"，而不是"被测代码有没有留半截状态"。

这条陷阱在 tests/test_alerts.py 里踩了两次，症状完全不同、原因完全相同：
    第一次：db.get(AlertRule, id) 返回 None —— 规则整行没了
    第二次：report.rules_checked == 0   —— 规则没被查，看起来像"检查器没工作"
两次都会让人去怀疑被测代码。
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import Alert, AlertRule, PriceHistory, Product, Shop


def product_factory(db: Session):
    """返回一个 `add(...)` 函数，往测试库里插商品（含可选的店铺和价格点）。

    ★ 返回值上还挂了 `add.shop(name, platform)`，用来单独取/建一家店铺。
      挂在函数属性上而不是返回一个元组，是为了让大多数调用方
      （只想插商品的那些）可以写 `add = product_factory(db)`，
      而不必每次都 `add, _ = product_factory(db)`。
    """

    def shop_fn(name: str, platform: str = "京东") -> Shop:
        """取或建一家店铺。(name, platform) 上有唯一约束，所以必须先查。"""
        existing = db.query(Shop).filter_by(name=name, platform=platform).one_or_none()
        if existing:
            return existing
        created = Shop(name=name, platform=platform)
        db.add(created)
        db.flush()
        return created

    def add(
        *,
        sid: str,
        title: str,
        source: str = "mock",
        price: float | None = 99.9,
        sales: int | None = 100,
        category: str | None = "充电器",
        brand: str | None = "安克",
        platform: str | None = "京东",
        shop: str | Shop | None = None,
        channel: str = "mock",
        points: int = 0,
        history: list[float] | None = None,
    ) -> Product:
        """插一个商品。`points` 是给它造几个价格历史点。

        ★ `history` 是"自己指定每个价格点的值"，`points` 是"造 N 个递增的点"。
          两者同时给时以 `history` 为准。

          加这个参数是因为告警的跌幅规则需要一个**已知的历史最高价**：
          用 `points` 造出来的序列最大值是 100+n-1，而要测"相对历史最高价
          跌了 X%"就得能精确控制那个最高价是多少。
          让测试去反推 100+n-1 这种算术，测的就不是它想测的东西了。
        """
        shop_id = None
        if isinstance(shop, Shop):
            shop_id = shop.id
        elif isinstance(shop, str):
            shop_id = shop_fn(shop).id

        product = Product(
            source=source,
            source_id=sid,
            source_channel=channel,
            title=title,
            # 真实链路里 title_norm 由 cleaner 算；这里直接用小写标题就够了，
            # 测试不依赖归一化的具体结果。
            title_norm=title.lower(),
            brand=brand,
            platform=platform,
            category=category,
            price=price,
            # ★ 单值时 price_max == price（见 models.py 的语义说明）
            price_max=price,
            sales=sales,
            shop_id=shop_id,
            last_crawled_at=utcnow(),
        )
        db.add(product)
        db.flush()

        prices = history if history is not None else [100.0 + i for i in range(points)]
        for i, value in enumerate(prices):
            db.add(
                PriceHistory(
                    product_id=product.id,
                    price=value,
                    recorded_at=utcnow() - timedelta(days=len(prices) - i),
                    # ★ 必须是 NULL：price_history 的唯一键是
                    #   (product_id, source_ref)，而 SQLite 把多个 NULL
                    #   视为互不相同——所以同一个商品的多个价格点才能共存。
                    #   如果这里填一个固定的字符串，第二个点就会撞唯一约束，
                    #   而报错信息完全不会提示"是你把 source_ref 写死了"。
                    source_ref=None,
                )
            )
        db.flush()
        return product

    add.shop = shop_fn
    return add


def alert_rule_factory(db: Session):
    """返回一个 `add_rule(...)` 函数，往测试库里插告警规则。"""

    def add_rule(
        *,
        product: Product,
        rule_type: str = "price_below",
        threshold: float = 100.0,
        enabled: bool = True,
        # ★ 默认 armed：`AlertRule.state` 的模型默认值也是 armed，
        #   这里显式写一遍是为了让"规则刚建出来是什么状态"在测试里可见——
        #   它是状态机的**初始状态**，不该藏在模型的 default 里。
        state: str = "armed",
    ) -> AlertRule:
        rule = AlertRule(
            product_id=product.id,
            rule_type=rule_type,
            threshold=threshold,
            enabled=enabled,
            state=state,
        )
        db.add(rule)
        db.flush()
        return rule

    return add_rule


# ===========================================================================
# 造完之后：读状态 / 改状态。
#
# ★ 和上面两个工厂放在一起，理由是一样的：**只能有一份实现**。
#   `test_alerts.py` 和 `test_api_alerts.py` 都要读规则状态、都要造一条
#   触发记录。两边各写一份的话，某天有人修好了其中一份的取数方式
#   （比如发现少了一次 expire），另一份就继续按老写法"通过"——
#   而它验的其实是同一件事。
# ===========================================================================


def state_of(db: Session, rule: AlertRule | int) -> str:
    """从**数据库**读规则的当前状态（不信任会话里的对象）。

    ★ 为什么不直接 `rule.state`：本项目踩过那个坑——批量 UPDATE 不会把新值
      同步到会话里已有的对象上（见 docs/可行性评估.md 16.10）。
      测试如果读会话里的对象，就会在"实现已经改对了"的时候仍然失败，
      或者更糟：在"实现错了"的时候仍然通过。
    """
    rule_id = rule if isinstance(rule, int) else rule.id
    db.expire_all()
    fresh = db.get(AlertRule, rule_id)
    assert fresh is not None, f"规则 #{rule_id} 不存在了"
    return fresh.state


def set_state(db: Session, rule: AlertRule | int, state: str) -> None:
    """直接把规则的状态摆成指定值，用来构造"已经触发过"的初始局面。

    ★ 这是**摆局面**，不是被测动作：状态机的转换一律通过
      `check_alerts` 或告警接口发生，这里只负责让测试的起点可以是
      "fired"，而不必先跑一轮真实触发再改价格把它弄回去。

    ★ 用 `update(AlertRule)`（ORM 形式）而不是 `AlertRule.__table__.update()`：
      前者会顺手同步会话里的对象，后者只改库。这个差别很实在——
      用 Core 形式的话，紧接着读 `rule.state` 会拿到旧值，
      而测试会把它当成"实现没生效"。
    """
    rule_id = rule if isinstance(rule, int) else rule.id
    db.execute(update(AlertRule).where(AlertRule.id == rule_id).values(state=state))
    db.commit()


def set_price(db: Session, product: Product, price: float) -> None:
    """模拟"下一次采集刷新了价格"。

    ★ 用 UPDATE 而不是 `product.price = price`：走的就是采集管道
      （`ingest._refresh_target`）改价格的那条路。
    ★ 同上：ORM 形式的 update 才会同步会话里的对象。
    """
    db.execute(
        update(type(product)).where(type(product).id == product.id).values(price=price)
    )
    db.commit()


def add_point(db: Session, product: Product, price: float) -> PriceHistory:
    """给商品补一个价格历史点。

    ★ 用 `product_factory` 的 `history=` 参数只能在**建商品的时候**给序列；
      而"历史点数"恰恰是跌幅告警能不能算出来的前提，
      有些用例要先建商品、再决定给它几个点。

    ★ `source_ref=None` 是关键：price_history 的唯一键是
      (product_id, source_ref)，而 SQLite 把多个 NULL 视为互不相同——
      填一个固定值的话，第二个点就撞唯一约束了（见 models.py）。
    """
    point = PriceHistory(
        product_id=product.id,
        price=price,
        recorded_at=utcnow(),
        source_ref=None,
    )
    db.add(point)
    # ★ commit 而不是 flush：这个点是**测试中途**加的，
    #   而接口用的是同一个会话（autoflush=False）——不提交的话，
    #   接下来那个请求里的查询根本看不见它。
    db.commit()
    return point


def count_alerts(db: Session) -> int:
    """alerts 表的行数。

    ★ 单独一个函数是为了让每个断言都写成一个数字——
      这样"多写了一条"和"一条都没写"在失败信息里都看得见。
    """
    return int(db.scalar(select(func.count()).select_from(Alert)) or 0)


def add_alert(
    db: Session,
    rule: AlertRule | int,
    price: float = 88.0,
    *,
    snapshot: bool = True,
) -> Alert:
    """直接插一条触发记录。

    ★ 用来构造"已经触发过一次"的历史。走 `check_alerts` 当然也能造出来，
      但那要求测试先把价格和轮次都摆对——而想测的东西可能只是
      "列表接口怎么显示这条记录"。

    ★ `snapshot=True`（默认）会把**触发时的规则值**一并写进快照列，
      这才是真实触发的形状——`check_alerts` 写出来的每条记录都带快照。
      默认值是 True 而不是 False，是因为**默认该造出真实的形状**：
      默认造"加列之前的老记录"的话，几乎所有用例都会在测一条
      "库被迁移过但数据没回填"的边缘路径，而它们本来想测的不是这个。

    ★ `snapshot=False` 造的是**迁移前的老记录**（两列 NULL）。
      这个形状只应该被"NULL 兜底"那条用例用到，所以它必须显式写出来——
      写出来的时候读代码的人就知道"这里在故意造老数据"。
    """
    rule_obj = rule if isinstance(rule, AlertRule) else db.get(AlertRule, rule)
    if rule_obj is None:
        # ★ 别让它退化成"外键指向不存在的规则"然后过一个用例：
        #   SQLite 开了 foreign_keys=ON 时这会抛 IntegrityError，
        #   但错误信息只会说外键，不会说"你传错了 id"。
        raise AssertionError(f"add_alert：规则 #{rule} 不存在，测试数据摆错了")

    alert = Alert(
        rule_id=rule_obj.id,
        triggered_at=utcnow(),
        price_at_trigger=price,
        # ★ 和 app/alerts_engine.py 的触发路径写的是同一对值。
        #   两边必须一致，否则工厂造出来的"历史记录"和真实产生的
        #   记录形状不同，用工厂写的用例就测不到真实路径。
        rule_type_at_trigger=rule_obj.rule_type if snapshot else None,
        threshold_at_trigger=rule_obj.threshold if snapshot else None,
        # ★ 恒为 False：本项目没有外部推送渠道。见 models.py 与
        #   app/alerts_engine.py 里的说明。
        notified=False,
    )
    db.add(alert)
    db.commit()
    return alert
