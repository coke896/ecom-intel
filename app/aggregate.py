"""共享的聚合计算：分位数、直方图分桶。

================================================================================
为什么这个文件必须存在（而不是让 stats.py 自己留着这几个函数）
================================================================================

阶段 3 要给 GLM 喂一份数据摘要。它需要的聚合量和界面上的四张图**完全一样**：
价格分位数、价格直方图、店铺占比、关键词。

如果让 `analysis/payload.py` 自己再实现一遍分桶，就会出现这样一个 bug：

    界面上的价格分布图（用 stats.py 的算法）和报告里说的
    "价格主要集中在 100-150 元区间"（用 payload.py 的算法）对不上。

而这个 bug 的表现形式极其恶劣：**两边都不报错，看起来都很正常**，
只有把图和报告并排放在一起看才会发现。面试官恰好就会这么看。

> 同一个规则写在两个地方，是阶段 2 已经栽过一次的坑
> （见 docs/可行性评估.md 14.1 和 14.4：空态文案漏了一处、
>   分桶步长的断言又描述了一遍规则）。
> 这次的应对是在写之前就抽出来，而不是等测试红了再抽。

注意本模块**只有计算、没有 IO**：不碰数据库、不碰 SQLAlchemy。
这样它可以被纯函数式地测试，也不会把 Web 层和采集层缠在一起。
"""

from __future__ import annotations

import math

# ---------------------------------------------------------------------------
# 分位数
# ---------------------------------------------------------------------------


def median(sorted_values: list[float]) -> float:
    """中位数。★ 入参必须**已排序**。

    ★ 为什么不用 statistics.median：标准库那个函数接受未排序的列表
      （它内部会检查并排序），而这里收到的列表动辄几千个元素、
      调用方本来就已经排好了（画直方图必须先排序拿到 min/max）。
      再排一次的代价没必要付——但代价是"必须已排序"成了隐含契约，
      所以写进 docstring 和函数名里。
      排序错了不会报错，只会得到一个错的数——这类错误最难查。

    ★ 为什么不在 SQL 里算：SQLite 没有 MEDIAN 聚合函数
      （PostgreSQL 也没有，要用 percentile_cont）。
    """
    n = len(sorted_values)
    if n == 0:
        raise ValueError("空列表没有中位数")
    mid = n // 2
    if n % 2:
        return float(sorted_values[mid])
    return (sorted_values[mid - 1] + sorted_values[mid]) / 2


def quantile(sorted_values: list[float], q: float) -> float:
    """线性插值分位数（等同于 numpy 的默认算法）。

    ★ 自己实现而不是 import numpy：只为算几个分位点就把 numpy
      拉进 Web 进程不划算（它在这个进程里唯一的用途就是这几行）。
      numpy 是 seed.py 的依赖，那是**离线脚本**，两者不该混。
    """
    if not sorted_values:
        raise ValueError("空列表没有分位数")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = (len(sorted_values) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(sorted_values[lo])
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


# ---------------------------------------------------------------------------
# 直方图分桶
# ---------------------------------------------------------------------------

# 分桶时优先用的"好看"的步长。
#
# ★ 为什么要吸附到这些数，而不是 range/16 直接算：
#   算出来的步长会是 38.4375 这种数，桶边界跟着变成
#   0 / 38.4 / 76.9 / 115.3…，坐标轴上全是小数，图很脏。
#   真实的分析图里桶边界一定是 10 的整数倍——不是为了好看，
#   而是因为读图的人要在脑子里做加法（"100~150 这一段有 200 个商品"），
#   边界不整的话这个加法做不了。
#
# ★ 注意 25 是合法步长，所以"是不是好步长"**不能**用 `% 10 == 0` 判断。
#   测试里曾经这么写过，在真实数据上炸了（算出 25）——
#   见 docs/可行性评估.md 14.4。
NICE_STEPS: tuple[int, ...] = (5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000)


def bucket_edges(lo: float, hi: float, target: int = 16) -> tuple[float, float, float]:
    """算出 (起始边界, 步长, 结束边界)。

    算法：先按目标桶数算一个粗略步长，再往上取到最近的"好看"步长，
    最后把首尾边界向外扩到步长的整数倍。
    """
    span = hi - lo
    raw = span / target if span > 0 else 1.0
    step = next((s for s in NICE_STEPS if s >= raw), NICE_STEPS[-1])

    start = math.floor(lo / step) * step
    end = math.ceil(hi / step) * step
    if end <= start:  # 所有价格相同（或只有一个商品）时的退化情况
        end = start + step
    return float(start), float(step), float(end)


def bucket_prices(
    sorted_prices: list[float], target: int = 16
) -> tuple[float, list[dict[str, float | int | str]]]:
    """把**已排序**的价格分桶，返回 (步长, 桶列表)。

    桶的字段是 `label` / `lower` / `upper` / `count`——
    恰好就是 `PriceBucket`（接口响应）和 payload 需要的形状，
    所以调用方直接把它塞进 Pydantic 模型或 JSON 都行。

    ★ 空列表返回 `(0.0, [])`，由调用方决定空态文案。
      这里不返回空态提示，是因为"为什么空"是接口层的事实
      （是没商品？还是没价格？），不该由分桶函数猜。
    """
    if not sorted_prices:
        return 0.0, []

    start, step, end = bucket_edges(sorted_prices[0], sorted_prices[-1], target=target)

    # 一次遍历分桶。★ 用 min(int((p-start)/step), n-1) 而不是 if/elif 链：
    #   p 恰好等于最大值 end 时，算出来的下标是 n（越界），
    #   钳到 n-1 就把最大值放进最后一个桶。这个边界每次都会写错一次。
    n = int(round((end - start) / step))
    counts = [0] * n
    for p in sorted_prices:
        idx = min(int((p - start) / step), n - 1)
        counts[idx] += 1

    buckets = [
        {
            # 标签用整数显示（"100-150" 而不是 "100.0-150.0"）——
            # 步长一定是整数，显示成小数只会让坐标轴变长。
            "label": f"{int(start + i * step)}-{int(start + (i + 1) * step)}",
            "lower": float(start + i * step),
            "upper": float(start + (i + 1) * step),
            "count": int(counts[i]),
        }
        for i in range(n)
    ]
    return float(step), buckets
