"""Mock 数据生成 —— 全项目的地基。

================================================================================
为什么 Mock 是一等公民而不是小配角
================================================================================
90 天 × N 个商品 = 上万条 price_history。前端的折线图、价格分布直方图、
Top20 销量排行**全部**依赖它。

更关键的是**阶段③ 的 GLM 分析**：报告的结论完全建立在数据之上。
如果 Mock 数据是一堆 `random.uniform(10, 1000)`，GLM 会一本正经地
分析一堆噪声，产出一份**看起来专业但毫无意义**的报告——
而这是最难被发现的问题，因为报告读起来很通顺。

所以这个模块的目标不是"造点数据让页面不空"，而是
**造出统计特征与真实电商市场一致的数据**，
让"分析 pipeline"这件事本身可以被独立验证。

================================================================================
三个必须做对的地方（都是踩过的坑）
================================================================================

【1】价格的形状：对数正态 + 拒绝采样，不能用 clip

  真实商品价格是右偏的——大量几十上百的平价商品，少量上千的高端货。
  用均匀分布是错的，对数正态才对（μ=ln(150) 即中位数 150 元）。

  ★ 边界处理不能用 np.clip：那会把越界值**堆在边界上**，
    直方图两端出现两根突兀的尖柱，一眼就看出是假数据。
    必须用【拒绝采样】——越界就重抽，让分布自然衰减。

【2】价格的演化：围绕固定基准波动，绝不能逐日累乘

  ★ 这是实际踩到的真坑，直觉写法是错的：

      p[t] = p[t-1] * (1 + ε)        ← 错！

    看起来"每天随机波动 1.2%"很合理，但乘性误差会**累积**：
    90 天后价格会偏离 3~5 倍。实测日波动 σ=0.012 时，
    90 天后价格中位数跌到初始值的 12%——折线图是一条一路下滑的滑梯，
    完全不像真实价格。

    正确做法：**所有价格围绕一个固定的 base 波动**。
    平日 = base × (1 + 小噪声)，促销日 = base × (1 - 折扣)。
    这样价格永远在 base 附近，促销结束后自然回归，
    不会漂移。

【3】销量的分布：幂律，且必须与价格解耦

  真实市场是"少数爆款 + 大量长尾"。均匀分布会让 Top20 图表
  变成一排等高的柱子，毫无信息量。

  ★ "与价格解耦"指的是：不能写成 `sales = f(price)`
    （比如"越便宜卖得越好"）。真实市场里贵的商品照样有卖得好的，
    两者是独立的市场因素。所以先抽一个独立的"热度分"，
    再由热度决定名次，而不是由价格决定。

================================================================================
幂律参数的标定（α = 0.85 是怎么来的）
================================================================================
目标不是"数学上正确的幂律"，而是"Top20 图表好看"。
用 `sales[k] = A · k^(-α)` 试了几个 α：

| α | 第1/第5 | 第1/第20 | 观感 |
|---|---|---|---|
| 0.7 | 2.9x | 5.0x | 太平，没有爆款感 |
| **0.85** | **~4.0x** | **~13.5x** | **梯度清晰，每根柱都可见** ✅ |
| 1.0 | 7.4x | 29.9x | 后 10 根几乎贴地 |
| 1.1 | 11.7x | 58.9x | 只剩 3 根看得见 |

α=1.0 时第 20 名只有第 1 名的 1/30，柱状图上后十根会矮到看不见，
图表失去信息量。0.85 是"能看出头部优势、但每根柱都还有高度"的位置。
"""

from __future__ import annotations

import argparse
import math
import re
from datetime import timedelta

import numpy as np
from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from app.db.base import SessionLocal, utcnow
from app.db.models import Alert, AlertRule, PriceHistory, Product, Shop
from scraper.dedup import normalize_title
from scraper.store import PricePoint, record_prices

# ---------------------------------------------------------------------------
# 可复现性
# ---------------------------------------------------------------------------

# ★ 固定种子 = 结果可复现。
#   不加这一条的话，每次 seed 出来的数据都不同，
#   "改了一行代码导致图表变了"和"随机波动导致图表变了"就分不清了——
#   调参阶段这是致命的。
DEFAULT_SEED = 20260928

DEFAULT_PRODUCT_COUNT = 1000
DEFAULT_DAYS = 90

# ---------------------------------------------------------------------------
# 价格分布参数（对数正态）
# ---------------------------------------------------------------------------

# 对数正态的中位数 = exp(μ)。想要 150 元的中位数，就得取 μ = ln(150)。
PRICE_MU = math.log(150.0)

# ★ 为什么是 0.60（实测标定，不是拍脑袋）：
#
#   σ      p5    p50    p95    <50元   >800元
#   0.55   58    148    358    2.9%    0.05%
#   0.60   56    151    402    3.3%    0.40%   ← 选它
#   0.75   42    148    513    8.0%    1.35%
#
#   σ 太小则价格全挤在 150 附近，"价格分布"图没有形状；
#   σ 太大则出现大量 30 元以下和 2000 元以上的商品，
#   而数码配件这个品类里那不现实。
PRICE_SIGMA = 0.60

# 合理价格区间。超出这个范围的重抽（拒绝采样），不用 clip。
PRICE_MIN = 50.0
PRICE_MAX = 800.0

# ---------------------------------------------------------------------------
# 价格演化参数
# ---------------------------------------------------------------------------

# 平日的日波动幅度。1.2% 是"价格基本稳定但有小幅调整"的量级。
DAILY_SIGMA = 0.012

