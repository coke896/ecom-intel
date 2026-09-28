"""Mock 数据生成的测试。

================================================================================
这份测试在防什么
================================================================================
不是"防代码写错"——代码写错跑一次就能看出来。它防的是**分布悄悄漂移**：

调参阶段你会反复改 σ、α、PROMO_PROB，每次改完图表都会变，
而"变好看了"和"变假了"之间没有清晰界限。等到阶段② 前端画完图才发现
"数据分布不对"，那时候已经不知道是哪个参数改坏的。

所以这里把**当前标定出来的分布特征固化成断言**。
再改参数时，测试会告诉你动了什么。

================================================================================
两处对计划验收标准的【诚实修订】
================================================================================
计划第七节写的是：
    ① 价格全在 [50,800]   ② 中位数 150±15
    ③ Top20 严格递减      ④ 最终/初始 ∈ [0.7,1.4]

实测后发现其中两条站不住，这里必须说明白，而不是悄悄放宽：

【修订 1】"价格全在 [50,800]"只对**基准价**成立，对**价格序列**不成立。
    促销的定义就是降价，降到 39.7 元是**正确行为**，不是数据缺陷。
    实测最低价 39.7（基准 50.9 的商品打了 8 折）。
    → 测试拆成两条：基准价严格在 [50,800]；价格序列只要求 > 0。

【修订 2】"Top20 严格递减"与计划自己的设计**自相矛盾**。
    计划第六节同时写了"×个体扰动（避免完美单调）"和"Top20 严格递减"——
    前者必然导致后者不成立，二者只能留一个。
    实测保留了个体扰动（±8%），因为它更真实：真实榜单尾部本来就有交错。
    → 测试改成断言**幂律指数**（log-log 回归斜率 ≈ -0.85），
      这才是 α=0.85 这个参数真正要保证的东西；
      再加一条 top1/top20 ∈ [8,25] 保证"梯度清晰且每根柱可见"。

================================================================================
实测基准值（seed=20260928, 1000 商品 × 90 天）
================================================================================
    价格中位数                142.4
    最终价/初始价 中位数       0.997
    单商品最大回撤 中位数       0.376
    价格变异系数 CV 中位数      0.126
    最低价/基准价 中位数        0.642
    Top1/Top20               12.1
    价格最小/最大值            37.1 / 651.9

★ 关于 CV：计划标的是 0.107，实测 0.126，且把 PROMO_PROB 从 0.07 降到 0.05
  只让它从 0.128 动到 0.124——说明方差主要来自促销的**深度**（降幅 10~40%
  是很有攻击性的取值），不是频率。
  不再继续调参的原因是：那个 0.107 出自我看不到的参考实现，
  继续逼近就是在拟合一个未知目标。数据本身是合理的
  （促销天数占比约 25%、最大回撤 38%、最低价打 6.4 折），
  所以按实测值定容差，并在此如实记录差异。

================================================================================
图表抓到了统计指标抓不到的问题（第二次）
================================================================================
seed.py 的模块注释里已经记了一次：价格累乘漂移那个 bug，所有统计指标都"正常"，
是价格曲线图一眼看出来的。

这里再记一次，因为它印证了同一件事，而且更隐蔽：

  大促日（第 30/75 天）被普通促销覆盖而跳过的 bug，
  **有一个测试一直在跑并且一直通过**——
  test_big_promo_days_create_a_detectable_trough 断言"200 个商品的
  日均值在大促日更低"。它确实更低，因为 70% 的商品是对的，
  平均下来就把 30% 的缺失抹平了。

  是价格曲线图暴露的：图上有几个商品第 30 天压根没有低谷。

  → 教训：**聚合指标会掩盖个体差异**。
    凡是设计意图里带"全品类""每个商品都"这类全称量词的，
    断言就必须落在个体上，不能落在均值上。
    补强的 test_every_single_product_has_the_big_promo 就是逐商品的，
    它在修复前报出 7/240 缺失。
"""

from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import func, select

from app.db.models import Alert, AlertRule, PriceHistory, Product, Shop
from scraper import seed as seed_mod
from scraper.dedup import normalize_title
from scraper.seed import (
    BIG_PROMO_DAYS,
    DEFAULT_DAYS,
    DEFAULT_PRODUCT_COUNT,
    DEFAULT_SEED,
    PRICE_MAX,
    PRICE_MIN,
    PROMO_PROB,
    SALES_ALPHA,
    build_products,
    gen_price_series,
    sample_base_prices,
    sample_ranked_sales,
    vary_specs,
)

# 测试里统一用小规模：跑得快，且失败时不用等 90 天 × 1000 商品。
# ★ 这些都是【纯函数】测试，不碰数据库——所以可以放心用任意数量。
T_COUNT = 60
T_DAYS = 30


# ===========================================================================
# 一、基准价格分布（对数正态 + 拒绝采样 + 心理定价）
# ===========================================================================


