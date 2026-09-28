"""数据指纹 —— AI 报告缓存的键。

================================================================================
为什么用指纹而不是时间窗口
================================================================================

用"报告超过 N 小时就重新生成"做缓存，是很容易想到、但会让演示现场尴尬的方案：

    你点了"重新分析"，什么都没变，但刚好过了 N 小时——
    于是它真的重跑了一次，花了 30 秒，产出跟上次一模一样的报告。
    反过来，你刚灌完新数据、立刻点分析——没过 N 小时，于是它把
    **基于旧数据**的报告端给你，报告里说的数字和你屏幕上看到的对不上。

指纹把这件事反过来：**同样的输入必然命中缓存，不同的输入必然不命中。**
与时间无关。这个性质让"演示时到底会不会命中"变成一件可以预测的事。

================================================================================
两个方向都会错，而且错法完全不同
================================================================================

**方向一：把会变但不影响结论的东西算进去 → 缓存永远命中不了。**

自增 `id`、`created_at`、`last_crawled_at`、`updated_at` 都是这类。
最典型的事故是"重新跑了一次 seed"：数据内容一个字没变，
但每行的 `last_crawled_at` 都刷新了，于是指纹全变，缓存全部失效。
这个 bug 的表现是"缓存没生效"，于是你会去怀疑缓存库、怀疑写入没落库、
怀疑进程重启把内存清了——**唯独不会怀疑是键算错了**。

**方向二：漏算了会影响结论的东西 → 改了代码却命中旧缓存。**

`PROMPT_VERSION` 和 `model` 是这类。漏了它们，改了 prompt 之后
拿到的还是旧报告，于是你会怀疑热重载、怀疑浏览器缓存、怀疑没保存文件。
所以 `PROMPT_VERSION` 就在本模块里，**改 `prompts.py` 就必须改它**。

================================================================================
本模块的设计：把"不许混进时间戳"变成一条会报错的规则
================================================================================

`build_fingerprint()` 接收的是**整个 payload**（也就是马上要发给模型的那一份），
而不是商品行的列表。这样有一条强制保证：

    指纹覆盖的范围 == 模型看到的范围

这条性质很重要。如果指纹算的是全部 1000 个商品、而 prompt 只喂了销量前 50 个，
那么改动第 900 个商品会白白让缓存失效；更糟的是反过来——
prompt 里加了"平均价格"这个聚合量、指纹却没算它，
平均价格变了而缓存照样命中，报告就会**基于过期的数字说话而不自知**。

但"传整个 payload"也带来一个新风险：某天有人往 payload 里加了个
`generated_at`，缓存就再也命中不了了，而且不报错。
所以校验是**运行时强制**的（`_assert_fingerprint_safe`）：
发现 payload 里出现已知的时间戳字段名就抛异常，把静默失效变成响亮报错。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# ★ 改 prompts.py 里的任何模板，必须同步改这里。
#
#   为什么要人肉维护一个版本号，而不是把 prompt 模板本身算进指纹：
#   算模板内容看起来更自动，但它会让"改一个错别字"也失效全部缓存——
#   而调 prompt 的过程中你会改几十个错别字。
#   版本号的作用是让**你**决定什么时候让旧报告作废。
#
#   版本号约定：v1 / v2 / v3… 每次实质性改动 +1。
#   （不要用日期：v20260928 看不出改了几次，也没法比较先后。）
PROMPT_VERSION = "v1"

# 指纹进入数据库的 analysis_reports.fingerprint 列（String(64)）。
# sha256 十六进制正好 64 个字符——长度对得上不是巧合，是建表时就按它定的。
_HASH_CHARS = 64


class FingerprintError(ValueError):
    """payload 里混进了不该参与指纹的字段。

    单独定义一个异常类型而不是直接 `assert`：
    测试要断言"确实**因为正确的原因**失败"，而不是"确实失败了"。
    """


# 这些字段名一旦出现在 payload 里就判为错误。
#
# ★ 为什么用"字段名黑名单"这种笨办法，而不是给每个字段标注"要不要进指纹"：
#   黑名单是**兜底**的——它拦不住一个叫 `snapshot_time` 的新字段。
#   但它能拦住 99% 的情况（时间戳字段的名字高度趋同），
#   而且误伤成本很低（真需要就叫别的名字）。
#   相比"什么都不拦"，这一点点不完美换来的是"再也不会静默失效"。
_FORBIDDEN_KEYS = frozenset({
    "id",
    "created_at",
    "updated_at",
    "last_crawled_at",
    "crawled_at",
    "generated_at",
    "recorded_at",
    "snapshot_at",
    "at",          # 兜底：很多时间字段会以 _at 结尾，但也有直接叫 at 的
    "now",
    "timestamp",
})


def _key_shape(key: str) -> str:
    """把字段名压成"只有字母数字"的形状，用来做同形比对。

    `created_at` / `createdAt` / `created-at` / `created at` → 全部是 `createdat`

    ★ 这个函数是**被测试逼出来的**。原本的写法是 `key.lower() in _FORBIDDEN_KEYS`，
      看起来已经是"大小写不敏感"了，但它只处理大小写、不处理分隔符——
      于是 `CreatedAt`（从任何 JS/JSON 系统抄过来的字段名都是这个形状）
      会**稳稳地穿过**这道兜底，而注释里写着"能拦住 99% 的情况"。

      这个缺口很隐蔽：黑名单的全部价值就是拦住"无意中加进来的字段"，
      而无意中加进来的字段，名字形态恰恰是不可预测的。

    ★ 用"压平后完全相等"而不是"包含子串"来匹配：
      子串匹配会误伤 `source_id`、`shop_id`、`valid`、`candidate` 这些
      含 "id" 的合法字段名——而误伤的代价是**分析接口直接 500**，
      比漏拦一个字段严重得多。
    """
    return "".join(ch for ch in key.lower() if ch.isalnum() and ch.isascii())


# 黑名单的同形集合，在模块加载时算一次。
# 里面只有字母数字，所以只可能是"实实在在就是同一个名字"的变体——
# `source_id` 压平后是 `sourceid`，与 `id` 不相等，安全。
_FORBIDDEN_SHAPES = frozenset(_key_shape(k) for k in _FORBIDDEN_KEYS)

# ★ 注意这里**没有** `source_id`。
#   它是业务主键（"smzdm:183089533"），必须进指纹——它是商品的身份。
#   被拦的只有**自增主键** `id`。
#   两者的区别是"稳定与否"：自增 id 在重灌数据后会变，source_id 不会。


def _assert_fingerprint_safe(node: Any, path: str = "payload") -> None:
    """递归检查 payload 里没有会破坏缓存的字段。

    ★ 递归而不是只看第一层：payload 是嵌套的
      （`{"summary": {...}, "top_products": [{...}]}`），
      时间戳最可能出现在最内层的商品行里——那正是要检查的地方。
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

    ★ 这一步不是"顺手格式化"，它是必需的。

      同一个价格在 Python 里可能有多种表示：
        199      （int，来自 JSON）
        199.0    （float）
        "199.00" （字符串，来自某些数据源）
        198.99999999999997  （浮点运算的产物）

      它们 `==` 起来是相等的（字符串除外），但 `repr()` 出来是不同的字符串，
      于是 json.dumps 出来的字节不同 → sha256 不同 → **缓存不命中**。
      这是"数据没变但缓存就是不命中"最常见的来源。

      统一成 `round(float(x), 2)` 之后，上面四种全部映射到 `199.0`，
      序列化结果一致。
    """
    if value is None or value == "":
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        # 价格字段脏掉时不要抛异常：指纹算不出来会让整个分析接口 500，
        # 而"某个商品价格是脏的"不该让整份报告生成不了。
        # 归一到一个固定的哨兵值，保证同样的脏数据得到同样的指纹。
        return None


def _normalize_text(value: Any) -> str:
    """文本归一化。

    ★ 只做 `strip()`，**不做**大小写折叠或去空格。

      原因是这两种处理的取舍方向相反：
        · 指纹算得越"松"（把 iPhone/iphone 视作相同）→ 缓存命中率越高，
          但"标题从 iPhone 改成 iphone"这种变化就不会触发重新分析
        · 算得越"紧" → 任何改动都重新分析，但缓存命中率低

      这里选"紧"。因为标题大小写在这个项目里几乎不会变（它是采集来的原文），
      所以"松"带来的命中率收益接近于零，而它带来的风险是真的。
    """
    return str(value or "").strip()


def _normalize_product_row(row: dict[str, Any]) -> dict[str, Any]:
    """把一行商品数据归一化到"可稳定序列化"的形式。

    ★ 显式列出要保留的字段，而不是 `{k: v for k, v in row.items()}`。

      白名单和黑名单在这里的区别很关键：
        黑名单（排除 id/时间戳）→ 将来 payload 里加了新字段，
                                  它会**自动**进指纹，可能与期望相反
        白名单（只保留这两个）  → 新字段默认**不进**指纹，
                                  需要时显式加，改动是可见的

      指纹需要的是"稳定"，而白名单天然更稳定——新字段默认不参与，
      不会因为某个上游代码加了个字段就悄悄改变所有缓存键。
    """
    return {
        "key": _normalize_text(row.get("key")),
        "title": _normalize_text(row.get("title")),
        "price": _round_price(row.get("price")),
        "price_max": _round_price(row.get("price_max")),
        "sales": _as_int(row.get("sales")),
        "brand": _normalize_text(row.get("brand")),
        # ★ platform / category 是补进来的两个字段。它们的判据和 `points` 完全一样
        #   （见下），所以漏掉的后果也完全一样——但它们**为什么能漏掉**值得记一笔：
        #
        #   验证"每个字段都进指纹"那句话的测试清单，是照着这个白名单写的
        #   （tests/test_fingerprint.py 的 _row() 8 个键、参数化清单 7 个字段），
        #   而白名单又和真实行高度重合。于是"真实行里有、这里没有"的字段
        #   在实现、夹具、清单**三处同时不存在**，谁也不会发现——
        #   本文件把"必须在指纹里"写了两遍（模块注释里那条不变式
        #   "指纹覆盖的范围 == 模型看到的范围"，以及下面 points 那一段），
        #   而两处都只是**声明**，没有任何一条路径把声明和真实行对起来。
        #
        #   ★ 所以补完之后，保障换成了从真实 payload 推导的那条用例：
        #     tests/test_fingerprint.py::test_every_field_of_a_real_product_row_reaches_the_fingerprint
        #     （逐个键改一遍，看指纹认不认）。声明和现实对上之后，
        #     漏字段这件事才第一次有了会变红的路径。
        #
        #   注：`platform`/`category` 进指纹会让**同一批数据的指纹值与从前不同**，
        #   于是库里已有的旧报告不会再被命中（它们本身没错，只是键对不上了）。
        #   这是安全的方向——多算一次，而不是拿旧结论冒充新的。
        #   不需要动 PROMPT_VERSION：prompt 本身一个字没改。
        "platform": _normalize_text(row.get("platform")),
        "category": _normalize_text(row.get("category")),
        "shop": _normalize_text(row.get("shop")),
        # ★ `points`（该商品被爆料过几次）也要进指纹，因为它确实在 prompt 里。
        #   这一条的判据始终是同一个：**出现在 prompt 里的每个字段都必须进指纹**。
        #   漏掉的后果是"该商品的观测次数从 1 变成 12、报告却还在说
        #   『这个商品只有一次性报价』"——报告没变，但数据变了。
        "points": _as_int(row.get("points")),
    }


def _as_int(value: Any) -> int | None:
    """销量归一化。★ 同样是为了让它稳定序列化。

    `1000`（int）和 `1000.0`（float）在 json.dumps 后是 `1000` 和 `1000.0`，
    字节不同、指纹不同。销量在 Mock 数据里是 int，
    但从 CSV 或 JSON 接口读进来时可能是 float 或字符串。
    """
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def build_fingerprint(payload: dict[str, Any], model: str) -> str:
    """算出该 payload + 该模型下的 sha256 指纹（64 位十六进制）。

    参数
    ----
    payload
        即将发给模型的**完整**输入（见 `payload.build_payload()`）。
        必须是一个纯数据 dict（只含 JSON 可序列化的类型）。
    model
        模型名。★ 必须进指纹：换模型（glm-4-flash → glm-4-plus）
        会让同样的数据得到不同的结论，缓存必须失效。

    返回
    ----
    64 个字符的十六进制字符串，可直接存进 `analysis_reports.fingerprint`。

    ★ 为什么把 `model` 做成参数而不是塞进 payload：
      它是一个**调用参数**，不是数据。混进 payload 的话，
      它就会出现在发给模型的 JSON 里，占掉几十个 token 且毫无意义。
    """
    _assert_fingerprint_safe(payload)

    # ---- 归一化 ----
    # 深拷贝着改，不改调用方传进来的对象。
    rows = [_normalize_product_row(r) for r in (payload.get("top_products") or [])]

    # ★ 排序。不排的话，两次查询的返回顺序不同（比如加了索引之后
    #   SQLite 换了扫描方式）→ 指纹不同 → 缓存永远命中不了。
    #
    #   排序键用 `key`（"source:source_id"），它是**唯一**的，
    #   所以排序结果是全序、与输入顺序完全无关。
    #   不用 title 排：标题可能重复（不同颜色 SKU 归一化后可能同名），
    #   重复键会让排序结果依赖 Python 的稳定排序行为，留下隐患。
    rows.sort(key=lambda r: r["key"])

    normalized = {
        "prompt_version": PROMPT_VERSION,
        "model": model,
        "source": _normalize_text(payload.get("source")),
        # 汇总区（含分位数、直方图、关键词）原样进指纹。
        # 它们是**派生量**，但正因为如此才更要算——它们直接出现在 prompt 里，
        # 变了就必须重新分析。里面不含 id 也不含时间戳，_assert 已经验过。
        "summary": payload.get("summary") or {},
        "price_buckets": payload.get("price_buckets") or [],
        "shops": payload.get("shops") or [],
        "keywords": payload.get("keywords") or [],
        "top_products": rows,
    }

    # separators 去掉空格、ensure_ascii=False 让中文原样编码。
    #
    # ★ 这两个参数对指纹的正确性没有影响（无论怎么序列化，
    #   同样的输入都会得到同样的输出），但 ensure_ascii=True 会把每个中文字
    #   变成 \uXXXX 六个字节——1000 个标题的 payload 会膨胀 6 倍。
    #   sort_keys=True 则让 dict 的键序确定，否则 Python 3.7+ 虽然
    #   保序、但两个"内容相同、构造顺序不同"的 dict 会算出不同指纹。
    blob = json.dumps(
        normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()

    # 长度自检。String(64) 在 SQLite 上不强制长度（它会照存），
    # 换到 PostgreSQL 时超长会直接报错——在这里拦住比在数据库那里拦住早得多。
    assert len(digest) == _HASH_CHARS, f"sha256 十六进制应为 64 字符，实际 {len(digest)}"
    return digest
