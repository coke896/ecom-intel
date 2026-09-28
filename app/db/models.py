
"""ORM 模型：7 张表。

设计要点（每一条都对应一个具体的失败场景，不是"规范要求"）：

1. **`source` 与 `platform` 是两个不同维度**，必须拆开。
   `source` 是数据血缘（从哪采的），`platform` 是销售平台（在哪卖）。
   混在一起的典型症状：把 `platform` 默认值写成 "smzdm"——
   然后统计"各平台占比"时出现一个叫 smzdm 的电商平台。

2. **两张表的 upsert 语义相反**，见 price_history 与 products 的注释。

3. **`price_history` 的唯一键是 (product_id, source_ref) 而不是
   (product_id, recorded_at)**，见该表注释。

4. **`alerts` 拆成规则表和触发表**，见对应注释。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utcnow


class Shop(Base):
    """店铺。

    单独建表而不是在 products 里存一个字符串，是为了让"店铺占比"这类
    聚合查询能走索引，也避免同一家店因为写法不同（"京东自营" / "京东自营 "）
    被拆成两条。
    """

    __tablename__ = "shops"
    __table_args__ = (
        # 同一平台下的店名唯一。跨平台同名是允许的（京东和天猫都可能有"官方旗舰店"）。
        UniqueConstraint("platform", "name", name="uq_shop_platform_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    platform: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    products: Mapped[list[Product]] = relationship(back_populates="shop")


class Product(Base):
    """商品。

    ★ 本表最重要的一点：**`source` 字段决定 Mock 与真实数据能否隔离**。

    没有它的话，阶段④ 接入真实数据后，统计图表里 Mock 和真实数据会混在一起算，
    "价格分布""销量 Top20"全部失真，演示时被问"这数据是真的吗"也答不清。
    """

    __tablename__ = "products"
    __table_args__ = (
        # ★ 去重的最终防线。用数据库唯一约束而不是应用层"先查再插"：
        #   后者在并发采集时存在竞态——两个 worker 同时查到"不存在"，
        #   然后都插入。唯一约束是原子性的，插不进去就是插不进去。
        UniqueConstraint("source", "source_id", name="uq_product_source_sid"),
        Index("ix_product_title_norm", "title_norm"),
        Index("ix_product_last_crawled", "last_crawled_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # ---- 数据血缘 -------------------------------------------------------------
    # 'mock'（生成的演示数据）/ 'smzdm'（真实采集）
    source: Mapped[str] = mapped_column(String(16), nullable=False)

    # 采集通道，区分同一站点里两种形态不同的页面：
    #   'jingxuan' —— 精选好价页，是【价格事件流】，每条带发布时间
    #   'fenlei'   —— 类目页，是【商品快照】，没有发布时间
    #   'mock'     —— 生成数据
    # 这个字段不只是元数据：它决定了 recorded_at 的语义（见 price_history）。
    source_channel: Mapped[str] = mapped_column(String(16), nullable=False, default="mock")

    # 源站记录 ID。SMZDM 是文章 id（如 183089533）。
    # Mock 数据用 "mock-0001" 这种可读格式。
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # ---- 业务属性 -------------------------------------------------------------
    # ★ 销售平台（京东/天猫/拼多多），不是数据来源。
    #   和上面的 source 是完全不同的维度，早期草稿把两者混为一谈过。
    platform: Mapped[str | None] = mapped_column(String(32))
    category: Mapped[str | None] = mapped_column(String(32))

    title: Mapped[str] = mapped_column(Text, nullable=False)
    # 归一化后的标题，归并比对用。
    # 单独存一列而不是每次查询时现算：一来能建索引，
    # 二来保证"比对时用的"和"入库时存的"是同一个值，不会因为
    # 归一化函数改动而出现新旧数据不一致。
    title_norm: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # ★ 品牌，归并的【硬门槛】依赖它。
    #   可空：白牌/杂牌提取不出品牌，此时硬门槛按"无证据不否决"放行。
    #   类目页（fenlei 通道）的卡片不含品牌字段，这里就是 NULL。
    brand: Mapped[str | None] = mapped_column(String(64))

    url: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)

    # ---- 价格 -----------------------------------------------------------------
    # ★ 这两列描述的是【当前这一次成交/爆料的价格区间】，不是历史统计量。
    #
    #   price     = 区间下限（或唯一价）
    #   price_max = 区间上限（单值时两者【相等】，不是 NULL）
    #
    #   来源是 cleaner.parse_price() 返回的二元组：
    #     "199元"          → (199.0, 199.0)
    #     "券后199-299"     → (199.0, 299.0)
    #     "199元起"        → (199.0, None)   ← 开区间，上限未知
    #
    # ★ 这一点必须写死，因为它有一个非常隐蔽的出错方式：
    #
    #   "上限"和"历史最高价"看起来是同一件事，但它们是两回事。
    #   Mock 数据一度把 price_max 填成了 90 天序列的最大值
    #   （本意是"历史最高价"），而真实采集路径按 parse_price 填区间上限。
    #   于是**同一个列在 ?source=mock 和 ?source=smzdm 下含义不同**，
    #   混在一起做「价格区间宽度」这类图时会得到毫无意义的数字，
    #   而且不报错、没有日志。
    #
    #   → 历史最高/最低价一律【从 price_history 现算】（MAX/MIN 聚合），
    #     那才是唯一的真相来源，而且对两个数据源语义一致。
    #     tests/test_seed.py::test_price_max_is_the_deal_range_not_a_historical_high
    #     守着这条。
    #
    # ★ 为什么建表时就要有 price_max：
    #   如果只有 price，阶段③ 写 prompt 时才发现价格有区间，
    #   就要改表 + 改 prompt + 重跑。
    price: Mapped[float | None] = mapped_column(Float)
    price_max: Mapped[float | None] = mapped_column(Float)

    # 可空：真实电商的销量不在网页端公开，接了 SMZDM 数据后这一列全是 NULL。
    # 保留它是为了让 Mock 数据能驱动"销量 Top20"图表——这是刻意的取舍，
    # 会在 README 和前端空态里如实说明。
    sales: Mapped[int | None] = mapped_column(Integer)

    shop_id: Mapped[int | None] = mapped_column(ForeignKey("shops.id"))

    # ---- 采集元数据 -----------------------------------------------------------
    last_crawled_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    # ★ 疑似重复指向：软门槛落在灰色地带（0.60~0.75）时新建商品并记下它像谁，
    #   留给人工核对。
    #   为什么只在灰色地带记：低于 0.60 的两个标题大概率就是不同商品，
    #   全记下来的话这个字段会迅速充满假阳性，"待人工核对"永远核对不完。
    suspected_duplicate_of: Mapped[int | None] = mapped_column(ForeignKey("products.id"))

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    shop: Mapped[Shop | None] = relationship(back_populates="products")
    price_points: Mapped[list[PriceHistory]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )


class PriceHistory(Base):
    """价格历史（时间序列的一个数据点）。

    ★ 唯一键是 (product_id, source_ref)，**不是 (product_id, recorded_at)**。

    为什么不能用 recorded_at：
      recorded_at 是【时间属性】，不该承担"身份"职责。
      同一商品被两篇爆料在同一秒提及（SMZDM 的 timesort 精度到秒）时，
      `ON CONFLICT DO NOTHING` 会静默丢掉一条真实数据——
      不报错、不告警，只是数据少了一条。这种 bug 排查起来极其痛苦。

    为什么 source_ref 对：
      它是源站的记录 id（爆料文章 id），才是真正的身份。
      "同一篇文章重复采集 → 冲突 → 跳过"，语义完全正确，
      而且顺带覆盖了上面那个同秒的极端情况。

    ★ Mock 数据不受这个约束影响：
      Mock 的 source_ref 全是 NULL，而 SQLite 的 UNIQUE 约束
      **把多个 NULL 视为互不相同**（这是 SQL 标准行为，
      因为 NULL != NULL）。所以 Mock 的 90 天价格序列照样能自由写入。

    ★ 写入语义：一律 ON CONFLICT DO NOTHING（见下面 upsert 的对照）。
    """

    __tablename__ = "price_history"
    __table_args__ = (
        UniqueConstraint("product_id", "source_ref", name="uq_ph_product_sourceref"),
        Index("ix_ph_product_recorded", "product_id", "recorded_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)

    price: Mapped[float] = mapped_column(Float, nullable=False)

    # ★ recorded_at 的语义随 source_channel 而变，使用前必须知道这一点：
    #   source_channel='jingxuan' → 爆料的【发布时间】（页面上的 timesort）
    #   source_channel='fenlei'   → 本次【采集时间】（页面没有发布时间）
    #   source_channel='mock'     → 生成的模拟时间
    # 混用不同语义的数据画趋势图是不诚实的，所以 source_channel 跟着商品一起
    # 传递到查询层，前端可以据此提示。
    recorded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # 源站记录 id（爆料文章 id）。Mock 数据为 NULL。
    source_ref: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    product: Mapped[Product] = relationship(back_populates="price_points")


class CrawlTask(Base):
    """采集任务。

    ★ 用数据库表做任务队列，而不是 FastAPI 的 BackgroundTasks，
    更不是 Celery：

    - BackgroundTasks 绑在请求生命周期上，请求返回后进程可能被回收，
      长任务（分页抓取 + 解析 + 写库）跑一半就断了，而且前端拿不到进度。
    - Celery 需要一个 Redis/RabbitMQ broker，为了一个作品集项目
      多引入一个中间件不划算，部署复杂度也上去了。

    本项目要的是"能查进度、能重试、进程重启不丢任务"，
    一张表 + 一个独立 worker 进程就够了。
    """

    __tablename__ = "crawl_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    # pending / running / success / failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")

    # 进度。★ 批量更新而不是每条数据都 UPDATE：
    #   SQLite 在 WAL 下虽然读写不阻塞，但写写仍然是串行的。
    #   每写一条商品就更新一次进度，会让写锁被高频抢占，
    #   Web 端的查询要排队等锁。所以 worker 每处理 N 条才更新一次。
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total: Mapped[int | None] = mapped_column(Integer)

    message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class AnalysisReport(Base):
    """GLM 分析报告。按数据指纹缓存。"""

    __tablename__ = "analysis_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # ★ 数据指纹：64 个字符（sha256 十六进制）。
    #   长度必须现在定死——SQLite 虽然不在乎 VARCHAR 长度，
    #   但换成 PostgreSQL 时 64 和 255 是两种不同的索引大小。
    #
    #   唯一索引是缓存能工作的前提：同一个指纹只能有一份报告。
    #   配合 force=1 强制刷新时的 ON CONFLICT DO UPDATE（不是 INSERT），
    #   否则第二次强制刷新会撞唯一索引。
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    model: Mapped[str] = mapped_column(String(64), nullable=False)
    # GLM 返回的 JSON（字符串形式）。存 JSON 而不是 Markdown，
    # 是为了后端渲染可控、可测、可缓存。
    content_json: Mapped[str] = mapped_column(Text, nullable=False)
    tokens_used: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class AlertRule(Base):
    """告警规则（"配置"实体）。

    ★ 为什么告警要拆成两张表：
      规则（阈值、作用商品、是否启用）和触发记录（何时触发、当时价格、是否推送）
      是两种不同的实体。塞进一张表会出现大量 NULL 列——
      规则行没有 triggered_at，触发记录行没有 threshold。
    """

    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)

    # 'price_below'（低于阈值）/ 'price_drop_pct'（跌幅超过百分比）
    rule_type: Mapped[str] = mapped_column(String(32), nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # ★ 状态机：'armed'（已武装）/ 'fired'（已触发，等待重新武装）。
    #
    #   没有这个字段的话，价格持续低于阈值期间，一条规则一天会写
    #   288 条记录、推 288 条通知（按 5 分钟轮询一次算）。
    #   规则是：
    #     armed + 价格<阈值        → 触发，写 alerts，state ← fired
    #     fired + 价格<阈值        → 跳过（不重复触发）
    #     fired + 价格≥阈值        → 重新武装，state ← armed（不发通知）
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="armed")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    alerts: Mapped[list[Alert]] = relationship(back_populates="rule")


class Alert(Base):
    """告警触发记录（"事件"实体）。"""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("alert_rules.id"), nullable=False)

    triggered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    # 触发时的价格。★ 必须冗余存下来：
    #   price_history 里可能有几万个点，事后想还原"当时为什么触发"
    #   要去反查时间点，既慢又可能因为数据清理而查不到。
    price_at_trigger: Mapped[float | None] = mapped_column(Float)

    notified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    rule: Mapped[AlertRule] = relationship(back_populates="alerts")