def test_base_prices_within_hard_range() -> None:
    """基准价必须落在 [50, 800]——这是拒绝采样保证的。

    注意断言用的是 PRICE_MIN/PRICE_MAX 常量本身，
    所以将来改参数时测试不会假失败（它测的是"约束生效"，
    不是"约束等于某个具体值"）。
    """
    prices = sample_base_prices(np.random.default_rng(1), 500)
    assert len(prices) == 500, "拒绝采样凑不满数量时必须报错，不能静默返回短数组"
    assert prices.min() >= PRICE_MIN
    assert prices.max() <= PRICE_MAX


def test_base_price_median_is_around_150() -> None:
    """中位数应该在 150 附近——μ=ln(150) 的全部意义。

    容差 ±15 是刻意的：对数正态的样本中位数本身有抽样波动，
    n=2000 时标准误约 150×0.6/√2000 ≈ 2 元，±15 留了近 7 倍余量。
    真要漂到区间外，说明 μ 被改错了，不是运气问题。
    """
    prices = sample_base_prices(np.random.default_rng(2), 2000)
    assert 135 <= float(np.median(prices)) <= 165


def test_base_prices_are_right_skewed() -> None:
    """必须是右偏的单峰分布，不是均匀分布。

    ★ 这条比中位数更重要：均匀分布的中位数同样是 150，
      但它画成直方图是一个方块，"价格分布"图毫无信息量。
      偏度 > 0 才能把对数正态和均匀分布区分开。
    """
    prices = sample_base_prices(np.random.default_rng(3), 3000)
    z = (prices - prices.mean()) / prices.std()
    skew = float(np.mean(z**3))
    assert skew > 0.8, f"偏度 {skew:.2f} 太小，不像对数正态分布"


def test_no_pile_up_at_boundaries() -> None:
    """边界上不能有堆叠——这是"拒绝采样 vs np.clip"的判别式。

    ★ 这是本模块最容易偷懒改错的地方：把拒绝采样换成 np.clip 之后，
      越界值会被**压成一个相同的数**，直方图两端各出现一根突兀的尖柱。

    ★ 判别式是【同一个数值上的精确堆叠】，不是"边界附近密度高"。

      这一点我第一版就写错了：当时用了"落在边界 5% 区间内的占比"
      （即价格 ≤ 87.5），断言 < 2%，结果实测 15.4% 直接失败。
      但那是**测试写错了，不是数据有问题**——对数正态 σ=0.6 时
      ≤87.5 这一段本来就占约 15% 的密度（p5 只有 56 元）。
      密度高是分布的自然形状，堆叠才是 bug 的特征。

      clip 的签名是：raw < 50 的那 3.3% 全部变成**同一个值** 50.9。
      所以直接数"有多少个样本等于最小值"即可——
      拒绝采样下这需要 raw 恰好落在 [50,51)，实测约 0.1%；
      clip 下是 3.3%。两者差 30 倍，判别毫无歧义。
    """
    n = 3000
    prices = sample_base_prices(np.random.default_rng(4), n)

    at_min = int((prices == prices.min()).sum())
    at_max = int((prices == prices.max()).sum())

    assert at_min / n < 0.015, (
        f"{at_min/n:.1%} 的样本都等于最小值 {prices.min()}，"
        "这是 np.clip 把越界值堆在边界的典型症状"
    )
    assert at_max / n < 0.015, (
        f"{at_max/n:.1%} 的样本都等于最大值 {prices.max()}，疑似用了 np.clip"
    )


def test_prices_snap_to_retail_psychology() -> None:
    """价格必须以 0.9 结尾——真实电商不出现 187.34 元。

    不做这一步的话，前端 tooltip 里会是一堆两位小数，
    一眼就看出是随机数生成的。
    """
    prices = sample_base_prices(np.random.default_rng(5), 500)
    for p in prices:
        frac = round(float(p) - int(p), 2)
        assert frac == pytest.approx(0.9), f"{p} 没有吸附到 .9 结尾"


def test_rejection_sampling_raises_instead_of_returning_short(monkeypatch) -> None:
    """参数自相矛盾时必须显式报错，不能静默返回短数组。

    ★ 为什么这条值得测：如果返回的是短数组，
      下游会拿到一个长度不对的价格列表，然后 `bases[i]` 抛 IndexError，
      报错位置离真正的病因（参数被改坏）隔了十万八千里。
      在源头 loud failure 便宜得多。
    """
    monkeypatch.setattr(seed_mod, "PRICE_MIN", 1e9)
    monkeypatch.setattr(seed_mod, "PRICE_MAX", 1e9 + 1)
    with pytest.raises(RuntimeError, match="拒绝采样"):
        seed_mod.sample_base_prices(np.random.default_rng(6), 5)


# ===========================================================================
# 二、价格序列（防漂移 —— 本项目踩过的最大的坑）
# ===========================================================================


def test_price_series_never_goes_nonpositive() -> None:
    """价格序列里不能出现 0 或负数。

    ★ 不加下限的话，"极端噪声 + 大促"叠加时会算出负价格，
      然后 price_history 里出现负值，前端折线图的坐标轴直接崩。
    """
    rng = np.random.default_rng(7)
    for _ in range(50):
        series = gen_price_series(rng, 60.0, 90)   # 用低价商品，最容易触底
        assert all(p > 0 for p in series), "价格序列出现非正值"


