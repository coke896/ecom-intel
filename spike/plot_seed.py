"""Mock 数据自检图：在阶段① 就暴露"数据一眼假"（统计指标证明不了形状对）。

三张图的用途：价格直方图 → 检验分布形状（右偏单峰）；Top20 条形图 → 检验幂律梯度；
单商品曲线 → 检验无漂移（曾有累乘误差让价格一路下滑成滑梯，数字查不出、图上一眼看穿）。
顺带它们也是 README 的配图。

用法：
    python spike/plot_seed.py            # 用已入库的数据画
    python spike/plot_seed.py --sample 4 # 画 4 个商品的价格曲线
"""

from __future__ import annotations

import argparse
import statistics as st
import sys
from pathlib import Path

import matplotlib
import numpy as np

# 必须在 import pyplot 之前设置后端：Windows 默认后端会开 GUI 窗口，无头环境（CI、远程终端）会挂。
# Agg 是纯文件输出后端。
matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
from sqlalchemy import select  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.base import SessionLocal  # noqa: E402
from app.db.models import PriceHistory, Product  # noqa: E402

# 输出目录
CHARTS_DIR = Path(__file__).resolve().parent.parent / "docs" / "charts"

# 中文字体：不设的话中文标签全变方框（tofu）；Windows 用微软雅黑，Linux 退到思源黑体。
plt.rcParams["font.sans-serif"] = [
    "Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "WenQuanYi Zen Hei",
]
plt.rcParams["axes.unicode_minus"] = False   # 负号也要用中文字体里的，否则同样变方框


def load_data() -> tuple[np.ndarray, list[int], list[int], dict[int, list[float]]]:
    """从库里读出画图需要的数据。"""
    with SessionLocal() as db:
        prices = np.array([
            p for (p,) in db.execute(
                select(Product.price).where(
                    Product.source == "mock", Product.price.is_not(None)
                )
            )
        ])
        sales_rows = db.execute(
            select(Product.title, Product.sales)
            .where(Product.source == "mock", Product.sales.is_not(None))
            .order_by(Product.sales.desc())
        ).all()

        series: dict[int, list[float]] = {}
        for pid, price, rec in db.execute(
            select(PriceHistory.product_id, PriceHistory.price, PriceHistory.recorded_at)
            .where(PriceHistory.product_id.in_(
                select(Product.id).where(Product.source == "mock").limit(400)
            ))
            .order_by(PriceHistory.product_id, PriceHistory.recorded_at)
        ):
            series.setdefault(pid, []).append(price)

    titles = [t for t, _ in sales_rows]
    sales = [int(s) for _, s in sales_rows]
    return prices, sales, titles, series


def plot_price_histogram(prices: np.ndarray) -> Path:
    """图 1：价格分布直方图。检验形状是右偏单峰，不是均匀方块。"""
    fig, ax = plt.subplots(figsize=(9, 5))

    # 分箱必须对齐价格网格（quantize_price 的 10 元步长），否则出现混叠；
    # >300 元出现"空洞"是 50 元步长定价所致（图里已注明），不是丢数据。
    grid = 10
    bins = np.arange(
        grid * np.floor(prices.min() / grid),
        grid * np.ceil(prices.max() / grid) + grid,
        grid,
    )
    ax.hist(prices, bins=bins, color="#4C8BF5", edgecolor="white", linewidth=0.5)

    med = float(np.median(prices))
    ax.axvline(med, color="#E8453C", linestyle="--", linewidth=1.5,
               label=f"中位数 {med:.0f} 元")

    ax.set_title("Mock 数据 · 商品价格分布（对数正态 σ=0.60）", fontsize=13)
    ax.set_xlabel("价格（元）")
    ax.set_ylabel("商品数")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)

    # 关键统计量写在图里：看图的人不用翻代码就知道数据用什么参数生成。
    skew = float(np.mean(((prices - prices.mean()) / prices.std()) ** 3))
    ax.text(
        0.98, 0.95,
        f"样本 {len(prices)} 个\n"
        f"中位数 {med:.0f} 元\n"
        f"p5–p95  {np.percentile(prices, 5):.0f}–{np.percentile(prices, 95):.0f} 元\n"
        # 不用 ✓ 之类符号：微软雅黑没有该字形，会渲染成方框并触发警告。
        f"偏度 {skew:.2f}（右偏）\n"
        # 给看图的人解释 >300 元的空洞，否则会被误读成"数据丢了"。
        f"注：>300 元为 50 元步长定价，故柱间隔变宽",
        transform=ax.transAxes, ha="right", va="top", fontsize=9,
        bbox=dict(boxstyle="round", facecolor="#F5F5F5", edgecolor="#CCCCCC"),
    )

    out = CHARTS_DIR / "price_distribution.png"
    fig.tight_layout()
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out