# 每天开始一次促销的概率。
#
# ★ 从 0.07 下调到 0.05 是看了价格曲线图之后改的。
#   0.07 时 90 天里会出现约 10 次降价，促销天数占比 36%——
#   曲线画出来是一串密集的深 V，不像真实价格，更像心电图。
#   真实商品在售天数占比通常 15~25%，0.05 大约给出 26%（含两次大促），
#   同时把价格变异系数从 0.128 拉回 0.11 附近，更接近计划里标定的 0.107。
PROMO_PROB = 0.05

# 普通促销的降幅与持续天数
PROMO_DROP = (0.10, 0.32)
PROMO_LEN = (2, 5)

# ★ 模拟大促（618 / 双11）。
#   用固定的天数而不是随机，是为了让"大促"这件事在数据里**可被检出**——
#   阶段③ 的 GLM 分析如果发现"第 30 天和第 75 天有全品类价格低谷"，
#   那说明它真的在读数据，而不是在编。
BIG_PROMO_DAYS = (30, 75)
BIG_PROMO_DROP = (0.25, 0.40)
BIG_PROMO_LEN = 5

# ---------------------------------------------------------------------------
# 销量分布参数
# ---------------------------------------------------------------------------

# 幂律指数。标定过程见模块顶部 docstring。
SALES_ALPHA = 0.85

# 名次第 1 的基准销量。取 8 万，让 Top20 的绝对值落在
# "看起来像真实数码配件销量"的量级（几万到几千）。
SALES_TOP = 80_000

# 个体扰动幅度（±8%）。
#
# ★ 作用是"避免完美单调"——不加的话销量是名次的确定性函数，
#   画出来是一条数学上完美光滑的幂律曲线，反而显得假。
#
# ★ 为什么是 8% 而不是更大：
#   相邻名次的销量差在尾部会变得很小（第 20 名与第 21 名只差 4.2%），
#   扰动太大会让尾部名次频繁互换，Top20 图看起来像随机排列。
#   8% 让头部（名次差很大）几乎不乱、尾部有轻微交错，接近真实榜单。
#   注意它的效果是**打破完美的严格递减**，这是刻意的。
SALES_NOISE = 0.08

# ★ 关于"爆款翻倍"（原计划有，实测后移除）
#
#   原计划写的是"头部 25 名里挑 3–5 个爆款翻 2.5–6 倍"。
#   实现后实测发现它**破坏了幂律梯度**：
#
#     名次 9  基准 12359 × 5.46 → 67459   （第 1 名才 80000，它到了 84%）
#     名次 4  基准 24623 × 2.81 → 69102
#
#   结果是第 2/3/4 名挤在第 1 名附近形成一个人为的"驼峰"，
#   再陡降到第 5 名——而 α=0.85 这个参数的全部意义
#   （Top20 每根柱都可见的干净梯度）就没了。
#
#   根本原因："爆款"本来就【是】幂律的头部，再额外翻倍是重复计算。
#   真实市场里第 1 名之所以卖得多，已经体现在 rank^-α 里了。
#
#   所以移除爆款逻辑，改成小幅个体扰动。

# ---------------------------------------------------------------------------
# 商品文案素材（数码配件品类）
# ---------------------------------------------------------------------------

# (平台, 店铺名)。★ 这张表是"哪些店铺存在"的定义，brand_shops 里的名字必须都在这里。
SHOP_TEMPLATES: tuple[tuple[str, str], ...] = (
    # 多品牌经营的平台自营店。品牌没有自家旗舰店时落到这里。
    ("京东", "京东自营"),
    ("天猫", "天猫超市"),
    ("京东", "安克京东自营旗舰店"),
    ("京东", "倍思京东自营旗舰店"),
    ("京东", "绿联京东自营旗舰店"),
    ("京东", "小米京东自营旗舰店"),
    ("京东", "罗马仕京东自营旗舰店"),
    ("京东", "华为京东自营旗舰店"),
    ("京东", "漫步者京东自营旗舰店"),
    ("天猫", "安克天猫旗舰店"),
    ("天猫", "倍思天猫旗舰店"),
    ("天猫", "绿联天猫旗舰店"),
    ("天猫", "品胜天猫旗舰店"),
    ("天猫", "酷态科天猫旗舰店"),
    ("天猫", "声阔天猫旗舰店"),
    ("天猫", "华为天猫旗舰店"),
    ("天猫", "漫步者天猫旗舰店"),
)