def test_no_compounding_drift() -> None:
    """★ 本项目踩过的最大的坑：价格不能逐日累乘。

        错： p[t] = p[t-1] * (1 + ε)    乘性误差累积，90 天偏离 3~5 倍
        对： p[t] = base   * (1 + ε)     永远围绕 base

    ★ 关键在于【用什么指标判别】。

      直觉会去断言"末价/首价的均值 ≈ 1"——但这条对**两种实现都成立**：
      随机游走在对数空间近似对称，N 条路径的均值照样是 1，
      只是每条路径各走各的。所以均值查不出这个 bug。

      真正的判别式是**离散度**：正确实现下所有序列的中位数都紧贴 base；
      累乘实现下每条路径的中位数散得很开。
      实测正确实现的 max/min < 1.1，累乘实现会到几倍甚至几十倍。
    """
    base = 299.0
    medians = [
        float(np.median(gen_price_series(np.random.default_rng(100 + i), base, 90)))
        for i in range(80)
    ]

    # ① 促销占比不高，中位数应该贴着 base（略低一点点）
    for m in medians:
        assert 0.80 * base <= m <= 1.05 * base, f"序列中位数 {m:.1f} 偏离基准 {base} 太远"

    # ② ★ 真正的漂移判别式：序列之间的离散度
    spread = max(medians) / min(medians)
    assert spread < 1.30, (
        f"80 条序列的中位数最大/最小 = {spread:.2f}，散得太开。"
        "典型病因是写成了 p[t] = p[t-1] * (1 + ε)（累乘漂移）。"
    )


def test_promotions_actually_happen() -> None:
    """促销必须真的发生，且降幅合理。

    没有促销的话，"最低价/基准价"恒等于 1，价格曲线是一条平线，
    前端的"价格波动"和 GLM 的"促销分析"全部落空。
    """
    rng = np.random.default_rng(8)
    ratios = []
    for _ in range(60):
        series = gen_price_series(rng, 299.0, 90)
        ratios.append(min(series) / float(np.median(series)))

    med = float(np.median(ratios))
    # 实测中位数 0.649。低于 0.5 说明折得太狠，高于 0.9 说明几乎没促销。
    assert 0.45 <= med <= 0.90, f"最低价/基准价 中位数 {med:.3f} 不合理"


def test_big_promo_days_create_a_detectable_trough() -> None:
    """大促日必须是可被检出的——这是给阶段③ 的 GLM 留的"考卷"。

    ★ 设计意图：如果 GLM 分析能在数据里发现"第 30 天和第 75 天
      有全品类价格低谷"，那说明它真的在读数据，而不是在编。

    做法：同一天的多个商品取平均后比较，单商品噪声会互相抵消。
    """
    n = 200
    base = 299.0
    day_means = np.zeros(90)
    for i in range(n):
        series = gen_price_series(np.random.default_rng(5000 + i), base, 90)
        day_means += np.array(series)
    day_means /= n

    big = float(np.mean([day_means[d] for d in BIG_PROMO_DAYS]))
    normal = float(np.median(day_means))
    assert big < normal * 0.85, (
        f"大促日均值 {big:.1f} 相对平日中位数 {normal:.1f} 降幅不够，"
        "GLM 分析时无法与随机波动区分开"
    )


def test_every_single_product_has_the_big_promo() -> None:
    """★★ 大促日必须【每个商品】都有，不能只是"平均下来看得见"。

    ★ 这条是上一版测试的补强，起因是看图发现的真 bug：

      上一版只断言了"200 个商品的日均值在大促日更低"。它**通过了**，
      但价格曲线图暴露了问题——有的商品第 30 天根本没有低谷。

      根因：大促判定写在了 `if promo_left == 0:` 的**内部**。
      如果第 30 天正好落在一次普通促销的中间（概率约 17%），
      `promo_left != 0`，大促分支**整个被跳过**，那一天就是普通促销价。
      两个大促日合计，约 30% 的商品缺至少一个。

    ★ 为什么"平均看得见"不够：
      设计意图写的是"全品类价格低谷"。如果一个商品有、另一个没有，
      那么逐商品分析（GLM 按商品给结论）会得到互相矛盾的答案，
      而"全品类"这个说法就是假的。
      取平均会把这个缺陷抹平——这正是聚合指标掩盖个体差异的典型场景。

    ★ 判别式：逐个商品检查它在大促日的价格是否显著低于自己的基准。
      用商品自己的中位数当基准，就不用假设 base 的绝对值。
    """
    n = 120
    misses: list[tuple[int, int, float]] = []

    for i in range(n):
        series = gen_price_series(np.random.default_rng(7000 + i), 299.0, 90)
        own_base = float(np.median(series))
        for day in BIG_PROMO_DAYS:
            # 大促日必须至少比自己的基准低 15%
            if not series[day] < own_base * 0.85:
                misses.append((i, day, series[day] / own_base))

    assert not misses, (
        f"{len(misses)}/{n * len(BIG_PROMO_DAYS)} 个「商品×大促日」没有出现价格低谷，"
        f"前几个：{misses[:5]}。"
        "典型病因是大促判定被写在 `if promo_left == 0:` 内部，"
        "撞上普通促销时被跳过。"
    )


