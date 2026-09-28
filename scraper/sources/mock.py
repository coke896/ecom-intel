"""Mock 采集源 —— 把 seed.py 的生成器包成一个符合 DataSource 协议的源。

================================================================================
它存在的理由：让"两个源走同一条管道"从一句话变成可验证的性质
================================================================================
scraper/base.py 和 ingest.py 的文档里都写着：

    "Mock 和 SMZDM 走的是同一条管道，差别只在怎么拿到 RawDeal"

在写这个文件之前，那句话是**不成立的**——Mock 数据走的是
`scraper/seed.py` 的直写路径，和 SMZDM 的采集管道是两条路。
一句话只要没人能验证，它就迟早变成一句不准确的话。
（这个坑本项目踩过一次：base.py 里原本写了"JSON 接口"那一层降级，
  而它从头到尾没有被证实存在，后来删掉了。见 docs 第十六节 16.1。）

有了这个文件，两件事可以说清楚而且能跑：

    POST /api/tasks/crawl?source=mock    → 不联网、不碰 WAF、可重复跑
    POST /api/tasks/crawl?source=smzdm   → 离线重放真实页面

================================================================================
★ 为什么它必须复用 seed 的生成器，而不是自己造一批数据
================================================================================
如果 Mock 源自己生成一套新 id（比如 `mockcrawl-00001`），会有两个后果：

  ① `?source=mock` 下会出现两批互不相干的 Mock 商品，
     统计图表把两批混在一起算，"1000 个商品"变成"1000 + N 个"，
     而 N 取决于你点了几次采集按钮。数字变得不可复现。
  ② `seed.py` 重灌时会清空 `source='mock'`，把这批采集来的 Mock 商品
     一起删掉——因为从数据库的角度它们确实是 Mock 数据。

复用 seed 的生成器（同样的 np.random.Generator 种子、同样的
`build_products(rng, count, days)` 调用）则得到**逐字节相同**的商品：
同样的 source_id、标题、价格。于是：

  · `POST /api/tasks/crawl?source=mock` 在 seed 过的库上跑 = 刷新，
    不新增任何商品，也不改任何价格（因为价格算出来是一样的）
  · 先采集、后 seed 也一样收敛到同一个状态
  · 两种入口（seed 直写 / 采集管道）产出的库状态是一致的

★ 对齐的前提是**随机数消费顺序完全相同**。
  seed() 里是 `rng = default_rng(seed_value)` 然后直接 `build_products(...)`，
  中间没有别的 rng 调用。这里也照做，一步不差。
  这也是为什么本文件要传 `count` 和 `days` 的**默认值给 build_products**
  ——只生成前 20 条是不够的，因为 rng 需要把 1000 条全部走一遍
  才能让第 21 条的随机状态和 seed 一致。

================================================================================
★ 它【不】做什么
================================================================================
· 不联网。Mock 源天生离线，这正是它相对 SMZDM 源的价值。
· 不写数据库。`fetch()` 只负责产出 `list[RawDeal]`，
  清洗和入库由 `scraper/ingest.py` 统一做——和 SMZDM 源一样。
· 不装成"另一个数据源"。它写的还是 `source='mock'`，
  统计接口的 `?source=mock` 过滤对它照常生效。
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

# 一页多少条。和 SMZDM 精选页一样是 20 条——
# ★ 这个数字不是随手取的：让两个源的"一页"大小一致，
#   "抓 3 页"在两种源下是同一个量级的工作，进度条的含义也就一致了。
PER_PAGE = 20


class MockSource:
    """确定性的、离线的采集源。

    ★ 确定性到什么程度，说清楚（第一版这里写的是"永远产出同一批爆料"，
      跑了一下才发现是错的）：

        source_id / 标题 / 价格 / 品牌 / 销量 / 店铺   完全确定，与调用次数无关
        recorded_at                                  每次调用不同，是抓取时刻

      所以 `fetch()` 两次的返回值**不相等**（RawDeal 里含 recorded_at），
      但前一组字段逐字节相同。

      recorded_at 有所不同是**对的**：它表示"这次采集发生在什么时候"。
      做成确定的反而会撒谎，而且会让两轮采集的价格点时间戳相同，
      看不出"第二轮是后来跑的"。

      数据层面的幂等不受影响：价格点的身份是 (product_id, source_ref)，
      而 source_ref 是 source_id，不含时间。所以第二轮那个新时间戳
      会被唯一约束挡掉，不会多出一个价格点。
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
        # ★ 只在构造时记参数，**不在这里生成数据**。
        #   放在构造里的话，一个用来"看看有几页"的临时实例
        #   会连带生成 1000 个商品 × 90 天价格，这是个纯粹的意外开销。
        self.count = count
        self.days = days
        self.seed_value = seed_value
        self.page_size = page_size

    # ------------------------------------------------------------------
    # DataSource 协议
    # ------------------------------------------------------------------

    def fetch(self, *, pages: int) -> tuple[str, list[RawDeal], FetchStats]:
        """产出前 `pages` 页爆料。永不失败、永不联网。

        ★ 每次调用都**重新建 rng**，这是"确定性"的来源。
          用同一个 rng 对象连续调用两次的话，第二次拿到的是
          rng 流里靠后的位置，产出完全不同的数据——
          而"连抓两轮结果一致"恰恰是要验证的性质之一。
        """
        if pages < 1:
            raise ValueError("pages 必须 ≥ 1")

        rng = np.random.default_rng(self.seed_value)
        # ★ 必须生成完整的一批，不能只生成前 N 条：
        #   随机数流的对齐要求走完 1000 条。见模块顶部。
        products = build_products(rng, self.count, self.days)

        want = min(pages * self.page_size, len(products))
        # ★ 抓取时刻取一次，整批共用。
        #   在循环里逐条调 utcnow() 会让同一批爆料的时间戳相差几毫秒，
        #   于是"同一批数据重放两次"的时间戳对不上——
        #   而价格点的身份是 (product_id, source_ref)，不受影响，
        #   但报告和日志里会出现无意义的差异。
        now = utcnow()

        # ★ pages_ok 按**实际产出**算，不是把请求的页数抄一遍。
        #   请求 3 页但数据集只剩 1 页时，报"3 页成功"就是假的——
        #   而这个数字会显示在任务进度里。
        stats = FetchStats(
            pages_ok=-(-want // self.page_size) if want else 0,
            layer="memory",
        )
        deals = [self._to_deal(p, now) for p in products[:want]]
        return "memory", deals, stats

    @staticmethod
    def _to_deal(p: dict, recorded_at) -> RawDeal:
        """把 seed 的商品 dict 转成 RawDeal。

        ★ 这里是"两个源形状不同"的那个接缝，值得看清楚：
          seed 的 dict 是**已经洗好、已经入库**的形状（含 price 浮点数、
          title_norm、shop_id）；RawDeal 是**一个字都没洗**的形状
          （含 price_raw 字符串、没有 title_norm）。

          所以这个转换是刻意"倒着走"的——把浮点价格重新写成
          `"199.9元"` 这样的原文，好让下游的 cleaner.parse_price
          真正跑一遍。直接把浮点塞进去就绕过了清洗层，
          而那正是要被验证的东西。

        ★ `_shop` 和 `_series` 是 build_products 的私有键（下划线开头），
          分别给 seed 自己用。这里只用 `_shop` 取店铺名：
          没有它的话 Mock 采集出来的商品 shop_id 全是 NULL，
          "店铺占比"饼图会空掉——而那个图正是 Mock 数据的主要价值之一。
          （`_series` 不用：90 天价格序列是 seed 直写路径的产物，
           采集路径每次只贡献"此刻的一个价格点"，这是对的。）
        """
        platform, shop_name = p["_shop"]
        return RawDeal(
            source="mock",
            source_id=p["source_id"],
            title=p["title"],
            # 浮点 → 原文，让 cleaner.parse_price 真的跑一遍。
            # 用 repr 而不是 f-string 补零：149.9 写成 "149.9元"，
            # 而 "%.2f" 会写成 "149.90元"——parse 回来都是 149.9，
            # 但日志和断言的观感不一样，用最短的那个。
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

    # ------------------------------------------------------------------
    # 离线入口（和 SmzdmSource.parse_files 对称）
    # ------------------------------------------------------------------

    def all_deals(self) -> list[RawDeal]:
        """产出全部 `count` 条爆料。

        给"把整个 Mock 数据集通过采集管道灌一遍"这种用法。
        按 page_size 换算就是 ceil(count / page_size) 页。
        """
        pages = -(-self.count // self.page_size)     # 向上取整
        _layer, deals, _stats = self.fetch(pages=pages)
        return deals