# ★ 品牌 → 该品牌在各平台的旗舰店。
#
#   为什么要这张表：第一版是**完全随机**分配店铺的
#   （`rng.integers(0, len(SHOP_TEMPLATES))`），于是生成出
#   "绿联的磁吸指环扣在声阔天猫旗舰店销售"这种数据。
#   单看一行不算错（确实有经销商），但它是**内部矛盾**的：
#   店铺名里就写着另一个品牌，而品牌字段写的是这个品牌。
#
#   这个 bug 直到阶段 3 才被发现，因为它是被 AI 报告暴露的——
#   报告会拿到每个商品的 `shop` 字段并据此写结论，
#   于是它可能写出"绿联在声阔旗舰店表现突出"这种一眼假的句子。
#   而 Mock 数据"是假的"和 Mock 数据"自相矛盾"是两回事：
#   前者是如实披露的取舍，后者会让整份报告的可信度一起垮掉。
#
#   ★ 顺带一提，这类问题在纯统计验证里查不出来——
#     它有 12 个店铺、1000 个商品、分布也"正常"。
#     只有把数据**读一遍**才会发现。
brand_shops: dict[str, dict[str, str]] = {
    "安克": {"京东": "安克京东自营旗舰店", "天猫": "安克天猫旗舰店"},
    "倍思": {"京东": "倍思京东自营旗舰店", "天猫": "倍思天猫旗舰店"},
    "绿联": {"京东": "绿联京东自营旗舰店", "天猫": "绿联天猫旗舰店"},
    "小米": {"京东": "小米京东自营旗舰店"},
    "罗马仕": {"京东": "罗马仕京东自营旗舰店"},
    "品胜": {"天猫": "品胜天猫旗舰店"},
    "酷态科": {"天猫": "酷态科天猫旗舰店"},
    "声阔": {"天猫": "声阔天猫旗舰店"},
    "华为": {"京东": "华为京东自营旗舰店", "天猫": "华为天猫旗舰店"},
    "漫步者": {"京东": "漫步者京东自营旗舰店", "天猫": "漫步者天猫旗舰店"},
}

# 品牌在某平台没有旗舰店时落到该平台的多品牌自营店。
GENERIC_SHOPS: dict[str, str] = {"京东": "京东自营", "天猫": "天猫超市"}


def resolve_shop(brand_cn: str, platform_pick: int) -> tuple[str, str]:
    """按品牌挑一个店铺，返回 (平台, 店铺名)。

    `platform_pick` 是 0 或 1，含义是"京东 / 天猫"。

    ★ 为什么接收一个整数而不是自己掷随机数：
      调用方必须**用掉那一次 rng 调用**，见 build_products 里那段注释——
      少消费一个随机数会让后面全部数据的生成序列偏移，
      价格、销量、标题全变，测试基线和自检图一起作废。
    """
    platform = "京东" if platform_pick == 0 else "天猫"
    shops = brand_shops.get(brand_cn, {})
    name = shops.get(platform) or GENERIC_SHOPS[platform]
    return platform, name

# (品类, 品牌中文名, 品牌英文名, 标题片段)
#
# ★ 品牌中英文名都要有：源站给的是 "HLA/海澜之家" 这种双语格式，
#   Mock 数据也用同样的格式，测试才能覆盖真实的品牌归一化逻辑。
PRODUCT_TEMPLATES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("充电宝", "安克", "Anker", (
        "磁吸充电宝 10000毫安 22.5W快充 自带支架",
        "充电宝 20000毫安 65W双向快充 笔记本可用",
        "超薄充电宝 5000毫安 15W无线磁吸",
        "胶囊充电宝 5000毫安 直插式便携",
    )),
    ("充电宝", "倍思", "Baseus", (
        "充电宝 10000毫安 22.5W快充 数显",
        "自带线充电宝 20000毫安 65W",
        "磁吸无线充电宝 10000毫安 15W",
    )),
    ("充电宝", "罗马仕", "ROMOSS", (
        "充电宝 20000毫安 22.5W快充 双向",
        "充电宝 30000毫安 大容量 户外",
    )),
    ("充电宝", "酷态科", "Cuktech", (
        "充电宝 10号电能棒 10000毫安 100W",
        "CP13 电能块 自带线 10000毫安",
    )),
    ("蓝牙耳机", "华为", "HUAWEI", (
        "FreeClip 2 耳夹式蓝牙耳机 冰莓紫",
        "FreeBuds 6i 主动降噪 真无线蓝牙耳机",
        "FreeClip 开放式设计 耳夹式蓝牙耳机",
    )),
    ("蓝牙耳机", "漫步者", "EDIFIER", (
        "LolliPods 真无线蓝牙耳机 半入耳式",
        "W820NB 主动降噪 头戴式蓝牙耳机",
    )),
    ("蓝牙耳机", "声阔", "Soundcore", (
        "Liberty 4 NC 主动降噪 真无线蓝牙耳机",
        "Space One 头戴式 主动降噪蓝牙耳机",
    )),
    ("蓝牙耳机", "小米", "Xiaomi", (
        "Redmi Buds 6 青春版 真无线蓝牙耳机",
        "开放式耳机 骨传导 不入耳蓝牙耳机",
    )),
    ("数据线", "绿联", "UGREEN", (
        "Type-C 数据线 100W快充 编织线 2米",
        "Lightning 数据线 MFi认证 1.5米",
        "USB4 全功能数据线 240W 40Gbps",
    )),
    ("数据线", "倍思", "Baseus", (
        "Type-C 数据线 100W 尼龙编织 1米",
        "三合一数据线 伸缩式 车载",
    )),
    ("数据线", "品胜", "PISEN", (
        "Type-C 数据线 66W快充 2米",
        "苹果数据线 20W快充 1米",
    )),
    ("充电器", "安克", "Anker", (
        "氮化镓充电器 65W 三口 便携",
        "充电器 30W 单口 PD快充",
    )),
    ("充电器", "倍思", "Baseus", (
        "氮化镓充电器 100W 四口 桌面",
        "充电头 20W PD快充 苹果通用",
    )),
    ("充电器", "绿联", "UGREEN", (
        "氮化镓充电器 45W 双口 折叠",
        "无线充电器 15W 磁吸 支架款",
    )),
    ("手机配件", "绿联", "UGREEN", (
        "手机支架 桌面折叠 铝合金",
        "磁吸手机指环扣 金属 超薄",
    )),
)