def elide(text: str, budget: int = 46) -> str:
    """按【显示宽度】截断：中文算 2 个单位、ASCII 算 1 个。

    按字符数截断会把中文标题尾部的颜色/规格切掉，图上出现假的"重复商品"。
    """
    out, width = [], 0
    for ch in text:
        w = 2 if ord(ch) > 0x2E80 else 1     # CJK 及全角标点算双宽
        if width + w > budget:
            return "".join(out) + "…"
        out.append(ch)
        width += w
    return text


def plot_sales_top(sales: list[int], titles: list[str], limit: int = 20) -> Path:
    """图 2：销量 Top20 条形图。检验幂律梯度每根柱都可见。"""
    top = sales[:limit]
    labels = [elide(t) for t in titles[:limit]]

    # 宽度 11：标签预算 46 单位需要更多横向空间，否则 y 轴标签挤占绘图区。
    fig, ax = plt.subplots(figsize=(11, 6))
    # 从深到浅的蓝色梯度，视觉上强化"名次"这个维度
    colors = plt.cm.Blues(np.linspace(0.85, 0.35, len(top)))[::-1]
    bars = ax.barh(range(len(top)), top, color=colors)

    ax.set_yticks(range(len(top)))
    ax.set_yticklabels(labels, fontsize=9)
    ax.invert_yaxis()          # 第 1 名在最上面
    ax.set_xlabel("销量")
    ax.set_title(f"Mock 数据 · 销量 Top{limit}（幂律 α=0.85）", fontsize=13)
    ax.grid(axis="x", alpha=0.3)

    for bar, val in zip(bars, top):
        ax.text(bar.get_width() * 1.01, bar.get_y() + bar.get_height() / 2,
                f"{val:,}", va="center", fontsize=8)

    # 右侧留 12% 空白：否则最长柱的数值标签会被裁掉——偏偏是最显眼的第 1 名。
    ax.set_xlim(0, max(top) * 1.12)

    ratio = top[0] / top[-1] if top[-1] else 0
    ax.text(
        0.98, 0.02,
        f"第1名 / 第{limit}名 = {ratio:.1f}x\n"
        f"（目标 8–25x：梯度清晰且每根柱可见）",
        transform=ax.transAxes, ha="right", va="bottom", fontsize=9,
        bbox=dict(boxstyle="round", facecolor="#F5F5F5", edgecolor="#CCCCCC"),
    )

    out = CHARTS_DIR / f"sales_top{limit}.png"
    fig.tight_layout()
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out


