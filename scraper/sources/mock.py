"""Mock 采集源 —— 把 seed.py 的生成器包成一个符合 DataSource 协议的源。

存在的理由：让"Mock 与 SMZDM 走同一条管道"从一句话变成可验证的性质（此前 Mock
只走 seed 直写路径；base.py 里删掉的"JSON 接口"层就是没被证实的一句空话，见 docs 16.1）。

★ 必须复用 seed 的生成器：同样的 rng 种子、同样的 build_products 调用得到**逐字节
相同**的商品——`?source=mock` 在 seed 过的库上跑 = 刷新而非新增，两种入口（seed 直写 /
采集管道）收敛到同一库状态。
★ 对齐前提是**随机数消费顺序完全相同**：必须先完整走完 count 条（默认 1000），
随机状态才能和 seed 一致——只生成前 N 条是不够的。
★ 它不联网、不写数据库（fetch 只产出 list[RawDeal]），写的仍是 source='mock'。
"""

from __future__ import annotations

import numpy as np

from app.db.base import utcnow
from scraper.base import FetchStats, RawDeal
from scraper.seed import (
    DEFAULT_DAYS,
    DEFAULT_PRODUCT_COUNT,
    DEFAULT_SEED,
    build_products,
)

# 一页多少条。和 SMZDM 精选页一样是 20 条——让两个源的"一页"大小一致，
# "抓 3 页"在两种源下是同一个量级的工作，进度条的含义也就一致了。
PER_PAGE = 20


class MockSource:
    """确定性的、离线的采集源。

    ★ 确定性边界：source_id / 标题 / 价格 / 品牌 / 销量 / 店铺完全确定，与调用次数
    无关；recorded_at 每次调用不同（是抓取时刻，做成确定的反而会撒谎）。数据层幂等
    不受影响——价格点身份 (product_id, source_ref) 不含时间，第二轮会被唯一约束挡掉。
    """

    name = "mock"

    def __init__(
        self,
        *,
        count: int = DEFAULT_PRODUCT_COUNT,
        days: int = DEFAULT_DAYS,
        seed_value: int = DEFAULT_SEED,
        page_size: int = PER_PAGE,
    ) -> None:
        # ★ 只在构造时记参数，不在这里生成数据：临时实例不该连带生成 1000×90 条。
        self.count = count
        self.days = days
        self.seed_value = seed_value
        self.page_size = page_size

    # --- DataSource 协议 ---

    def fetch(self, *, pages: int) -> tuple[str, list[RawDeal], FetchStats]:
        """产出前 `pages` 页爆料。永不失败、永不联网。

        ★ 每次调用都**重新建 rng**：用同一个 rng 对象连续调用两次的话，第二次
        从流里靠后的位置取数、产出完全不同——而"连抓两轮结果一致"正是要验证的性质。
        """
        if pages < 1:
            raise ValueError("pages 必须 ≥ 1")

        rng = np.random.default_rng(self.seed_value)
        # ★ 必须生成完整的一批：随机数流的对齐要求走完 1000 条（见模块顶部）。
        products = build_products(rng, self.count, self.days)

        want = min(pages * self.page_size, len(products))
        # ★ 抓取时刻取一次整批共用：循环里逐条 utcnow() 会让同一批时间戳差几毫秒，
        # 重放对比对不上；价格点幂等不受影响（身份不含时间）。
        now = utcnow()

        # ★ pages_ok 按实际产出算：请求 3 页而数据集只剩 1 页时，报"3 页成功"是假的
        # （这个数字会显示在任务进度里）。
        stats = FetchStats(
            pages_ok=-(-want // self.page_size) if want else 0,
            layer="memory",
        )
        deals = [self._to_deal(p, now) for p in products[:want]]
        return "memory", deals, stats

    @staticmethod
    def _to_deal(p: dict, recorded_at) -> RawDeal:
        """把 seed 的商品 dict 转成 RawDeal。

        ★ 这个转换刻意"倒着走"：把浮点价格重新写成 `"199.9元"` 原文，好让下游
        cleaner.parse_price 真正跑一遍——直接塞浮点就绕过了要被验证的清洗层。
        ★ 用 `_shop` 取店铺名：没有它 shop_id 全 NULL，"店铺占比"饼图会空掉。
        """
        platform, shop_name = p["_shop"]
        return RawDeal(
            source="mock",
            source_id=p["source_id"],
            title=p["title"],
            # 浮点 → 原文，让 cleaner.parse_price 真的跑一遍；用 {:g} 而非 %.2f，
            # 149.9 写成 "149.9元"（"149.90元" parse 结果相同但观感不同）。
            price_raw=f"{p['price']:g}元",
            recorded_at=recorded_at,
            url=p["url"],
            image_url=p["image_url"],
            brand=p["brand"],
            platform=platform,
            category=p["category"],
            channel=p["source_channel"],
            sales=p["sales"],
            shop_name=shop_name,
        )

    # --- 离线入口（和 SmzdmSource.parse_files 对称） ---

    def all_deals(self) -> list[RawDeal]:
        """产出全部 `count` 条爆料（给"整个数据集过一遍采集管道"这种用法）。

        按 page_size 换算就是 ceil(count / page_size) 页。
        """
        pages = -(-self.count // self.page_size)     # 向上取整
        _layer, deals, _stats = self.fetch(pages=pages)
        return deals