# ===========================================================================
# 三、销量分布（幂律）
# ===========================================================================


def test_sales_are_all_positive() -> None:
    """销量必须 ≥ 1。

    ★ 0 和 NULL 语义不同（"卖光了" vs "不知道"），
      而且前端算"Top20 销量占比"时 0 会导致除零。
    """
    sales = sample_ranked_sales(np.random.default_rng(9), 1000)
    assert sales.min() >= 1
    assert len(sales) == 1000


def test_power_law_exponent() -> None:
    """★ 幂律指数必须 ≈ 0.85 —— 这才是 α 这个参数要保证的东西。

    为什么不断言"Top20 严格递减"：那与"个体扰动（避免完美单调）"自相矛盾，
    见模块 docstring【修订 2】。

    判别方法：对 log(销量) 与 log(名次) 做最小二乘回归，
    斜率就是幂律指数。用较大的名次范围（1..200）让 ±8% 的个体扰动
    互相抵消——只回归前 20 名的话，扰动会让斜率有可见的抖动。
    """
    sales = sample_ranked_sales(np.random.default_rng(10), 1000)

    # sample_ranked_sales 返回的是【原名次顺序】，不是降序，
    # 所以要先排序才能拿到 第1名、第2名……
    top = np.sort(sales)[::-1][:200]
    ranks = np.arange(1, len(top) + 1)

    slope = float(np.polyfit(np.log(ranks), np.log(top), 1)[0])
    assert slope == pytest.approx(-SALES_ALPHA, abs=0.04), (
        f"实测幂律指数 {slope:.3f}，目标 -{SALES_ALPHA}"
    )


def test_sales_distribution_is_not_uniform() -> None:
    """必须是"少数爆款 + 大量长尾"，不是均匀分布。

    均匀分布也能通过"销量为正"的断言，但 Top20 图表会变成一排等高的柱子。
    """
    sales = np.sort(sample_ranked_sales(np.random.default_rng(11), 1000))[::-1]
    assert sales[0] / sales[19] > 5, "Top20 太平了，没有爆款感"
    assert sales[0] / sales[19] < 40, "Top20 太陡了，后十根会矮到看不见"


def test_sales_are_decoupled_from_rank_order() -> None:
    """知名度要真的随机分配——不能所有商品都是"越靠前越热"。

    ★ 这条防的是把随机性写错位置：如果直接在 sales 数值上加噪声，
      排序后仍然是完美单调的曲线，Top20 图会很假。
      正确做法是随机性放在【排名】这一步（见 sample_ranked_sales 的注释）。
    """
    sales = sample_ranked_sales(np.random.default_rng(12), 500)
    hot = int(np.argmax(sales))
    # 最热的商品不该总是第 0 个
    assert hot != 0, "最大销量的商品永远是第一个，说明排名没有随机化"


# ===========================================================================
# 四、商品文案
# ===========================================================================


def test_vary_specs_only_replaces_number_plus_unit() -> None:
    """规格替换只能动「数字 + 单位」，不能碰型号名里的数字。

    ★ 这是很容易写错的地方：'CP13'、'10号电能棒'、'USB4' 里的数字
      没有单位，改了就不是那个商品了；而 '10000毫安' 改了仍然合理，
      因为同一型号确实有不同容量的 SKU。
    """
    rng = np.random.default_rng(13)
    # 型号名的数字必须原样保留
    for raw in ("CP13 电能块 10000毫安", "10号电能棒 100W", "USB4 数据线 40Gbps"):
        for _ in range(20):
            out = vary_specs(raw, rng)
            head = raw.split()[0]
            assert head in out, f"型号 {head} 被改坏了：{out}"


def test_vary_specs_expands_title_space() -> None:
    """规格替换必须真的扩大标题空间——这是它存在的唯一理由。

    ★ 只有约 45 条标题模板，生成 1000 个商品时会有近一半重复标题，
      Top20 图表上连着出现三个同名商品，看起来像程序写错了。
    """
    rng = np.random.default_rng(14)
    raw = "安克 充电宝 10000毫安 22.5W快充"
    variants = {vary_specs(raw, rng) for _ in range(200)}
    assert len(variants) > 20, f"200 次只产出 {len(variants)} 种变体，标题空间没打开"