def plot_price_series(series: dict[int, list[float]], sample: int = 3) -> Path:
    """图 3：抽样商品的价格曲线。检验无漂移、促销有尖峰。"""
    # 按变异系数排序取中位数附近：随机取可能全抽到没促销的平线。
    scored = sorted(
        ((np.std(v) / np.mean(v), pid, v) for pid, v in series.items() if len(v) > 40),
        key=lambda x: x[0],
    )
    if not scored:
        raise SystemExit("库里没有足够的价格历史，先跑 python -m scraper.seed")
    mid = len(scored) // 2
    picks = scored[max(0, mid - sample // 2): mid - sample // 2 + sample]

    fig, axes = plt.subplots(sample, 1, figsize=(11, 2.6 * sample), sharex=True)
    if sample == 1:
        axes = [axes]

    for ax, (cv, pid, values) in zip(axes, picks):
        base = float(np.median(values))
        ax.plot(range(len(values)), values, color="#4C8BF5", linewidth=1.4)

        # 基准线是重点：价格应围绕它波动；整体偏离（滑成滑梯）正是累乘漂移 bug 的症状。
        ax.axhline(base, color="#E8453C", linestyle="--", linewidth=1.2,
                   label=f"基准 {base:.0f} 元")

        ax.set_ylabel("价格(元)", fontsize=9)
        ax.grid(alpha=0.25)
        ax.legend(fontsize=8, loc="upper right")

        final_ratio = values[-1] / values[0]
        ax.text(
            0.01, 0.95,
            f"商品 #{pid}   CV={cv:.3f}   末价/首价={final_ratio:.3f}",
            transform=ax.transAxes, va="top", fontsize=8.5,
            bbox=dict(boxstyle="round", facecolor="#F5F5F5", edgecolor="#CCCCCC"),
        )

    axes[0].set_title(
        "Mock 数据 · 抽样商品 90 天价格曲线（围绕基准波动，无漂移）",
        fontsize=12,
    )
    axes[-1].set_xlabel("第 N 天")

    out = CHARTS_DIR / "price_series_sample.png"
    fig.tight_layout()
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out


def print_metrics(
    prices: np.ndarray, sales: list[int], series: dict[int, list[float]]
) -> None:
    """把验收指标的实测值打出来，供 test_seed.py 定容差时参考。"""
    ratios = [v[-1] / v[0] for v in series.values() if v[0]]
    mdds = []
    for v in series.values():
        peak, m = v[0], 0.0
        for p in v:
            peak = max(peak, p)
            m = max(m, (peak - p) / peak)
        mdds.append(m)
    cvs = [np.std(v) / np.mean(v) for v in series.values() if np.mean(v)]
    mins = [min(v) / float(np.median(v)) for v in series.values()]

    top = sales[:20]
    print("\n=== 验收指标实测值 ===")
    print(f"{'指标':<30}{'实测':>10}")
    print("-" * 42)
    print(f"{'价格中位数':<28}{float(np.median(prices)):>10.1f}")
    print(f"{'最终价/初始价 中位数':<26}{st.median(ratios):>10.3f}")
    print(f"{'单商品最大回撤 中位数':<24}{st.median(mdds):>10.3f}")
    print(f"{'价格变异系数 CV 中位数':<24}{st.median(cvs):>10.3f}")
    print(f"{'最低价/基准价 中位数':<25}{st.median(mins):>10.3f}")
    print(f"{'Top1/Top20':<30}{top[0] / top[-1]:>10.1f}")
    print(f"{'价格最小/最大值':<25}{prices.min():>6.1f} / {prices.max():.1f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="画 Mock 数据自检图到 docs/charts/")
    parser.add_argument("--sample", type=int, default=3, help="抽样画几个商品的价格曲线")
    args = parser.parse_args()

    CHARTS_DIR.mkdir(parents=True, exist_ok=True)

    prices, sales, titles, series = load_data()
    if len(prices) == 0:
        raise SystemExit("库里没有 Mock 数据，先跑：python -m scraper.seed")

    print(f"载入 {len(prices)} 个商品、{len(series)} 条价格序列")

    for path in (
        plot_price_histogram(prices),
        plot_sales_top(sales, titles),
        plot_price_series(series, sample=args.sample),
    ):
        print(f"  → {path.relative_to(CHARTS_DIR.parent.parent)}")

    print_metrics(prices, sales, series)


if __name__ == "__main__":
    main()