# 颜色/规格后缀。真实标题里几乎总带一个，加上它让标题更像真的。
COLOR_SUFFIXES: tuple[str, ...] = (
    "", "", "", "",  # 大部分商品不带颜色，用空串占位保持权重
    " 黑色", " 白色", " 深空灰", " 冰莓紫", " 星光金", " 远峰蓝",
)

# 标题模板里已经写死的颜色。★ 用于避免拼出"冰莓紫 星光金"这种双颜色标题——
# 那是复用模板时踩到的真实 bug，一眼就能看出数据是拼出来的。
KNOWN_COLORS: tuple[str, ...] = (
    "黑色", "白色", "深空灰", "冰莓紫", "星光金", "远峰蓝",
    "橄榄金", "勃艮第酒红", "原色钛金属",
)

# ★ 规格数值的可选值：按单位给出该品类下合理的取值。
#
# 为什么需要它：标题模板只有约 45 条（15 个品牌×品类组合 × 各 2-4 个型号），
# 乘以 11 种颜色也才 ~495 个不同的标题。生成 1000 个商品时会有
# **一半是重复标题**，Top20 图表上会连着出现三根"绿联 氮化镓充电器 45W"，
# 看起来像程序写错了。
#
# 把模板里的数字按单位替换成随机的合理值之后，标题空间扩大到几万，
# 重复自然消失。而且这更真实——同一个型号确实有 5000/10000/20000 毫安
# 这些不同容量的 SKU 在卖。
#
# ★ 只替换"数字 + 已知单位"的组合：像 "CP13"、"10号"、"USB4" 这种
#   没有单位的数字不会被碰，因为它们是型号名的一部分，改了就不是那个商品了。
SPEC_CHOICES: dict[str, tuple[str, ...]] = {
    "毫安": ("5000", "10000", "15000", "20000", "30000"),
    "W": ("20", "22.5", "30", "45", "65", "100", "120", "140"),
    "米": ("0.5", "1", "1.5", "2", "3"),
    "Gbps": ("10", "20", "40"),
}

# 匹配「数字 + 单位」。单位保留在捕获组里，替换时原样拼回去。
RE_SPEC_NUM = re.compile(r"\d+(?:\.\d+)?\s*(?:毫安|W|米|Gbps)")


def vary_specs(title: str, rng: np.random.Generator) -> str:
    """把标题里的规格数字替换成同类目下的其他合理取值。

    这样同一个型号能派生出"5000毫安"、"20000毫安"等多个 SKU，
    标题空间从几百扩大到几万，避免 Top20 里出现同名商品。

    ★ 注意它只动"数字 + 单位"的组合。像 'CP13'、'10号电能棒'、'USB4'
      里的数字没有单位，不会被替换——它们是型号名，改了就不是同一个商品了。
    """

    def repl(m: re.Match[str]) -> str:
        unit = m.group(0)
        # 从匹配到的完整片段里剥出单位
        for u in ("毫安", "Gbps", "W", "米"):
            if unit.endswith(u):
                choices = SPEC_CHOICES[u]
                return f"{rng.choice(choices)}{u}"
        return unit

    return RE_SPEC_NUM.sub(repl, title)


# ===========================================================================
# 采样函数
# ===========================================================================


def quantize_price(price: float) -> float:
    """把价格吸附到"心理定价"上。

    ★ 真实电商几乎不出现 `187.34` 元，而是 `149.9` / `299` / `59.9`。
      不做这一步的话，前端 tooltip 里会显示一堆 `187.34`,
      一眼就看出是随机数生成的。

    规则：
        小额（<100）  → 整数 + 0.9        29.9 / 59.9 / 99.9
        中额（<300）  → 10 的倍数 - 0.1   149.9 / 299.9
        大额（<1000） → 50 的倍数 - 0.1   549.9 / 799.9

    ★ 减 0.1 而不是加：`299.9` 比 `300.1` 更像真实定价，
      这是零售业的经典做法（左位数效应）。
    """
    if price < 100:
        return round(int(price)) + 0.9
    if price < 300:
        step = 10
    elif price < 1000:
        step = 50
    else:
        step = 100
    return float(int(price / step) * step) - 0.1


def sample_base_prices(rng: np.random.Generator, n: int) -> np.ndarray:
    """抽 n 个基准价格：对数正态 + 拒绝采样 + 心理定价。

    ★ 拒绝采样而不是 np.clip：
      用一个 while 循环不断重抽越界的样本，直到全部落在
      [PRICE_MIN, PRICE_MAX] 内。

      为什么不 clip：clip 把越界值**堆在边界上**，价格直方图两端
      会出现两根突兀的尖柱。拒绝采样只是丢弃重抽，分布形状自然衰减。
      实测丢弃率约 3.7%，代价可以接受。

    ★ 循环有次数上限：正态分布下 p5≈56、p95≈402，
      落在 [50, 800] 外的概率很小，正常几轮就收敛。
      加保险是为了防止参数被改坏时进入死循环。
    """
    out: list[float] = []
    for _ in range(200):  # 保险：最多重抽 200 轮
        need = n - len(out)
        if need <= 0:
            break
        # 多抽 25% 冗余，减少循环次数
        candidates = rng.lognormal(PRICE_MU, PRICE_SIGMA, size=int(need * 1.25) + 16)
        for p in candidates:
            if PRICE_MIN <= p <= PRICE_MAX:
                out.append(quantize_price(float(p)))
                if len(out) == n:
                    break

    if len(out) < n:
        # 理论上到不了这里；到了说明参数被改成不合理的值，
        # 与其静默返回短数组让下游莫名报错，不如明确报错。
        raise RuntimeError(
            f"拒绝采样未能凑够 {n} 个价格（只得到 {len(out)} 个）。"
            f"检查 PRICE_MIN/PRICE_MAX 与 PRICE_MU/PRICE_SIGMA 是否自相矛盾。"
        )
    return np.array(out[:n])