def test_titles_are_unique_at_full_scale() -> None:
    """★★ 回归测试：1000 个商品的标题必须**互不重复**。

    ★ 这个 bug 的症状和原因相隔了三个阶段，值得完整记下来：

      症状：Mock 源采了 100 条爆料，结果只建出 **64 个商品**。
            报告写"新商品 64"，而库里的 Mock 商品本来是 100 个。

      原因：`build_products` 抽模板、抽片段、抽颜色时是**各自独立**抽的
            （有放回），42% 的商品拿到了重复标题。
            实测 count=100 → 87 个不同；count=1000 → 583 个不同（最多重复 19 次）。

      为什么三个阶段都没发现：**从来没有任何东西拿归并逻辑跑过 Mock 数据**。
            seed 直写只关心 (source, source_id) 唯一约束，标题重不重复无所谓；
            前端图表也不在乎。直到阶段 4 把采集管道接上 mock 源，
            归并逻辑第一次看见这批数据——它看到一个 source_id 已有主、
            但标题和别的商品一模一样，于是判定"这是重复爆料"，静默地少建了 36 个。
            ★ 归并**少建商品**不报错，这正是它藏了这么久的原因。

      修法：`_pick_title` 维护一个 seen_titles 集合，重抽直到标题唯一；
            抽不出来（池子真的用尽）就抛带池子大小的 RuntimeError，
            而不是悄悄返回一个重复的。

    ★ 断言取 1000 而不是 100：100 那次只重了 13%，1000 那次重了 42%，
      取满量程才能守住真实场景。实测标题池约 4039，撑 1000 有余量。
    """
    rng = np.random.default_rng(DEFAULT_SEED)
    products = build_products(rng, DEFAULT_PRODUCT_COUNT, DEFAULT_DAYS)

    titles = [p["title"] for p in products]
    assert len(titles) == DEFAULT_PRODUCT_COUNT
    assert len(set(titles)) == DEFAULT_PRODUCT_COUNT, (
        f"{len(titles) - len(set(titles))} 个标题重复了——"
        "采集管道会把它们当成同一条爆料归并掉，商品数凭空少一截"
    )

    # ★ title_norm 也必须唯一。归并比对看的是归一化后的形式，
    #   两个标题只要归一化后相同，就一定会被判定成重复——
    #   哪怕原始标题逐字不同（比如只差一个被当成噪声词去掉的"包邮"）。
    norms = [p["title_norm"] for p in products]
    assert len(set(norms)) == DEFAULT_PRODUCT_COUNT, (
        "原始标题唯一但归一化后重复——归并逻辑用的正是归一化形式"
    )


def test_title_pool_exhaustion_raises_instead_of_repeating() -> None:
    """★ 池子抽干时必须报错，不能"安静地返回一个重复标题"。

    这条守的是上面那条测试的**实现方式**：如果 _pick_title 在重抽
    _MAX_TITLE_RETRIES 次之后认命返回重复值，上面那条会红，
    但错误信息只说"标题重复了"，看不出根因是"池子不够大"。

    ★ 抛错信息里带池子大小，是因为这个错真的会犯：
      加了几条标题模板就以为池子够大了，实际要 count=10000 时才炸。
      看到"池子 4039，要抽 10000 个"就能立刻明白该干什么。
    """
    from scraper.seed import _MAX_TITLE_RETRIES  # noqa: F401  （存在性断言）

    rng = np.random.default_rng(DEFAULT_SEED)
    with pytest.raises(RuntimeError, match="标题"):
        build_products(rng, 100_000, 1)


def test_titles_never_have_two_colors() -> None:
    """★ 回归测试：标题里不能出现两个颜色。

    复用模板时踩到的真实 bug：模板里已经写了"冰莓紫"，
    代码又追加了一个颜色后缀，拼出
    「华为 FreeClip 2 耳夹式蓝牙耳机 冰莓紫 星光金」——
    一眼就能看出数据是拼出来的。
    """
    from scraper.seed import COLOR_SUFFIXES, KNOWN_COLORS

    colors = tuple(c.strip() for c in COLOR_SUFFIXES if c.strip())
    assert set(colors) <= set(KNOWN_COLORS), (
        "COLOR_SUFFIXES 里有 KNOWN_COLORS 不认识的色名，去重逻辑会漏掉它"
    )

    # 直接验证拼接规则：模板已含颜色时不追加
    rng = np.random.default_rng(15)
    for _ in range(300):
        title = "华为 FreeClip 2 耳夹式蓝牙耳机 冰莓紫"
        if not any(c in title for c in KNOWN_COLORS):
            title += COLOR_SUFFIXES[int(rng.integers(0, len(COLOR_SUFFIXES)))]
        hit = [c for c in KNOWN_COLORS if c in title]
        assert len(hit) == 1, f"标题含多个颜色 {hit}：{title}"


# ===========================================================================
# 五、入库正确性（需要数据库夹具）
# ===========================================================================


def test_seed_writes_expected_row_counts(db) -> None:
    stats = seed_mod.seed(db, count=T_COUNT, days=T_DAYS)

    assert stats["products"] == T_COUNT
    assert stats["price_history"] == T_COUNT * T_DAYS

    assert db.execute(select(func.count()).select_from(Product)).scalar_one() == T_COUNT
    assert (
        db.execute(select(func.count()).select_from(PriceHistory)).scalar_one()
        == T_COUNT * T_DAYS
    )


def test_price_column_equals_last_point_of_series(db) -> None:
    """products.price 必须是价格序列的【最后一天】，不是基准价。

    ★ 否则用户会发现"列表页写着 199，点进详情曲线末端是 249"。
      这个不一致很难在图表上直接看出来，但一旦发现就很掉价。
    """
    seed_mod.seed(db, count=T_COUNT, days=T_DAYS)

    for product in db.execute(select(Product).where(Product.source == "mock")).scalars():
        last = db.execute(
            select(PriceHistory.price)
            .where(PriceHistory.product_id == product.id)
            .order_by(PriceHistory.recorded_at.desc())
            .limit(1)
        ).scalar_one()
        assert product.price == pytest.approx(last), f"商品 {product.id} 当前价与末点不一致"


