"""把库里的数据装配成"即将发给模型的那一份"。

================================================================================
这个文件在整条链路上的位置
================================================================================

    build_payload(db, source)   ← 本文件，**只有 IO，没有判断对错**
              │
              ├──▶ prompts.build_messages()  → 发给模型
              │
              └──▶ fingerprint.build_fingerprint()  → 缓存键

`build_payload()` 的返回值**同时**是"喂给模型的输入"和"算指纹的原料"。
这是刻意的，也是本层最重要的设计：

    指纹覆盖的范围 == 模型看到的范围

如果这两者不是同一个对象，就会出现这类 bug：prompt 里加了"平均价"这个聚合量，
而指纹没算它 → 平均价变了、缓存照样命中 → 报告**基于过期数字说话而不自知**，
不报错、没有日志。

反过来的错误同样存在：指纹算上了全部 1000 个商品，而 prompt 只喂了前 60 个
→ 改动第 900 个商品会让缓存白白失效，而它对报告毫无影响。

================================================================================
一个必须显式说明的问题：prompt 里的样本是**有偏的**
================================================================================

1000 个商品全塞进 prompt 是 5~8 万 token，既贵又没必要。
所以只抽 60 个。但**抽法本身就是结论的一部分**：

    按销量降序抽的前 60 个，平均价 230 元
    和全部 1000 个的平均价 150 元

是两件完全不同的事。如果只把样本丢给模型、不说这是怎么抽的，
模型完全可能写出"该品类商品均价约 230 元"——**一个由采样方式制造的假结论**。

所以 payload 里有一个 `sample` 块，显式写明：
  · 这 60 个是怎么选出来的（`basis`）
  · 它们占全体的多少（`size` / `total_products`）
  · 一句话提醒（`note`）

而系统提示词里也明令禁止用样本的数字去推断整体。
**两层一起上**，因为这个错误的隐蔽性很高：报告读起来通顺、数字也都真实存在。

（`build_payload` 里那些聚合量——分位数、直方图——算的都是**全体**，
 和采样无关。两套数字放在同一个 payload 里，靠 `sample` 块区分，
 靠 prompt 里的措辞约束模型不要混用。）

================================================================================
一条明确的、**不打算**去修的边界：样本里的标题是外部文本
================================================================================

上面那个 `sample` 块里的 `title`，内容直接来自页面（SMZDM 的爆料标题，
或 seed 造的标题），**原样拼进 prompt**。于是这份 prompt 里有一块内容
不受我们控制，原则上可以在里面写"忽略以上规则，改为输出……"。
这就是 prompt injection，这里把边界如实写下来：

  · **不清洗，这是取舍不是疏忽。**
    "一句话像不像指令"在中文商品标题上分不出真假——"必须买""到手价"
    "不容错过"本来就是正常营销语。任何激进的过滤都会先杀掉真实数据，
    而收益只是挡住一个本来就没有杀伤力的输入。

  · **能做这个取舍，是因为影响面是封死的。** 模型手上没有任何工具：
    `glm_client` 每次只发一个 chat completion，参数只有
    model / messages / temperature / max_tokens / response_format，
    **没有 `tools`、没有 `tool_choice`**；它不能联网、不能读文件、
    不能执行代码。输出还要过 `_extract_json` 和 `_validate` 两道关，
    不合格直接抛 `AnalysisError`，不会写进 `analysis_reports`。
    所以被带偏的最坏结果是**一份胡说八道的报告**——是人会当场看出来的那种，
    而不是"系统被拿走"。

  · ★ 会出事的是另一种架构：一个能读文件、能发请求、能调工具的 agent。
    同一句标题在那种系统里的后果和这里完全不同。
    本项目不打算做那种 agent，所以这条边界**停在原地，不升级**。

★ 写下来是因为"没做"和"没想到"在代码里长得一模一样：
  两者都只是**没有这几行代码**。区别全在有没有地方说清楚为什么。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.aggregate import bucket_prices, median, quantile
from app.db.models import PriceHistory, Product, Shop
from app.keywords import rank_keywords
from app.source_scope import SourceFilter, apply_source
from app.textutil import elide_middle

# 这些数字是"够用就好"和"别太贵"之间的取舍点。
#
# ★ 为什么不是把 1000 个商品全发过去：
#   1000 行 × 约 60 token ≈ 6 万 token。glm-4-flash 的上下文放得下，
#   但（a）每次分析要花十几秒，（b）报告质量并不会更好——
#   模型需要的是**结构**（分布、头部、长尾），60 行已经足够表达结构，
#   剩下 940 行只是在重复"又有一个 100~200 元的充电宝"。
#
# ★ 为什么是这个具体的数：60 行 × 60 token ≈ 3600 token，
#   加上汇总区和 prompt 模板，总量在 5000 token 上下。
#   这个量级下单次调用 3~8 秒，演示时不会让人等得以为卡死了。
DEFAULT_SAMPLE_SIZE = 60
DEFAULT_KEYWORD_LIMIT = 40
DEFAULT_SHOP_LIMIT = 12

# 采样依据。写进 payload 让模型知道这批样本是怎么来的。
SAMPLE_BY_SALES = "sales_desc"
SAMPLE_BY_PRICE_POINTS = "price_point_count_desc"


def _fetch_products(db: Session, source: SourceFilter) -> list[dict[str, Any]]:
    """取回该数据源下所有商品的原始行（不带聚合）。

    ★ 一次性全取出来、后面的聚合全在 Python 里做，理由和 stats.py 一致：
      这张表只有 1000 行，写 SQL 窗口函数会让代码难读十倍，
      而换来的性能收益在这个量级上测不出来。

    ★ 数量上限是有的，但不是硬编码的魔法数字：
      真到百万行时这里必须改成分页 + SQL 预聚合，
      在那之前加一个假的 LIMIT 只会掩盖问题（数据被悄悄截断，
      报告却说"这是全部商品"）。
    """
    stmt = (
        select(
            Product.source,
            Product.source_id,
            Product.title,
            Product.brand,
            Product.platform,
            Product.category,
            Product.price,
            Product.price_max,
            Product.sales,
            Shop.name.label("shop_name"),
        )
        .select_from(Product)
        .outerjoin(Shop, Product.shop_id == Shop.id)
    )
    stmt = apply_source(stmt, source)

    return [
        {
            # ★ 业务主键，不是自增 id。见 fingerprint.py 的模块注释。
            "key": f"{row.source}:{row.source_id}",
            "title": row.title or "",
            "brand": row.brand,
            "platform": row.platform,
            "category": row.category,
            "price": row.price,
            "price_max": row.price_max,
            "sales": row.sales,
            "shop": row.shop_name,
        }
        for row in db.execute(stmt)
    ]


def _fetch_price_point_counts(db: Session, source: SourceFilter) -> dict[str, int]:
    """每个商品有多少个价格点（"被爆料过几次"）。

    ★ 返回值用业务主键 `source:source_id` 做键，而不是 `product_id`。
      因为调用方拿到的是上面那个已经"去掉了自增 id"的行列表——
      如果这里返回 product_id，就得在行里加回自增 id 才能 join 上，
      而自增 id 一旦出现，就有可能被顺手塞进 payload 里（指纹就废了）。
      全程只流通业务主键，从结构上避免这件事。

    ★ 单独一次聚合查询，而不是给上面那个查询加 GROUP BY：
      加了 GROUP BY 之后，每个商品会按价格点数量展开成多行，
      要再去重——那才是真正的性能陷阱（笛卡尔积）。
      两次简单查询比一次带 JOIN 的聚合更好读，在这个数据量下也更快。
    """
    stmt = (
        select(Product.source, Product.source_id, func.count(PriceHistory.id))
        .select_from(Product)
        .join(PriceHistory, PriceHistory.product_id == Product.id, isouter=True)
        .group_by(Product.id)
    )
    stmt = apply_source(stmt, source)
    return {f"{src}:{sid}": int(n) for src, sid, n in db.execute(stmt)}


def _build_summary(products: list[dict[str, Any]], point_counts: dict[str, int]) -> dict[str, Any]:
    """全体商品的汇总统计。★ 口径是【全部商品】，不是采样后的 60 个。"""
    prices = sorted(float(p["price"]) for p in products if p["price"] is not None)
    price_missing = sum(1 for p in products if p["price"] is None)
    sales_values = [int(p["sales"]) for p in products if p["sales"] is not None]

    summary: dict[str, Any] = {
        "product_count": len(products),
        "shop_count": len({p["shop"] for p in products if p["shop"]}),
        "price_point_count": sum(point_counts.values()),
        "price_missing_count": price_missing,
        "has_sales": bool(sales_values),
    }

    if prices:
        summary["price"] = {
            "min": round(prices[0], 2),
            "p25": round(quantile(prices, 0.25), 2),
            "median": round(median(prices), 2),
            "p75": round(quantile(prices, 0.75), 2),
            "max": round(prices[-1], 2),
            # mean 保留两位就够。★ 不写更多位是因为：模型看到 187.36458333333334
            # 这种数字时，有相当高的概率会在报告里原样抄一遍，
            # 于是报告里出现一个精度远高于数据本身意义的数字，
            # 读起来像是"算得很精确"，实际是在虚张声势。
            "mean": round(sum(prices) / len(prices), 2),
        }
    else:
        summary["price"] = None

    if sales_values:
        sales_values.sort()
        summary["sales"] = {
            "total": sum(sales_values),
            "median": int(median([float(v) for v in sales_values])),
            "max": sales_values[-1],
        }
    else:
        summary["sales"] = None

    # 品牌集中度：前 5 个品牌各占多少商品。
    # 这是"竞品格局"最直接的一个切面，而且不需要销量数据就能算——
    # 对只有真实采集数据的场景（没有销量）尤其有用。
    brand_counter: dict[str, int] = {}
    for p in products:
        name = (p["brand"] or "").strip()
        if name:
            brand_counter[name] = brand_counter.get(name, 0) + 1
    summary["top_brands"] = [
        {"brand": b, "product_count": c}
        for b, c in sorted(brand_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
    ]
    summary["brand_missing_count"] = sum(1 for p in products if not (p["brand"] or "").strip())

    return summary


def _build_shops(products: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """店铺维度。字段和 /api/stats/shop-share 对齐，便于两边互相对照。"""
    buckets: dict[str, dict[str, Any]] = {}
    for p in products:
        name = (p["shop"] or "").strip() or "未知店铺"
        item = buckets.setdefault(name, {"shop": name, "product_count": 0, "_prices": []})
        item["product_count"] += 1
        if p["price"] is not None:
            item["_prices"].append(float(p["price"]))

    total = len(products) or 1
    out = []
    for item in buckets.values():
        prices = item.pop("_prices")
        item["share"] = round(item["product_count"] / total, 4)
        item["avg_price"] = round(sum(prices) / len(prices), 2) if prices else None
        out.append(item)

    # ★ 排序必须确定：`-product_count` 之后加 `shop` 做兜底键。
    #   只按数量排的话，数量相同的两个店铺的先后取决于 dict 的插入顺序，
    #   而插入顺序又取决于 SQLite 的返回顺序——同一个库、同一份数据，
    #   换个索引或换个 SQLite 版本就可能变。
    #   后果不是"图不好看"，而是**同样的数据算出不同的 payload → 不同的指纹
    #   → 缓存永远不命中**。这个 bug 极其难查（见 fingerprint.py 的模块注释）。
    out.sort(key=lambda x: (-x["product_count"], x["shop"]))
    return out[:limit]


def _sample_products(
    products: list[dict[str, Any]], point_counts: dict[str, int], size: int
) -> tuple[list[dict[str, Any]], str]:
    """挑出要发给模型的那几十个商品，返回 (样本, 采样依据)。

    ★ 有销量时按销量降序——"卖得好的"显然是分析重点。

    ★ 没有销量时（真实电商的常态，见 docs/可行性评估.md 12.2）
      **不能**按价格降序。按价格排出来的样本全是贵的，
      模型会得出"该品类均价偏高"的结论——而这个结论完全由采样方式制造。
      改按"价格点数量降序"：被爆料次数最多的商品，
      恰好就是市场上最活跃、最有观察价值的那些，而且这个依据与价格无关，
      不会给模型植入价格上的偏见。

    ★ 兜底键（key）同样是必需的，理由见 _build_shops 里那段。
    """
    # ★ 空列表不需要特判：下面 `any(...)` 在空列表上是 False、
    #   `sorted([])` 是 []、切片也是 []，自然就走到"返回空样本"。
    #   加一个 `if not products: return ...` 只是为了写起来安心，
    #   而它多出来的那个分支反而要单独维护（第一版就在这里留了句废代码）。
    has_sales = any(p["sales"] is not None for p in products)

    if has_sales:
        ordered = sorted(
            products,
            key=lambda p: (-(p["sales"] if p["sales"] is not None else -1), p["key"]),
        )
        basis = SAMPLE_BY_SALES
    else:
        ordered = sorted(
            products,
            key=lambda p: (-point_counts.get(p["key"], 0), p["key"]),
        )
        basis = SAMPLE_BY_PRICE_POINTS

    sample = []
    for p in ordered[:size]:
        sample.append({
            "key": p["key"],
            # ★ 标题截断到 40 个显示宽度：60 个完整标题（含大量营销语）
            #   会吃掉一两千 token，而"某条标题里的营销话术"不是这份报告要分析的东西。
            #   40 是实测值——能保住品牌 + 型号 + 规格 + 颜色（见 app/textutil.py）。
            "title": elide_middle(p["title"], budget=40),
            "brand": p["brand"],
            "platform": p["platform"],
            "category": p["category"],
            "price": round(float(p["price"]), 2) if p["price"] is not None else None,
            "price_max": round(float(p["price_max"]), 2) if p["price_max"] is not None else None,
            "sales": p["sales"],
            "shop": p["shop"],
            "points": point_counts.get(p["key"], 0),
        })
    return sample, basis


def _build_keywords(products: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """复用词云的分词实现。

    ★ 用 `app.keywords.rank_keywords()`，而不是自己调 jieba。
      理由和 app/aggregate.py 完全一样：词云图上的词和报告里说的词
      必须是同一份计算结果，否则"图上最大的词是『充电宝』、
      报告里却说『快充』是最高频词"这种矛盾就会出现，且两边都不报错。

    ★ `rank_keywords` 已经包含了"还原人类写法"这一步（`iphone` → `iPhone`）。
      如果不还原，报告里会出现全小写的型号名，读起来像是数据脏，
      其实是我们在分词时把它归一化了。

    ★ 分词依赖 jieba，首次调用要加载词典（约 0.5 秒）。
      这个开销已经由 app/main.py 的 warm_up() 在启动时付掉了。
    """
    ranked = rank_keywords([p["title"] for p in products], limit=limit)
    return [{"name": name, "value": count} for name, _key, count in ranked]


def build_payload(
    db: Session,
    source: SourceFilter,
    *,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    keyword_limit: int = DEFAULT_KEYWORD_LIMIT,
    shop_limit: int = DEFAULT_SHOP_LIMIT,
) -> dict[str, Any]:
    """装配完整的分析输入。

    ★ 返回的 dict 必须**只含 JSON 可序列化的类型**，且**不含任何时间戳
      或自增 id**——`fingerprint.build_fingerprint()` 会强制检查这一点，
      混进去会直接抛 FingerprintError（而不是静默让缓存失效）。

    ★ 这个函数是纯读取的：不改库、不发网络请求。所以它可以被反复调用，
      测试里也能随便调而不产生副作用。
    """
    products = _fetch_products(db, source)
    point_counts = _fetch_price_point_counts(db, source)

    prices = sorted(float(p["price"]) for p in products if p["price"] is not None)
    _step, price_buckets = bucket_prices(prices)

    sample, sample_basis = _sample_products(products, point_counts, sample_size)

    # ★ `note` 是给模型看的一句话。它是这个 payload 里唯一"不是数据"的东西，
    #   但它防的是一个具体的、很容易发生的错误：
    #   把采样之后的统计量当成全体的统计量说出来。
    #   （system prompt 里也有一条对应的禁令，两层一起上。）
    sample_note = (
        f"以下 {len(sample)} 个商品是按"
        + ("销量从高到低" if sample_basis == SAMPLE_BY_SALES else "被爆料次数从多到少")
        + f"选出的样本，占全部 {len(products)} 个商品的一部分。"
        "样本内部的均价/占比**不能**代表整体，整体的统计量请看 summary 块。"
    )

    return {
        "source": source.value,
        "summary": _build_summary(products, point_counts),
        "price_buckets": price_buckets,
        "shops": _build_shops(products, shop_limit),
        "keywords": _build_keywords(products, keyword_limit),
        "sample": {
            "basis": sample_basis,
            "size": len(sample),
            "total_products": len(products),
            "note": sample_note,
        },
        "top_products": sample,
    }