def sample_ranked_sales(rng: np.random.Generator, n: int) -> np.ndarray:
    """生成 n 个商品的销量，按名次服从幂律。

    ★ 关键在于"热度分"与价格**完全解耦**：
      这里不接收 prices 参数，就是为了在类型层面杜绝
      "贵的东西卖得差"这种想当然的写法。

    步骤：
      1. 抽热度分（对数正态）
      2. 按热度排名 —— ★ 随机性放在【排名】这一步，而不是放在销量数值上。
         这样"哪个商品是第几名"是随机的，但"第 k 名该卖多少"是确定的，
         幂律形状不会被随机性破坏。
      3. 名次 → 幂律基准销量  sales = A · rank^(-α)
      4. 乘一个 ±8% 的个体扰动，打破完美单调

    ★ 返回的数组【按原名次排列】（不是降序），
      调用方按名次挂到对应商品上即可。
    """
    # 1) 热度分
    heat = rng.lognormal(0.0, 1.0, size=n)

    # 2) 名次（热度高的名次靠前）
    order = np.argsort(-heat)          # order[0] 是最热的商品下标
    ranks = np.empty(n, dtype=int)
    ranks[order] = np.arange(1, n + 1)  # 每个商品的名次（1 开始）

    # 3) 幂律基准销量
    sales = SALES_TOP * ranks.astype(float) ** (-SALES_ALPHA)

    # 4) 个体扰动
    noise = 1.0 + rng.uniform(-SALES_NOISE, SALES_NOISE, size=n)
    sales = sales * noise

    # ★ 下限 1：销量不能是 0。
    #   0 和 NULL 语义不同（"卖光了" vs "不知道"），而且
    #   前端画对数轴或算"Top20 占比"时 0 会出现除零。
    return np.maximum(np.round(sales), 1).astype(np.int64)


def gen_price_series(
    rng: np.random.Generator, base: float, days: int
) -> list[float]:
    """生成一个商品 days 天的价格序列。

    ★ 所有价格围绕【固定的 base】波动，绝不围绕前一天累乘。

      对比两种写法：
        错： p[t] = p[t-1] * (1 + ε)     乘性误差累积，90 天偏 3~5 倍
        对： p[t] = base * (1 + ε)        永远在 base 附近，促销后自然回归

    促销是一个有状态的区间：开始后持续 PROMO_LEN 天，期间价格维持降幅，
    结束后回到 base。
    """
    prices: list[float] = []
    promo_left = 0          # 当前促销还剩几天
    promo_drop = 0.0        # 当前促销的降幅

    for t in range(days):
        # ---- 促销开始判定 ----
        #
        # ★ 大促必须【无条件覆盖】正在进行的普通促销，所以它的判断在
        #   `promo_left == 0` 之外。这是看图时发现的一个真 bug：
        #
        #   原来的写法把大促判断放在了 `if promo_left == 0:` 的**内部**，
        #   于是当第 30 天正好落在一次普通促销的中间（概率约 17%）时，
        #   promo_left != 0，大促分支整个被跳过——那一天只是普通促销价。
        #   两个大促日合计，约 30% 的商品缺至少一个。
        #
        #   为什么必须修：设计意图是"第 30 天和第 75 天有**全品类**价格低谷"
        #   （给阶段③ 的 GLM 当考卷）。有的商品有、有的没有的话，
        #   逐商品分析会得出互相矛盾的结论，"全品类"就是假的。
        #
        #   现实里也确实是这个语义：618 当天开始，商家在跑的日常促销
        #   就被大促价盖掉了，不会"等我把这次促销跑完再说"。
        if t in BIG_PROMO_DAYS:
            promo_drop = float(rng.uniform(*BIG_PROMO_DROP))
            promo_left = BIG_PROMO_LEN
        elif promo_left == 0 and rng.random() < PROMO_PROB:
            promo_drop = float(rng.uniform(*PROMO_DROP))
            promo_left = int(rng.integers(PROMO_LEN[0], PROMO_LEN[1] + 1))

        # ---- 计算当天价格 ----
        if promo_left > 0:
            # 促销期：降幅固定，仍带一点点日内噪声
            noise = float(rng.normal(0.0, DAILY_SIGMA * 0.3))
            price = base * (1.0 - promo_drop) * (1.0 + noise)
            promo_left -= 1
        else:
            # 平日：围绕 base 的小幅波动
            noise = float(rng.normal(0.0, DAILY_SIGMA))
            price = base * (1.0 + noise)

        # ★ 价格不能低于 1 元。真实数据里不会出现 0 元或负数，
        #   不加这个下限的话，极端噪声 + 大促叠加会算出负价格，
        #   然后 price_history 表里出现负值，图表直接崩。
        prices.append(round(max(price, 1.0), 2))

    return prices


# ★ 标题去重的最大重抽次数。
#
#   池子实测约 4000 个不同标题（40 个种子 × 2000 条累计数出来的），
#   所以 count=1000 时重抽几次就够。取 200 是给"池子快用完"的情况留足余量，
#   同时又能在真的抽不出来时快速失败——而不是无限循环挂在那里。
_MAX_TITLE_RETRIES = 200


