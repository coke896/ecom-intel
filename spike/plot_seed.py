"""Mock 数据自检图 —— 在阶段① 就暴露"数据一眼假"。

================================================================================
为什么要在最早期画这三张图
================================================================================
Mock 数据的统计指标（中位数、变异系数、幂律指数）只能证明**数字**对，
证明不了**形状**对。

一个真实踩过的例子：价格序列最初写成 `p[t] = p[t-1] * (1 + ε)`，
所有统计指标都"正常"——中位数对、量级对、没有负值。
但画出来是一条**一路下滑的滑梯**：乘性误差累积导致 90 天后价格
只剩初始值的 12%。这个错误用数字查不出来，用眼睛一眼就看出来了。

所以这三张图不是"给别人看的演示素材"，而是**开发期的自检工具**：
  · 价格直方图   → 检验分布形状（对数正态应该是右偏的单峰，不是均匀的方块）
  · Top20 条形图 → 检验幂律梯度（应该每根柱都可见，不是一根高十九根贴地）
  · 单商品曲线   → 检验无漂移（应该围绕基准价波动，不是滑梯或单调上涨）

顺带它们也是 README 的配图——一份有数据分布图的 README，
和一份只有截图的 README，可信度完全不同。

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

# ★ 必须在 import pyplot 之前设置后端。
#   Windows 上默认后端会尝试开 GUI 窗口，在无头环境（CI、远程终端）会挂。
#   Agg 是纯文件输出后端，不需要显示器。
matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
from sqlalchemy import select  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.base import SessionLocal  # noqa: E402
from app.db.models import PriceHistory, Product  # noqa: E402

# 输出目录
CHARTS_DIR = Path(__file__).resolve().parent.parent / "docs" / "charts"

# ★ 中文字体设置。不设的话所有中文标签会变成一排方框（tofu），
#   图上写着"□□□□"，等于没画。
#   Windows 上用微软雅黑，Linux 上退到思源黑体。
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

    # ★ 分箱必须对齐价格网格（quantize_price 的 10 元步长），否则会出现混叠。
    #
    #   这是画第一版时踩到的：用 bins=50（约 12 元一箱）时，图上出现一串
    #   突兀的尖刺——因为 289.9 和 299.9 被分进同一箱、而 299.9 和 309.9
    #   被分进两箱，于是某些箱子是别人的两倍高。
    #
    #   那不是数据的问题，是**对一个离散变量用了任意宽度的箱子**。
    #   价格本来就是离散的（真实电商同样如此），把箱子对齐到网格，
    #   图才是诚实的。
    #
    # ★ 300 元以上会出现"空洞"，这也不是丢数据：
    #   quantize_price 的步长在 300 元处从 10 元跳到 50 元，
    #   所以 350 附近的取值只会是 349.9、400 附近的只会是 399.9——
    #   用 10 元宽的箱子去装，中间那些箱子自然是空的。
    #   真实电商的心理定价本来就是这样分段的，所以不修数据，
    #   而是让图**自己说明**这件事（见下面文本框里那一行）。
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

    # ★ 把关键统计量写在图里，这样这张图既是自检工具也是文档素材——
    #   看的人不用去翻代码就知道数据是用什么参数生成的。
    skew = float(np.mean(((prices - prices.mean()) / prices.std()) ** 3))
    ax.text(
        0.98, 0.95,
        f"样本 {len(prices)} 个\n"
        f"中位数 {med:.0f} 元\n"
        f"p5–p95  {np.percentile(prices, 5):.0f}–{np.percentile(prices, 95):.0f} 元\n"
        # ★ 不要用 ✓ 之类的符号字符：微软雅黑里没有这个字形，
        #   会渲染成一排方框，matplotlib 还会顺手打一条 UserWarning。
        f"偏度 {skew:.2f}（右偏）\n"
        # ★ 这一行是给看图的人（尤其是面试官）的解释：
        #   不给的话，300 元以上那串空洞会被误读成"数据丢了"。
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
    """按【显示宽度】截断：中文算 2 个单位，ASCII 算 1 个。

    ★ 为什么不能用 `t[:14]` 这种按字符数的截断：

      这是画这一版时发现的——图上出现了三条看起来一模一样的
      "绿联 磁吸手机指环扣 金属 …"，销量却是 45794 / 7595 / 7022，
      看起来像"同一个商品被记了三次、销量还不一样"这种严重的数据错误。

      实际去查库才发现 Top20 里 19 个标题都是唯一的：那三条是
      **不同颜色的 SKU**（深空灰 / 黑色 / 白色），而颜色恰好是
      标题的最后两个字，被字符截断切掉了。

      → 数据没错，是**图在骗人**。按字符数截断对中文天然不公平：
        15 个中文字占的宽度是 15 个英文字母的 1.8 倍左右，
        而"数码配件"这个品类的标题恰恰是"品牌中文 + 型号英文 + 规格 + 颜色"，
        最需要保留的颜色和规格都在尾部。
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

    # 宽度从 10 加到 11：标签预算放宽到 46 个单位后需要更多横向空间，
    # 否则 y 轴标签区会挤占绘图区，柱子变短。
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

    # 在柱子末端标数值
    for bar, val in zip(bars, top):
        ax.text(bar.get_width() * 1.01, bar.get_y() + bar.get_height() / 2,
                f"{val:,}", va="center", fontsize=8)

    # ★ 右侧留 12% 空白。
    #   不留的话，最长那根柱子的数值标签会被画到坐标轴外面裁掉，
    #   而偏偏被裁掉的是最显眼的第 1 名数值。
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
    # 挑价格波动有代表性的几个：按变异系数排序取中位数附近，
    # 而不是随机取——随机取可能全抽到没促销的平线，看不出问题。
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

        # ★ 基准线是这张图的重点：所有价格应该围绕它波动。
        #   如果曲线整体偏离基准线（跑成滑梯），一眼就能看出来——
        #   这正是累乘漂移 bug 的典型症状。
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