def test_price_max_is_the_deal_range_not_a_historical_high(db) -> None:
    """★ price_max 的语义必须与真实采集路径一致：**单值成交时等于 price**。

    ★ 这条测试替换掉了之前的 test_price_max_matches_series_maximum，
      后者把 price_max 断言成"90 天序列最大值"——**那是错的语义**。

    为什么错：真实采集路径按 cleaner.parse_price() 的返回值填这个字段，
    含义是【当前这次爆料的优惠价区间上限】（"券后199-299" → 299）。
    而"90 天最高价"是另一个东西（历史统计量）。
    两种语义塞进同一个列，后果是 ?source=mock 和 ?source=smzdm 下
    price_max 含义不同，混在一起算"价格区间宽度"会得到无意义的数字，
    而且**不报错、没有日志**。这正是本项目的 source 字段要防的那类问题。

    历史最高价没丢：从 price_history 现算（下面第二段就演示了怎么算）。

    ★ 判别式：把 seed 里的 price_max 改回 max(series)，这条测试会失败。
      为了让断言不空洞，先确认这些商品的价格序列**确实有波动**
      （否则 max(series) == series[-1]，两种实现无法区分）。
    """
    seed_mod.seed(db, count=T_COUNT, days=T_DAYS)

    checked = 0
    for product in db.execute(select(Product).where(Product.source == "mock")).scalars():
        top, bottom = db.execute(
            select(func.max(PriceHistory.price), func.min(PriceHistory.price))
            .where(PriceHistory.product_id == product.id)
        ).one()

        # ★ 只挑"序列确实波动过"的商品来断言——
        #   对一条平线来说 max(series) == series[-1]，
        #   两种实现给出同样的值，断言就没有判别力了。
        if top == bottom:
            continue
        checked += 1

        assert product.price_max == pytest.approx(product.price), (
            f"商品 {product.id} 的 price_max={product.price_max} != price={product.price}。"
            "price_max 被填成了历史最高价——那会让 mock 和 smzdm 的同名列语义不同。"
            "历史最高价应当从 price_history 现算（本测试上面就在算）。"
        )

    assert checked > T_COUNT // 2, (
        f"只有 {checked}/{T_COUNT} 个商品的价格序列有波动，"
        "断言覆盖不足——检查 DAILY_SIGMA / PROMO_PROB 是不是被调成了 0"
    )


def test_historical_high_is_available_from_price_history(db) -> None:
    """★ price_max 不再承载"历史最高价"之后，那个数必须仍然拿得到。

    这条不测实现，而是把"替代方案可用"这件事**演示成可执行的断言**：
    从 price_history 聚合出的 MAX/MIN 就是历史最高/最低价，
    而且它对 mock 和 smzdm 两个数据源语义一致（都是"见过的价格的极值"）。

    ★ 为什么值得单独写一条：改 price_max 语义时最容易的失误是
      "把列改对了，但没人记得历史最高价要从哪儿拿"，
      然后在阶段② 做"较最高价降幅"这个卡片时才发现数据没了。
    """
    seed_mod.seed(db, count=T_COUNT, days=T_DAYS)

    rows = db.execute(
        select(
            Product.id,
            Product.price,
            func.max(PriceHistory.price),
            func.min(PriceHistory.price),
        )
        .join(PriceHistory, PriceHistory.product_id == Product.id)
        .where(Product.source == "mock")
        .group_by(Product.id)
    ).all()

    assert rows
    for pid, current, high, low in rows:
        assert low <= current <= high, f"商品 {pid} 的当前价 {current} 不在历史区间 [{low}, {high}] 内"
        # 至少要有商品真的降过价，否则"降幅"这个卖点没有数据支撑
    assert any(current < high for _, current, high, _ in rows), (
        "所有商品的当前价都等于历史最高价——没有任何促销，降幅类图表会全空"
    )


def test_title_norm_is_produced_by_the_shared_normalizer(db) -> None:
    """title_norm 必须由 dedup.normalize_title 生成，不能另写一套。

    ★ 两套归一化逻辑会悄悄分叉：入库时用 A，比对时用 B，
      结果就是"看着明明一样的两条数据永远归并不上"。
    """
    seed_mod.seed(db, count=T_COUNT, days=T_DAYS)

    products = list(db.execute(select(Product).where(Product.source == "mock")).scalars())
    assert products
    for product in products:
        assert product.title_norm == normalize_title(product.title)


def test_mock_products_carry_bilingual_brand(db) -> None:
    """品牌必须是 "Anker/安克" 这种双语格式。

    ★ 为什么 Mock 也要保持这个格式：源站（SMZDM）给的就是双语，
      硬门槛依赖品牌归一化，两边的输入形态不一致的话，
      测试覆盖不到真实数据的品牌处理路径。
    """
    seed_mod.seed(db, count=T_COUNT, days=T_DAYS)

    brands = [
        p.brand
        for p in db.execute(select(Product).where(Product.source == "mock")).scalars()
    ]
    for brand in brands:
        assert brand and brand.count("/") == 1, f"品牌格式不对：{brand}"


