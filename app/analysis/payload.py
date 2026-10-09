"""把库里的数据装配成"即将发给模型的那一份"。

`build_payload()` 的返回值**同时**是喂给模型的输入和算指纹的原料，这是刻意的：
指纹覆盖的范围 == 模型看到的范围。两者不一致会出现"聚合量变了缓存照样命中、
报告基于过期数字说话而不自知"，或"改第 900 个商品白白让缓存失效"。

样本是**有偏的**：1000 个商品只抽 60 个，抽法本身就是结论的一部分。所以 payload
里有 sample 块写明怎么选的、占全体多少，system prompt 也禁止用样本推断整体。

已知边界：样本标题来自外部、原样进 prompt（prompt injection），**不清洗是取舍**
——中文商品标题里"必须买"本是正常营销语；且影响面封死（模型没有 tools，输出
还要过 _extract_json 和 _validate 两道关），最坏是一份胡说八道的报告。
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

# 这些数字是"够用就好"和"别太贵"之间的取舍点：1000 个商品全发 ≈ 6 万 token、
# 每次十几秒，而报告质量并不会更好——模型要的是**结构**，60 行已经足够表达。
DEFAULT_SAMPLE_SIZE = 60
DEFAULT_KEYWORD_LIMIT = 40
DEFAULT_SHOP_LIMIT = 12

# 采样依据。写进 payload 让模型知道这批样本是怎么来的。
SAMPLE_BY_SALES = "sales_desc"
SAMPLE_BY_PRICE_POINTS = "price_point_count_desc"


def _fetch_products(db: Session, source: SourceFilter) -> list[dict[str, Any]]:
    """取回该数据源下所有商品的原始行（不带聚合）。

    ★ 一次性全取出来、聚合全在 Python 里做：这张表只有 1000 行，窗口函数会让
      代码难读十倍，换来的性能收益在这个量级上测不出来。
    ★ 真到百万行时这里必须改成分页 + SQL 预聚合；在那之前加一个假的 LIMIT
      只会掩盖问题（数据被悄悄截断，报告却说"这是全部商品"）。
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

    ★ 用业务主键 `source:source_id` 做键而不是 product_id：调用方拿到的是已经
      "去掉自增 id"的行列表，自增 id 一旦出现就可能被顺手塞进 payload（指纹就废了），
      全程只流通业务主键，从结构上避免这件事。
    ★ 单独一次聚合查询，而不是给上面的查询加 GROUP BY：加了之后每个商品会按
      价格点数量展开成多行、还要去重——那才是真正的性能陷阱（笛卡尔积）。
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
            # mean 保留两位就够。★ 不写更多位：模型看到 187.36458333333334 这类
            # 数字时很可能在报告里原样抄一遍，读起来像"算得很精确"，实际是虚张声势。
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

    # 品牌集中度：前 5 个品牌各占多少商品。这是"竞品格局"最直接的切面，
    # 而且不需要销量数据就能算——对只有真实采集数据的场景尤其有用。
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

    # ★ 排序必须确定：只按数量排的话，数量相同的店铺先后取决于 dict 插入顺序 →
    #   SQLite 返回顺序，同一个库换个索引或 SQLite 版本就可能变。后果不是"图不好看"，
    #   而是同样的数据算出不同 payload → 不同指纹 → **缓存永远不命中**。
    out.sort(key=lambda x: (-x["product_count"], x["shop"]))
    return out[:limit]


def _sample_products(
    products: list[dict[str, Any]], point_counts: dict[str, int], size: int
) -> tuple[list[dict[str, Any]], str]:
    """挑出要发给模型的那几十个商品，返回 (样本, 采样依据)。

    ★ 有销量时按销量降序——"卖得好的"显然是分析重点。
    ★ 没有销量时（真实电商的常态）**不能**按价格降序：样本会全是贵的，模型得出
      "该品类均价偏高"——这个结论完全由采样方式制造。改按"价格点数量降序"：
      被爆料次数最多的商品恰是市场上最活跃的那些，而且依据与价格无关，不会
      给模型植入价格上的偏见。
    ★ 兜底键（key）同样是必需的，理由见 _build_shops 里那段。
    """
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
            # ★ 标题截断到 40 个显示宽度：完整标题（含大量营销语）会吃掉一两千
            #   token，而营销话术不是这份报告要分析的。40 是实测值——能保住
            #   品牌 + 型号 + 规格 + 颜色（见 app/textutil.py）。
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

    ★ 用 `app.keywords.rank_keywords()`：词云图上的词和报告里说的词必须是同一份
      计算结果，否则"图上最大的词是『充电宝』、报告里却说『快充』是最高频词"
      这种矛盾就会出现，且两边都不报错。
    ★ 它还负责"还原人类写法"（iphone → iPhone）；不还原的话报告里会出现全小写
      型号名，读起来像是数据脏，其实是我们在分词时把它归一化了。
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

    ★ 返回的 dict 必须**只含 JSON 可序列化的类型**，且**不含任何时间戳或自增
      id**——`fingerprint.build_fingerprint()` 会强制检查，混进去会直接抛
      FingerprintError（而不是静默让缓存失效）。
    ★ 纯读取：不改库、不发网络请求，可反复调用，测试里也没有副作用。
    """
    products = _fetch_products(db, source)
    point_counts = _fetch_price_point_counts(db, source)

    prices = sorted(float(p["price"]) for p in products if p["price"] is not None)
    _step, price_buckets = bucket_prices(prices)

    sample, sample_basis = _sample_products(products, point_counts, sample_size)

    # ★ `note` 是给模型看的一句话，也是 payload 里唯一"不是数据"的东西，防的是
    #   "把采样之后的统计量当成全体的统计量说出来"。（system prompt 里也有一条
    #   对应的禁令，两层一起上。）
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
