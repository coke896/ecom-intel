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
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import PriceHistory, Product, Shop


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
    ) -> Product:
        """插一个商品。`points` 是给它造几个价格历史点。"""
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

        for i in range(points):
            db.add(
                PriceHistory(
                    product_id=product.id,
                    price=100.0 + i,
                    recorded_at=utcnow() - timedelta(days=points - i),
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
