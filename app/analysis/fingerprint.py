"""数据指纹 —— AI 报告缓存的键。

不用"报告超过 N 小时就重新生成"：**同样的输入必然命中，不同的输入必然不命中**，
与时间无关——这样"演示时到底会不会命中"才是可预测的。

两个方向都会错：把会变但不影响结论的东西（自增 id、created_at、last_crawled_at）
算进去 → 重跑一次 seed 就让缓存全量失效，而人会去怀疑缓存库、怀疑进程重启，
唯独不会怀疑是键算错了；漏算影响结论的东西（PROMPT_VERSION、model）→ 改了
prompt 却命中旧报告。**改 prompts.py 就必须改 PROMPT_VERSION**。

`build_fingerprint()` 收的是**整个 payload**（马上要发给模型的那一份），保证
"指纹覆盖的范围 == 模型看到的范围"；运行时强制的字段名检查
（_assert_fingerprint_safe）把"payload 混进时间戳"从静默失效变成响亮报错。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# ★ 改 prompts.py 里的任何模板，必须同步改这里。
#
#   为什么人肉维护一个版本号、而不是把 prompt 模板本身算进指纹：后者会让
#   "改一个错别字"也作废全部缓存，而调 prompt 的日常就是改几十个错别字。
#   版本号让你决定什么时候让旧报告作废；约定 v1 / v2 / v3…，实质性改动 +1
#   （不用日期：看不出改了几次，也没法比较先后）。
#
#   v2（2026-09-28）：说清 price_buckets 只覆盖**有价格**的商品——原文写的
#   "全部商品"是错的，模型可能拿 bucket 的计数去除以 product_count。
PROMPT_VERSION = "v2"

# 指纹进入数据库的 analysis_reports.fingerprint 列（String(64)）。
# sha256 十六进制正好 64 个字符——长度对得上不是巧合，是建表时就按它定的。
_HASH_CHARS = 64


class FingerprintError(ValueError):
    """payload 里混进了不该参与指纹的字段。

    单独定义一个异常类型而不是直接 `assert`：
    测试要断言"确实**因为正确的原因**失败"，而不是"确实失败了"。
    """


# 这些字段名一旦出现在 payload 里就判为错误。
# ★ 用"字段名黑名单"这种笨办法是兜底：它拦不住一个叫 `snapshot_time` 的新字段，
#   但时间戳字段名高度趋同，能拦住绝大多数，而误伤成本很低（真需要就叫别的名字）。
_FORBIDDEN_KEYS = frozenset({
    "id",
    "created_at",
    "updated_at",
    "last_crawled_at",
    "crawled_at",
    "generated_at",
    "recorded_at",
    "snapshot_at",
    "at",          # 兜底：也有直接叫 at 的时间字段
    "now",
    "timestamp",
})


def _key_shape(key: str) -> str:
    """把字段名压成"只有字母数字"的形状，用来做同形比对。

    `created_at` / `createdAt` / `created-at` / `created at` → 全部是 `createdat`

    ★ 只做 `key.lower()` 不够：它处理大小写、不处理分隔符，于是 `CreatedAt`
      （从任何 JS/JSON 系统抄来的字段名都是这个形状）会稳稳穿过黑名单
      （复盘见 docs/可行性评估.md 15.2）。
    ★ 用"压平后完全相等"而不是"包含子串"：子串匹配会误伤 `source_id`、`shop_id`、
      `valid`、`candidate` 这些含 "id" 的合法字段名，而误伤代价是分析接口直接 500。
    """
    return "".join(ch for ch in key.lower() if ch.isalnum() and ch.isascii())


# 黑名单的同形集合，在模块加载时算一次。里面只有字母数字，所以只可能是
# "实实在在就是同一个名字"的变体——`source_id` 压平后是 `sourceid`，与 `id` 不等。
_FORBIDDEN_SHAPES = frozenset(_key_shape(k) for k in _FORBIDDEN_KEYS)

# ★ 注意这里**没有** `source_id`：它是业务主键（"smzdm:183089533"），必须进指纹——
#   它是商品的身份。被拦的只有**自增主键** `id`。区别是"稳定与否"：自增 id 在
#   重灌数据后会变，source_id 不会。


def _assert_fingerprint_safe(node: Any, path: str = "payload") -> None:
    """递归检查 payload 里没有会破坏缓存的字段。

    ★ 递归而不是只看第一层：payload 是嵌套的，时间戳最可能出现在最内层的
      商品行里——那正是要检查的地方。
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key.lower() in _FORBIDDEN_KEYS or _key_shape(key) in _FORBIDDEN_SHAPES:
                raise FingerprintError(
                    f"payload 的 {path}.{key} 是一个会变的字段，不能参与指纹计算。\n"
                    f"  · 如果是自增 id → 改用业务主键（source + source_id）\n"
                    f"  · 如果是时间戳 → 它只该在渲染报告时加，不该进 payload\n"
                    f"  （理由见 app/analysis/fingerprint.py 的模块注释）"
                )
            # 值本身也可能是嵌套的，继续往下走
            _assert_fingerprint_safe(value, f"{path}.{key}")
    elif isinstance(node, (list, tuple)):
        for i, item in enumerate(node):
            _assert_fingerprint_safe(item, f"{path}[{i}]")