def test_mock_products_are_linked_to_shops(db) -> None:
    """每个 Mock 商品都要挂到店铺上——"店铺占比"饼图依赖它。"""
    seed_mod.seed(db, count=T_COUNT, days=T_DAYS)

    orphan = db.execute(
        select(func.count()).select_from(Product).where(Product.shop_id.is_(None))
    ).scalar_one()
    assert orphan == 0

    # 店铺必须真的存在于 shops 表（外键开着，这里再确认一次不是悬空 id）
    shop_ids = {s.id for s in db.execute(select(Shop)).scalars()}
    for product in db.execute(select(Product).where(Product.source == "mock")).scalars():
        assert product.shop_id in shop_ids


# ===========================================================================
# 六、幂等（一等公民的硬要求）
# ===========================================================================


def test_seed_is_idempotent(db) -> None:
    """连跑两次不报错，且结果一致。

    ★ 不幂等的话，调参阶段第二次跑就撞 (source, source_id) 唯一约束，
      而那个报错信息长得像代码 bug，不是"你没清库"——
      每次改参数都要排查一遍，非常浪费时间。
    """
    first = seed_mod.seed(db, count=T_COUNT, days=T_DAYS)
    assert first["products"] == T_COUNT

    second = seed_mod.seed(db, count=T_COUNT, days=T_DAYS)

    # 第二次应该先删掉第一次的全部数据，再灌同样多的
    assert second["removed_products"] == T_COUNT
    assert second["removed_price_history"] == T_COUNT * T_DAYS
    assert second["products"] == T_COUNT

    assert db.execute(select(func.count()).select_from(Product)).scalar_one() == T_COUNT
    assert (
        db.execute(select(func.count()).select_from(PriceHistory)).scalar_one()
        == T_COUNT * T_DAYS
    )


def test_seed_is_deterministic(db) -> None:
    """固定种子 → 完全相同的输出。

    ★ 这条是调参的前提：改了一行代码导致图表变了，
      和随机波动导致图表变了，必须能区分开。
      不加固定种子的话，这两件事外观完全一样。
    """
    seed_mod.seed(db, count=T_COUNT, days=T_DAYS)
    first = [
        (p.source_id, p.title, p.price, p.sales)
        for p in db.execute(select(Product).order_by(Product.source_id)).scalars()
    ]

    seed_mod.seed(db, count=T_COUNT, days=T_DAYS)
    second = [
        (p.source_id, p.title, p.price, p.sales)
        for p in db.execute(select(Product).order_by(Product.source_id)).scalars()
    ]

    assert first == second


def test_seed_does_not_touch_real_data(db) -> None:
    """★ 重新 seed 绝不能动真实采集的数据。

    这条是双数据源隔离设计的价值兑现点：
    "重新 seed"永远不需要重新抓数据（而 SMZDM 有 WAF，重抓的代价很高）。

    验证方式：先塞一条 source='smzdm' 的商品和它的价格历史，
    跑完 seed 之后确认它们原封不动。
    """
    shop = Shop(name="京东自营", platform="京东")
    db.add(shop)
    db.flush()

    real = Product(
        source="smzdm",
        source_channel="jingxuan",
        source_id="182950953",
        title="京觅 猕猴桃 24枚礼盒装",
        title_norm="京觅 猕猴桃 24枚 礼盒装",
        brand="京觅",
        platform="京东",
        price=17.5,
        price_max=17.5,
        shop_id=shop.id,
    )
    db.add(real)
    db.flush()
    db.add(
        PriceHistory(
            product_id=real.id,
            price=17.5,
            recorded_at=seed_mod.utcnow(),
            source_ref="182950953",
        )
    )
    db.commit()
    real_id = real.id

    seed_mod.seed(db, count=T_COUNT, days=T_DAYS)

    survivor = db.get(Product, real_id)
    assert survivor is not None, "seed 把真实数据删掉了"
    assert survivor.source_id == "182950953"
    assert survivor.price == pytest.approx(17.5)

    points = db.execute(
        select(func.count()).select_from(PriceHistory).where(PriceHistory.product_id == real_id)
    ).scalar_one()
    assert points == 1, "真实数据的价格历史被删了"

    # 它挂靠的店铺也不能被当成"孤儿店铺"清掉
    assert db.get(Shop, shop.id) is not None, "真实数据的店铺被当成孤儿清掉了"


def test_reset_removes_only_mock_products(db) -> None:
    """reset_mock_data 的删除范围必须精确到 source='mock'。"""
    seed_mod.seed(db, count=10, days=5)
    removed = seed_mod.reset_mock_data(db)

    assert removed["products"] == 10
    assert removed["price_history"] == 50
    assert db.execute(select(func.count()).select_from(Product)).scalar_one() == 0