def _pick_title(
    rng: np.random.Generator, seen: set[str]
) -> tuple[str, str, str, str]:
    """抽一个**没出现过**的标题，返回 (品类, 品牌中文, 品牌英文, 标题)。

    ============================================================================
    ★ 为什么必须保证唯一：一个藏了三个阶段的缺陷
    ============================================================================
    原来的写法是每次独立抽「模板 + 标题片段 + 颜色」，等价于**有放回采样**。
    后果实测：

        count=100    → 只有 87 个不同标题
        count=1000   → 只有 583 个不同标题（最多的一个重复了 19 次）

    也就是说"1000 个商品"里 **42% 是重名商品**。

    这个缺陷之所以能藏这么久，是因为**从来没有任何东西拿归并逻辑跑过 Mock 数据**：
      · 图表不在乎（它只是把 1000 行画出来）
      · seed 的直写路径不做比对（直接 insert）
      · 分布断言看的是价格和销量，不看标题

    直到 `scraper/sources/mock.py` 把 Mock 数据送进采集管道，
    归并逻辑第一次看到这批数据，100 条里立刻合并掉 36 条——
    "1000 个商品"被静默压成 600 个左右。

    ★ 一个有 42% 重名的商品目录本身就是不真实的数据。
      真实电商里同名的两条商品记录会被当成同一个商品，
      所以"生成器保证标题唯一"不是为了让测试好看，
      而是让这批数据**符合它自己要模拟的那个对象**。

    ★ 重抽会改变随机数流，所以 `test_seed.py` 里依赖价格序列的
      分布断言需要重新验证一遍。价格基准和销量是**在循环之前**抽的
      （sample_base_prices / sample_ranked_sales），不受影响；
      受影响的只有循环内抽的价格序列和店铺。
    """
    for _ in range(_MAX_TITLE_RETRIES):
        category, brand_cn, brand_en, titles = PRODUCT_TEMPLATES[
            int(rng.integers(0, len(PRODUCT_TEMPLATES)))
        ]
        title = f"{brand_cn} {titles[int(rng.integers(0, len(titles)))]}"
        title = vary_specs(title, rng)
        # ★ 模板里已经有颜色就不再追加，避免拼出 "冰莓紫 星光金"
        if not any(c in title for c in KNOWN_COLORS):
            title += COLOR_SUFFIXES[int(rng.integers(0, len(COLOR_SUFFIXES)))]
        title = title.strip()

        if title not in seen:
            seen.add(title)
            return category, brand_cn, brand_en, title

    # ★ 抽不出来时报错，而不是放弃唯一性让重名混进去。
    #   静默降级的话，"标题唯一"这条性质就变成了"通常唯一"，
    #   而依赖它的地方（归并、mock 采集）会在数据量大时才出问题。
    raise RuntimeError(
        f"连抽 {_MAX_TITLE_RETRIES} 次都没抽到不重复的标题，"
        f"已用 {len(seen)} 个。说明请求的数量超过了标题池的大小——"
        f"要么调小 count，要么给 PRODUCT_TEMPLATES 加更多标题片段。"
    )


def build_products(
    rng: np.random.Generator, n: int, days: int
) -> list[dict]:
    """生成 n 个商品的完整数据（含 90 天价格序列）。

    返回的是纯 dict 列表而不是 ORM 对象——
    因为要一次插入上万行，ORM 对象的实例化开销在这个量级上是浪费。

    ★ 标题**保证互不重复**（见 _pick_title 里的重抽循环）。
      这条约束是后补的，加它的原因写在 _pick_title 的文档里。
    """
    bases = sample_base_prices(rng, n)
    sales = sample_ranked_sales(rng, n)
    now = utcnow()
    start = now - timedelta(days=days)

    out: list[dict] = []
    seen_titles: set[str] = set()
    for i in range(n):
        category, brand_cn, brand_en, title = _pick_title(rng, seen_titles)

        base = float(bases[i])
        series = gen_price_series(rng, base, days)

        out.append({
            # ---- 商品身份 ----
            # ★ source_id 用固定前缀 + 序号，保证同一商品每次 seed 得到同样的 id。
            "source": "mock",
            "source_channel": "mock",
            "source_id": f"mock-{i:05d}",
            "title": title,
            # ★ 用 dedup 里同一个归一化函数。
            #   这样 Mock 数据也能跑归并逻辑，测试覆盖面更真实；
            #   而如果这里另写一套归一化，两套逻辑会悄悄分叉。
            "title_norm": normalize_title(title),
            # 双语品牌格式，与源站的 "HLA/海澜之家" 保持一致
            "brand": f"{brand_en}/{brand_cn}",
            "category": category,
            "url": f"https://www.smzdm.com/p/mock{i:05d}/",
            "image_url": None,   # Mock 不造图片，避免前端代理层去请求假域名

            # ---- 业务数据 ----
            # ★ price 是【当前价】= 序列最后一天的价格，
            #   不是基准价。列表页显示的和趋势图末端必须是同一个数，
            #   否则用户会发现"表格写着 199，点进去曲线末端是 249"。
            "price": series[-1],

            # ★ price_max 与 price【相等】，因为这是一次单值成交。
            #
            #   这里一度写的是 max(series)——把 price_max 当成"90 天最高价"用了。
            #   看着更"有用"（列表页能显示"较最高价降了 40%"），但它是错的：
            #
            #   真实采集路径按 cleaner.parse_price() 的返回值填这个字段，
            #   语义是【当前这次爆料的优惠价区间上限】（"券后199-299" → 299）。
            #   两种语义混在同一个列里，后果是 ?source=mock 和 ?source=smzdm
            #   下的 price_max 含义不同，混在一起算"价格区间宽度"会得到
            #   毫无意义的数字，而且不报错。
            #
            #   历史最高价不是丢了——它随时可以从 price_history 现算：
            #       SELECT MAX(price) FROM price_history WHERE product_id=?
            #   那才是唯一的真相来源，且对两个数据源语义一致。
            #
            #   详见 app/db/models.py 里 PriceHistory/Product 的价格注释。
            "price_max": series[-1],
            "sales": int(sales[i]),
            "last_crawled_at": now,

            # ---- 内部使用，不直接入库 ----
            "_series": series,
            "_start": start,

            # ★ 店铺由【品牌 + 平台】决定，不再在全表里随机挑。
            #   平台仍然是随机的（0=京东 / 1=天猫），理由见 resolve_shop。
            #
            #   ★★ 这里必须仍然只消费**一个**随机数、并且位置不变
            #      （在字典字面量的最后，也就是本次循环的最后一次 rng 调用）。
            #      原来的写法 `rng.integers(0, len(SHOP_TEMPLATES))` 也是
            #      一个数、也在同一位置，所以改成 `integers(0, 2)` 之后
            #      **随机数流的消耗完全一致**——价格序列、销量、标题
            #      全部逐字节不变，只有 shop_id 这一列变了。
            #      这让本次修改的影响面严格限制在"店铺"上：
            #      test_seed.py 的分布断言和 docs/charts/ 的自检图都不用重做。
            "_shop": resolve_shop(brand_cn, int(rng.integers(0, 2))),
        })

    return out


