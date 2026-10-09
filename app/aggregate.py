"""共享的聚合计算：分位数、直方图分桶（只有计算、没有 IO，可纯函数式测试）。

图和 GLM 报告必须用**同一份**聚合量：两边各实现一遍，就会出现"图上的价格分布"
和"报告里说的价格分布"对不上——而且**两边都不报错**，只有并排看才发现
（同一规则写两遍的坑，见 docs/可行性评估.md 14.1、14.4）。
"""

from __future__ import annotations

import math

# ---- 分位数 -----------------------------------------------------------------


def median(sorted_values: list[float]) -> float:
    """中位数。★ 入参必须**已排序**——排序错了不报错，只会得到一个错的数
    （调用方画直方图前本来就排过，再排一次没必要）。

    ★ 为什么不在 SQL 里算：SQLite 没有 MEDIAN 聚合函数。
    """
    n = len(sorted_values)
    if n == 0:
        raise ValueError("空列表没有中位数")
    mid = n // 2
    if n % 2:
        return float(sorted_values[mid])
    return (sorted_values[mid - 1] + sorted_values[mid]) / 2


def quantile(sorted_values: list[float], q: float) -> float:
    """线性插值分位数（等同于 numpy 的默认算法）。★ 手工实现而不是 import numpy：
    算几个分位点不值得把 numpy 拉进 Web 进程（它是离线脚本的依赖）。
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


# ---- 直方图分桶 -------------------------------------------------------------

# 分桶时优先用的"好看"的步长。★ 不吸附的话步长会是 38.4375 这种数，桶边界全是
# 小数，而读图的人要在脑子里做加法（"100~150 这一段有 200 个商品"）。
# ★ 25 是合法步长，所以"是不是好步长"**不能**用 `% 10 == 0` 判断
#   （测试里这么写过，在真实数据上炸了——见 docs/可行性评估.md 14.4）。
NICE_STEPS: tuple[int, ...] = (5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000)


def bucket_edges(lo: float, hi: float, target: int = 16) -> tuple[float, float, float]:
    """算出 (起始边界, 步长, 结束边界)：粗算步长后吸附到"好看"步长，边界外扩整数倍。
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
    """把**已排序**的价格分桶，返回 (步长, 桶列表)。桶的字段恰好是 `PriceBucket`
    需要的形状，调用方直接塞进去就行。

    ★ 空列表返回 `(0.0, [])`：空态文案由调用方决定——"为什么空"（没商品？
      还是没价格？）是接口层的事实，不该由分桶函数猜。
    """
    if not sorted_prices:
        return 0.0, []

    start, step, end = bucket_edges(sorted_prices[0], sorted_prices[-1], target=target)

    # 一次遍历分桶。★ 用 min(idx, n-1) 钳位：p 恰好等于最大值时下标是 n（越界），
    #   钳到 n-1 就把它放进最后一个桶。
    n = int(round((end - start) / step))
    counts = [0] * n
    for p in sorted_prices:
        idx = min(int((p - start) / step), n - 1)
        counts[idx] += 1

    buckets = [
        {
            # 标签用整数显示（"100-150"）：步长一定是整数，小数只会让坐标轴变长。
            "label": f"{int(start + i * step)}-{int(start + (i + 1) * step)}",
            "lower": float(start + i * step),
            "upper": float(start + (i + 1) * step),
            "count": int(counts[i]),
        }
        for i in range(n)
    ]
    return float(step), buckets