def test_reset_deletes_alerts_before_their_rules(db) -> None:
    """★ 有触发记录的规则，必须先删 alerts 再删 alert_rules。

    `alerts.rule_id` 指向 `alert_rules.id`，而 PRAGMA foreign_keys=ON 是开着的
    （见 app/db/base.py），所以顺序反了会被外键挡住、抛 IntegrityError。

    ★ 这条**很容易踩到**：给任意一个 mock 商品建一条规则，等它触发一次
      （worker 每 5 分钟扫一遍），此后任何一次 `python -m scraper.seed`
      都会死在 reset_mock_data 的第一步上——整个 seed 直接不可用。
      而"给 mock 商品建条告警规则试试"恰恰是最自然的演示动作。

    ★ 项目里同一个顺序在 app/api/alerts.py 的 DELETE /rules/{id} 里是对的
      （先删 Alert 再删规则），seed 这边漏了——所以要有测试钉住，
      而不是靠"记得两处都改"。

    ★ 顺带守住隔离：真实数据（source='smzdm'）的规则和触发记录
      不能被这次清理碰到。
    """
    seed_mod.seed(db, count=2, days=1)
    mock_product = db.execute(select(Product).order_by(Product.id)).scalars().first()

    # 真实采集的商品 + 它的规则和触发记录（这两个必须活下来）
    real = Product(source="smzdm", source_id="real-1", title="真实采集商品", price=50.0)
    db.add(real)
    db.commit()

    mock_rule = AlertRule(
        product_id=mock_product.id, rule_type="price_below", threshold=200.0, enabled=True
    )
    real_rule = AlertRule(
        product_id=real.id, rule_type="price_below", threshold=60.0, enabled=True
    )
    db.add_all([mock_rule, real_rule])
    db.commit()
    db.add_all([
        Alert(rule_id=mock_rule.id, price_at_trigger=150.0, notified=False),
        Alert(rule_id=real_rule.id, price_at_trigger=55.0, notified=False),
    ])
    db.commit()

    removed = seed_mod.reset_mock_data(db)  # ★ 修复前这里抛 IntegrityError

    assert removed["alerts"] == 1, "mock 规则下的触发记录没被删（或删多了）"
    assert removed["alert_rules"] == 1

    # 隔离：真实数据的规则和记录都还在
    remaining_rules = db.execute(select(AlertRule)).scalars().all()
    assert [r.id for r in remaining_rules] == [real_rule.id], "真实数据的规则被删了"
    remaining_alerts = db.execute(select(Alert)).scalars().all()
    assert [a.rule_id for a in remaining_alerts] == [real_rule.id], "真实数据的触发记录被删了"


def test_seed_cleans_up_orphan_shops(db) -> None:
    """孤儿店铺要被清掉，但有商品挂靠的店铺必须留下。

    ★ 无条件删光 shops 会连真实数据的店铺一起删掉（外键会挡住，报错很突兀）。
      正确做法是只删"现在没有任何商品引用"的那些。
    """
    db.add(Shop(name="一家没人挂靠的店", platform="京东"))
    db.commit()

    seed_mod.seed(db, count=T_COUNT, days=T_DAYS)

    names = {s.name for s in db.execute(select(Shop)).scalars()}
    assert "一家没人挂靠的店" not in names
    assert names, "有商品挂靠的店铺不能被删光"


# ===========================================================================
# 七、价格历史表的唯一约束交互（Mock 是 NULL source_ref 的极端场景）
# ===========================================================================


def test_mock_history_rows_all_have_null_source_ref(db) -> None:
    """★ Mock 的 source_ref 必须全是 NULL，靠 SQLite "多个 NULL 互不相同"
      的行为绕开 (product_id, source_ref) 唯一约束。

    ★ 这条是价格历史能存 90 个点的**前提条件**。
      如果哪天给 Mock 也填上 source_ref（比如图省事填 product_id），
      第 2 天的数据就会被唯一约束挡掉，
      而且 ON CONFLICT DO NOTHING 是**静默**的——不报错，只是数据变少，
      最后表现为"折线图只画了一个点"。这个 bug 极难排查。
    """
    seed_mod.seed(db, count=10, days=30)

    not_null = db.execute(
        select(func.count()).select_from(PriceHistory).where(PriceHistory.source_ref.is_not(None))
    ).scalar_one()
    assert not_null == 0, "Mock 的价格历史填了 source_ref，唯一约束会吃掉后续所有数据点"

    per_product = db.execute(
        select(PriceHistory.product_id, func.count())
        .group_by(PriceHistory.product_id)
    ).all()
    assert per_product, "没有任何价格历史"
    for pid, cnt in per_product:
        assert cnt == 30, f"商品 {pid} 只存了 {cnt} 个价格点，期望 30"


def test_default_params_are_the_calibrated_ones() -> None:
    """★ 把标定结果钉死，防止被无意改掉。

    这几个数字是整套分布参数的入口。它们变了，之前所有的图表
    和 docs/charts/ 里的对照图就都对不上了——而那种不一致
    通常要到演示前一天才会被发现。
    """
    assert DEFAULT_DAYS == 90
    assert SALES_ALPHA == pytest.approx(0.85)
    assert PROMO_PROB == pytest.approx(0.05)
    assert BIG_PROMO_DAYS == (30, 75)