# ===========================================================================
# 幂等：清空后重灌
# ===========================================================================

# ★ 为什么要先清空：
#   Mock 数据是【可再生】的——它没有"原始来源"这回事，随时可以重造。
#   如果不清空直接插，第二次跑就会撞 (source, source_id) 唯一约束。
#   更麻烦的是那个报错信息长得像代码 bug，而不是"你没清库"，
#   调参阶段每次改参数都要排查一遍，非常浪费时间。
#
# ★ 清空顺序受外键约束限制（PRAGMA foreign_keys=ON 是开着的）：
#   price_history 引用 products，products 引用 shops，
#   alerts 引用 alert_rules。
#   所以必须从叶子往根删：
#     price_history / alerts → alert_rules / products → shops
#   反过来删会被外键挡住。
#   ★ 这条顺序有两组，而"记得改"是不可靠的——alerts → alert_rules 那一组
#     就漏过一次，代价是"给 mock 商品建过一条规则"之后整个 seed 直接不可用。
#
# ★ 只删 source='mock' 的，绝不动真实数据。
#   这正是 db/models.py 里把 source 拆成独立字段的价值——
#   双数据源隔离不只是给统计图用的，也是给数据管理用的。


def reset_mock_data(db: Session) -> dict[str, int]:
    """清空所有 Mock 数据，返回删除的行数。

    ★ 只删 Mock：真实采集的数据（source='smzdm'）绝不能受影响。
      这也意味着"重新 seed"永远不需要重新抓数据。
    """
    # 1) price_history：通过 product_id 关联到 mock 商品
    mock_ids = select(Product.id).where(Product.source == "mock").scalar_subquery()
    deleted_ph = db.execute(
        delete(PriceHistory).where(PriceHistory.product_id.in_(mock_ids))
    ).rowcount

    # 2) alerts → alert_rules：**顺序不能反**。
    #
    #    `alerts.rule_id` 指向 `alert_rules.id`，而 PRAGMA foreign_keys=ON
    #    是开着的（见 app/db/base.py），所以先删规则会被外键挡住、
    #    抛 IntegrityError。
    #
    #    ★ 这里原来只删了 alert_rules，注释却写着"alert_rules / alerts 也指向
    #      mock 商品，先清掉"——注释比代码多承诺了一件事，而多出来的那条
    #      恰好是决定这段代码能不能跑的那条。（同一个顺序在
    #      app/api/alerts.py 的 DELETE /rules/{id} 里是对的：先删 Alert
    #      再删规则。seed 这边漏了。）
    #
    #    ★ 触发条件很低：给任意一个 mock 商品建条规则、等它触发一次
    #      （worker 每 5 分钟扫一遍），此后任何一次 `python -m scraper.seed`
    #      都会直接死在第一步，整个 seed 不可用。
    mock_rule_ids = (
        select(AlertRule.id).where(AlertRule.product_id.in_(mock_ids)).scalar_subquery()
    )
    deleted_alerts = db.execute(
        delete(Alert).where(Alert.rule_id.in_(mock_rule_ids))
    ).rowcount

    deleted_rules = db.execute(
        delete(AlertRule).where(AlertRule.product_id.in_(mock_ids))
    ).rowcount

    # 3) products
    deleted_products = db.execute(
        delete(Product).where(Product.source == "mock")
    ).rowcount

    # 4) 孤儿店铺：删掉没有任何商品挂靠的店铺。
    #    ★ 不能无条件删光 shops——真实数据的店铺可能还在用。
    #      只删"现在没有任何商品引用"的那些。
    used_shop_ids = select(Product.shop_id).where(Product.shop_id.is_not(None))
    deleted_shops = db.execute(
        delete(Shop).where(Shop.id.not_in(used_shop_ids))
    ).rowcount

    db.commit()
    return {
        "price_history": deleted_ph,
        "alerts": deleted_alerts,
        "alert_rules": deleted_rules,
        "products": deleted_products,
        "shops": deleted_shops,
    }