def _round_price(value: Any) -> float | None:
    """价格归一化到 2 位小数。

    ★ 这一步是必需的，不是"顺手格式化"：同一个价格可能有 199（int）、199.0、
      "199.00"、198.99999999999997 几种表示，`==` 相等但 repr() 不同 → 序列化
      出的字节不同 → sha256 不同 → **缓存不命中**（"数据没变但缓存就是不命中"
      最常见的来源）。统一成 `round(float(x), 2)` 后全部映射到 `199.0`。
    """
    if value is None or value == "":
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        # 价格字段脏掉时不要抛异常：指纹算不出来会让整个分析接口 500，
        # 而"某个商品价格是脏的"不该让整份报告生成不了。归一到一个固定的
        # 哨兵值，保证同样的脏数据得到同样的指纹。
        return None


def _normalize_text(value: Any) -> str:
    """文本归一化。

    ★ 只做 `strip()`，**不做**大小写折叠或去空格——指纹算得越松（iPhone/iphone
      视作相同）命中率越高，但"标题从 iPhone 改成 iphone"这种变化就不会触发
      重新分析。标题是采集来的原文、几乎不变，所以松的收益接近于零，风险是真的。
    """
    return str(value or "").strip()


def _normalize_product_row(row: dict[str, Any]) -> dict[str, Any]:
    """把一行商品数据归一化到"可稳定序列化"的形式。

    ★ 显式列出要保留的字段（白名单），而不是遍历 `row.items()`：白名单下新字段
      默认**不进**指纹、需要时显式加，改动是可见的；黑名单则会让上游新增字段
      **自动**改变全部缓存键。指纹需要的是"稳定"，白名单天然更稳定。
    """
    return {
        "key": _normalize_text(row.get("key")),
        "title": _normalize_text(row.get("title")),
        "price": _round_price(row.get("price")),
        "price_max": _round_price(row.get("price_max")),
        "sales": _as_int(row.get("sales")),
        "brand": _normalize_text(row.get("brand")),
        # ★ platform / category 同样出现在 prompt 里，必须进指纹。补上它们会让
        #   旧指纹对不上、库里已有的旧报告不再命中——这是安全方向（多算一次，
        #   而不是拿旧结论冒充新的）；prompt 本身没改，不需要动 PROMPT_VERSION。
        "platform": _normalize_text(row.get("platform")),
        "category": _normalize_text(row.get("category")),
        "shop": _normalize_text(row.get("shop")),
        # ★ `points`（该商品被爆料过几次）也在 prompt 里，必须进指纹。判据始终是
        #   同一个：**出现在 prompt 里的每个字段都必须进指纹**。漏掉的后果是
        #   "观测次数从 1 变成 12、报告还在说『这个商品只有一次性报价』"。
        "points": _as_int(row.get("points")),
    }


def _as_int(value: Any) -> int | None:
    """销量归一化。★ 同样是为了让它稳定序列化。

    `1000`（int）和 `1000.0`（float）在 json.dumps 后字节不同、指纹不同，
    而销量可能是 int、float 或字符串。
    """
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def build_fingerprint(payload: dict[str, Any], model: str) -> str:
    """算出该 payload + 该模型下的 sha256 指纹（64 位十六进制）。

    payload 是即将发给模型的**完整**输入（见 `payload.build_payload()`）；
    model 必须进指纹——换模型（glm-4-flash → glm-4-plus）会让同样的数据得到
    不同的结论，缓存必须失效。
    ★ `model` 做成参数而不是塞进 payload：它是**调用参数**，不是数据，
      混进去就会出现在发给模型的 JSON 里，占掉几十个 token 且毫无意义。
    """
    _assert_fingerprint_safe(payload)

    # ---- 归一化 ----
    # 深拷贝着改，不改调用方传进来的对象。
    rows = [_normalize_product_row(r) for r in (payload.get("top_products") or [])]

    # ★ 排序。不排的话，两次查询的返回顺序不同（比如加了索引之后 SQLite 换了
    #   扫描方式）→ 指纹不同 → 缓存永远命中不了。
    #   排序键用 `key`（"source:source_id"），它是**唯一**的，排序结果是全序、
    #   与输入顺序完全无关；不用 title（可能重复，重复键会让结果依赖稳定排序行为）。
    rows.sort(key=lambda r: r["key"])

    normalized = {
        "prompt_version": PROMPT_VERSION,
        "model": model,
        "source": _normalize_text(payload.get("source")),
        # 汇总区（含分位数、直方图、关键词）原样进指纹。它们是**派生量**，但直接
        # 出现在 prompt 里，变了就必须重新分析；里面不含 id 也不含时间戳（已验过）。
        "summary": payload.get("summary") or {},
        "price_buckets": payload.get("price_buckets") or [],
        "shops": payload.get("shops") or [],
        "keywords": payload.get("keywords") or [],
        "top_products": rows,
    }

    # separators 去掉空格、ensure_ascii=False 让中文原样编码。
    # ★ 这对指纹的正确性没有影响（同样的输入总是同样的输出），但 ensure_ascii=True
    #   会把每个中文字变成六个字节、payload 膨胀 6 倍；sort_keys=True 让 dict 键序
    #   确定，否则"内容相同、构造顺序不同"的两个 dict 会算出不同指纹。
    blob = json.dumps(
        normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()

    # 长度自检。String(64) 在 SQLite 上不强制长度（它会照存），
    # 换到 PostgreSQL 时超长会直接报错——在这里拦住比在数据库那里拦住早得多。
    assert len(digest) == _HASH_CHARS, f"sha256 十六进制应为 64 字符，实际 {len(digest)}"
    return digest
