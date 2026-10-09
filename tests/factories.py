"""造测试数据的共享工厂。

放在独立文件是为了两种用法共用同一实现：直接 `from tests.factories import ...`
当函数调，或经 conftest.py 的 `make_product` 夹具用（conftest 的东西只能通过
夹具取用）。
★ 夹具只 flush 不 commit（提交时机由被测代码决定，见 store.py）；但**测回滚
语义的用例必须先自己 `db.commit()`**，否则回滚会把夹具建的数据一并抹掉
（test_alerts.py 踩过两次）。
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import Alert, AlertRule, PriceHistory, Product, Shop


def product_factory(db: Session):
    """返回一个 `add(...)` 函数，往测试库里插商品（含可选的店铺和价格点）。
    ★ 返回值上挂了 `add.shop(name, platform)` 用来单独取/建店铺——挂在函数属性
      上，调用方就不必写成 `add, _ = product_factory(db)`。"""

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
        ★ `history` 自己指定每个点的值，与 `points` 同时给时以它为准——告警跌幅
          规则需要精确控制**历史最高价**，而 points 造的序列最大值是 100+n-1。"""
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
            # 真实链路里 title_norm 由 cleaner 算；测试不依赖归一化结果。
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
                    # ★ 必须是 NULL：唯一键 (product_id, source_ref) 上，
                    #   SQLite 把多个 NULL 视为互不相同，多个价格点才能共存；
                    #   填固定值的话第二个点就撞唯一约束。
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
        # ★ 默认 armed，与 `AlertRule.state` 的模型默认值一致；显式写出来
        #   让状态机的**初始状态**在测试里可见，不藏在模型的 default 里。
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


# 造完之后：读状态 / 改状态。★ 和工厂放一起的理由同样是**只能有一份实现**——
# test_alerts.py 与 test_api_alerts.py 都读状态、都造记录，各写一份就会悄悄分叉。


def state_of(db: Session, rule: AlertRule | int) -> str:
    """从**数据库**读规则的当前状态（不信任会话里的对象）——不能直接 `rule.state`：
    批量 UPDATE 不会把新值同步到会话里已有的对象上（见 docs/可行性评估.md 16.10），
    读会话会读出假绿或假红。"""
    rule_id = rule if isinstance(rule, int) else rule.id
    db.expire_all()
    fresh = db.get(AlertRule, rule_id)
    assert fresh is not None, f"规则 #{rule_id} 不存在了"
    return fresh.state


def set_state(db: Session, rule: AlertRule | int, state: str) -> None:
    """直接把规则的状态摆成指定值，用来构造"已经触发过"的初始局面——这是**摆局面**，
    不是被测动作：状态机的转换一律通过 `check_alerts` 或告警接口发生。
    ★ 用 `update(AlertRule)`（ORM 形式）而不是 `AlertRule.__table__.update()`：
      前者会同步会话里的对象，后者只改库——用 Core 形式紧接着读 `rule.state`
      会拿到旧值，测试会把它当成"实现没生效"。"""
    rule_id = rule if isinstance(rule, int) else rule.id
    db.execute(update(AlertRule).where(AlertRule.id == rule_id).values(state=state))
    db.commit()


def set_price(db: Session, product: Product, price: float) -> None:
    """模拟"下一次采集刷新了价格"——用 UPDATE 而不是 `product.price = price`：
    走的就是采集管道（`ingest._refresh_target`）改价格的那条路，ORM 形式才会
    同步会话对象。"""
    db.execute(
        update(type(product)).where(type(product).id == product.id).values(price=price)
    )
    db.commit()


def add_point(db: Session, product: Product, price: float) -> PriceHistory:
    """给商品补一个价格历史点。
    ★ `history=` 只能在**建商品的时候**给序列，而"历史点数"是跌幅告警能不能算出
      来的前提——有些用例要先建商品、再决定加几个点。
    ★ `source_ref=None` 是关键：唯一键 (product_id, source_ref) 上 SQLite 把多个
      NULL 视为互不相同，填固定值第二个点就撞唯一约束（见 models.py）。"""
    point = PriceHistory(
        product_id=product.id,
        price=price,
        recorded_at=utcnow(),
        source_ref=None,
    )
    db.add(point)
    # ★ commit 而不是 flush：这个点是**测试中途**加的，而接口用的是同一个
    #   会话（autoflush=False），不提交的话接下来请求里的查询看不见它。
    db.commit()
    return point


def count_alerts(db: Session) -> int:
    """alerts 表的行数——单独立一个函数是为了让断言里是一个数字："多写了一条"
    和"一条都没写"在失败信息里都看得见。"""
    return int(db.scalar(select(func.count()).select_from(Alert)) or 0)


def add_alert(
    db: Session,
    rule: AlertRule | int,
    price: float = 88.0,
    *,
    snapshot: bool = True,
) -> Alert:
    """直接插一条触发记录，用来构造"已经触发过一次"的历史。
    ★ 走 `check_alerts` 也能造出来，但那要求先把价格和轮次都摆对，而想测的可能
      只是"列表接口怎么显示这条记录"。
    ★ `snapshot=True`（默认）连**触发时的规则值**一起写进快照列，这才是真实触发的
      形状；`snapshot=False` 造**迁移前的老记录**（两列 NULL），只该被"NULL 兜底"
      的用例显式使用。"""
    rule_obj = rule if isinstance(rule, AlertRule) else db.get(AlertRule, rule)
    if rule_obj is None:
        # ★ 别让它退化成"外键指向不存在的规则"然后过一个用例：SQLite 开了
        #   foreign_keys=ON 会抛 IntegrityError，错误信息不会说"id 传错了"。
        raise AssertionError(f"add_alert：规则 #{rule} 不存在，测试数据摆错了")

    alert = Alert(
        rule_id=rule_obj.id,
        triggered_at=utcnow(),
        price_at_trigger=price,
        # ★ 和 app/alerts_engine.py 的触发路径写同一对值，否则工厂造的
        #   "历史记录"和真实产生的形状不同，用工厂写的用例就测不到真实路径。
        rule_type_at_trigger=rule_obj.rule_type if snapshot else None,
        threshold_at_trigger=rule_obj.threshold if snapshot else None,
        # ★ 恒为 False：本项目没有外部推送渠道（见 models.py）。
        notified=False,
    )
    db.add(alert)
    db.commit()
    return alert