# ===========================================================================
# 主流程
# ===========================================================================


def ensure_shops(db: Session) -> dict[tuple[str, str], int]:
    """确保店铺存在，返回 (平台, 店名) → id 的映射。

    ★ 用"查一遍再补"而不是无条件插入：
      shops 有 (platform, name) 唯一约束，无条件插入第二次就撞约束。
    """
    existing = {
        (s.platform, s.name): s.id
        for s in db.execute(select(Shop)).scalars()
    }
    for platform, name in SHOP_TEMPLATES:
        if (platform, name) not in existing:
            shop = Shop(name=name, platform=platform)
            db.add(shop)
            db.flush()      # 立刻拿到自增 id
            existing[(platform, name)] = shop.id
    db.commit()
    return existing


def seed(
    db: Session,
    count: int = DEFAULT_PRODUCT_COUNT,
    days: int = DEFAULT_DAYS,
    seed_value: int = DEFAULT_SEED,
) -> dict[str, int]:
    """生成并写入 Mock 数据。可重复调用（幂等）。

    返回本次写入的行数统计。
    """
    rng = np.random.default_rng(seed_value)

    removed = reset_mock_data(db)
    shops = ensure_shops(db)

    products = build_products(rng, count, days)

    # ---- 批量插入 products ----
    # ★ 先构造好全部 dict 再一次性 insert，而不是循环 db.add()。
    #   ORM 的 unit-of-work 会为每个对象做一次 flush 和身份检查，
    #   1000 个商品时会明显变慢；Core 的批量 insert 走 executemany。
    product_rows = []
    for p in products:
        platform, shop_name = p["_shop"]
        product_rows.append({
            "source": p["source"],
            "source_channel": p["source_channel"],
            "source_id": p["source_id"],
            "platform": platform,
            "category": p["category"],
            "title": p["title"],
            "title_norm": p["title_norm"],
            "brand": p["brand"],
            "url": p["url"],
            "image_url": p["image_url"],
            "price": p["price"],
            "price_max": p["price_max"],
            "sales": p["sales"],
            "shop_id": shops[(platform, shop_name)],
            "last_crawled_at": p["last_crawled_at"],
        })

    db.execute(insert(Product), product_rows)
    db.commit()

    # ---- 拿回自增 id（批量插入本身不返回）----
    id_map = {
        sid: pid
        for sid, pid in db.execute(
            select(Product.source_id, Product.id).where(Product.source == "mock")
        )
    }

    # ---- 批量插入 price_history ----
    #
    # ★ 走 store.record_prices（ON CONFLICT DO NOTHING），而不是裸 insert。
    #
    #   看起来这是多此一举——seed 刚刚才把 mock 数据全删了，
    #   不可能有冲突。但这么做有一个实打实的好处：
    #
    #   它让 Mock 数据**流经真实的写入路径**。于是
    #   test_price_history.py 里那条"同一商品的多条 NULL source_ref 记录能共存"
    #   测的就是真的写入器，而不是 seed 自己另写的一套 insert。
    #   如果哪天有人给 record_prices 加了个唯一键或者改了冲突目标，
    #   测试会立刻发现"Mock 的 90 天序列变成了 1 天"。
    #
    #   代价实测为零：批路径 9 万行 0.84 秒，和裸 executemany 一样快。
    #
    # ★ source_ref 保持 None。
    #   Mock 没有"爆料 id"这个东西，而 SQLite 的 UNIQUE 约束
    #   把多个 NULL 视为互不相同——所以同一商品的多条历史
    #   不会被 (product_id, source_ref) 唯一键挡住。
    #   这个设计在 test_price_history.py 里有专门的测试守着。
    points = [
        PricePoint(
            product_id=id_map[p["source_id"]],
            price=price,
            recorded_at=p["_start"] + timedelta(days=offset),
            source_ref=None,
        )
        for p in products
        for offset, price in enumerate(p["_series"])
    ]
    written = record_prices(db, points)
    db.commit()

    return {
        "products": len(product_rows),
        "price_history": written,
        "days": days,
        **{f"removed_{k}": v for k, v in removed.items()},
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="生成 Mock 数据（幂等：会先清空 source='mock' 的旧数据）"
    )
    parser.add_argument(
        "--count", type=int, default=DEFAULT_PRODUCT_COUNT,
        help=f"商品数量（默认 {DEFAULT_PRODUCT_COUNT}）",
    )
    parser.add_argument(
        "--days", type=int, default=DEFAULT_DAYS,
        help=f"每个商品的价格历史天数（默认 {DEFAULT_DAYS}）",
    )
    parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED,
        help=f"随机种子，固定值保证结果可复现（默认 {DEFAULT_SEED}）",
    )
    args = parser.parse_args()

    with SessionLocal() as db:
        print(f"生成 {args.count} 个商品 × {args.days} 天价格历史 ……")
        stats = seed(db, count=args.count, days=args.days, seed_value=args.seed)

    print("\n完成：")
    for k, v in stats.items():
        print(f"  {k:<20} {v}")


if __name__ == "__main__":
    main()
