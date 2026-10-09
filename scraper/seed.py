"""Mock 数据生成 —— 全项目的地基。

90 天 × N 个商品的 price_history 供前端全部图表与阶段③ 的 GLM 分析使用；
目标是**造出统计特征与真实电商市场一致的数据**，让分析 pipeline 可以被独立验证
（不是"造点数据让页面不空"）。

三个核心设计决策（标定过程见 docs/可行性评估.md）：
  价格形状：对数正态（中位数 150 元）+ 拒绝采样，不能用 clip（越界值堆在边界会露馅）；
  价格演化：所有价格围绕固定 base 波动，绝不逐日累乘（乘性误差会累积，见 gen_price_series）；
  销量分布：幂律且与价格完全解耦（随机性放在排名上，α=0.85）。
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from datetime import timedelta

import numpy as np
from sqlalchemy import delete, insert, select, update
from sqlalchemy.orm import Session

from app.db.base import SessionLocal, utcnow
from app.db.models import Alert, AlertRule, PriceHistory, Product, Shop
from scraper.dedup import normalize_title
from scraper.store import PricePoint, record_prices

# --- 可复现性 ---

# ★ 固定种子 = 结果可复现：否则"代码改动导致图表变了"和"随机波动导致图表变了"分不清。
DEFAULT_SEED = 20260928

DEFAULT_PRODUCT_COUNT = 1000
DEFAULT_DAYS = 90

# --- 价格分布参数（对数正态） ---

# 对数正态的中位数 = exp(μ)：要 150 元的中位数就取 μ = ln(150)。
PRICE_MU = math.log(150.0)

# ★ σ=0.60（实测标定）：p5/p50/p95 ≈ 56/151/402，<50 元约 3.3%、>800 元约 0.4%。
# σ 太小价格全挤在 150 附近没有形状；太大则 30 元以下 / 2000 元以上大量出现，不现实。
PRICE_SIGMA = 0.60

# 合理价格区间。超出这个范围的重抽（拒绝采样），不用 clip。
PRICE_MIN = 50.0
PRICE_MAX = 800.0

# --- 价格演化参数 ---

# 平日的日波动幅度。1.2% 是"价格基本稳定但有小幅调整"的量级。
DAILY_SIGMA = 0.012

# ★ 促销概率 0.05（从 0.07 下调，看价格曲线图后改的）：0.07 时 90 天约 10 次降价、
# 促销天数占比 36%，曲线像心电图；0.05 约 26%（含两次大促），价格变异系数从
# 0.128 拉回 0.11 附近，更接近计划标定的 0.107。
PROMO_PROB = 0.05

# 普通促销的降幅与持续天数
PROMO_DROP = (0.10, 0.32)
PROMO_LEN = (2, 5)

# ★ 模拟大促（618 / 双11）：用固定的第 30/75 天而不是随机，让"大促"在数据里
# **可被检出**（阶段③ 的 GLM 若发现全品类价格低谷，说明它真的在读数据）。
BIG_PROMO_DAYS = (30, 75)
BIG_PROMO_DROP = (0.25, 0.40)
BIG_PROMO_LEN = 5

# --- 销量分布参数 ---

# ★ 幂律指数 0.85：α=1.0 时第 20 名只有第 1 名的 1/30、后十根柱几乎贴地；
# 0.7 太平没有爆款感。0.85 梯度清晰，每根柱都还看得见。
SALES_ALPHA = 0.85

# 名次第 1 的基准销量：让 Top20 的绝对值落在"像真实数码配件销量"的量级。
SALES_TOP = 80_000

# 个体扰动 ±8%：不加的话销量是名次的确定性函数，曲线光滑得反而假。
# 尾部相邻名次只差 4.2%，扰动再大会让名次频繁互换、图表像随机排列；
# 8% 让头部几乎不乱、尾部轻微交错（刻意打破完美严格递减）。
SALES_NOISE = 0.08

# ★ "爆款翻倍"（原计划头部挑 3–5 个翻 2.5–6 倍）实测会破坏幂律梯度——
# 爆款本来就是幂律头部，额外翻倍是重复计算，已移除，改为小幅个体扰动。

# --- 商品文案素材（数码配件品类） ---

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
# 完全随机分配店铺会生成"绿联的磁吸指环扣在声阔天猫旗舰店销售"这种内部矛盾
# （店铺名里写着另一个品牌），直到阶段③ 被 AI 报告暴露才被发现——Mock 数据
# "是假的"和"自相矛盾"是两回事，后者会让整份报告的可信度一起垮掉。
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
    """按品牌挑一个店铺，返回 (平台, 店铺名)。`platform_pick` 0/1 = 京东 / 天猫。

    ★ 收整数而不是自己掷随机数：调用方必须**用掉那一次 rng 调用**，少消费一个
    随机数会让后面全部数据的生成序列偏移（价格、销量、标题全变，测试基线作废）。
    """
    platform = "京东" if platform_pick == 0 else "天猫"
    shops = brand_shops.get(brand_cn, {})
    name = shops.get(platform) or GENERIC_SHOPS[platform]
    return platform, name

# (品类, 品牌中文名, 品牌英文名, 标题片段)
# ★ 品牌中英文名都要有：源站是 "HLA/海澜之家" 双语格式，Mock 同格式才能覆盖真实归一化逻辑。
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

# 标题模板里已经写死的颜色：用于避免拼出"冰莓紫 星光金"这种双颜色标题。
KNOWN_COLORS: tuple[str, ...] = (
    "黑色", "白色", "深空灰", "冰莓紫", "星光金", "远峰蓝",
    "橄榄金", "勃艮第酒红", "原色钛金属",
)

# ★ 规格数值的可选值：按单位给出该品类下合理的取值。
# 模板×颜色只能拼出 ~495 个标题，1000 个商品会一半重名；替换"数字+单位"后
# 标题空间扩到几万——同一个型号本来也有不同容量的 SKU 在卖。
SPEC_CHOICES: dict[str, tuple[str, ...]] = {
    "毫安": ("5000", "10000", "15000", "20000", "30000"),
    "W": ("20", "22.5", "30", "45", "65", "100", "120", "140"),
    "米": ("0.5", "1", "1.5", "2", "3"),
    "Gbps": ("10", "20", "40"),
}

# 匹配「数字 + 单位」。单位保留在捕获组里，替换时原样拼回去。
RE_SPEC_NUM = re.compile(r"\d+(?:\.\d+)?\s*(?:毫安|W|米|Gbps)")


def vary_specs(title: str, rng: np.random.Generator) -> str:
    """把标题里的规格数字替换成同类目下的其他合理取值（见 SPEC_CHOICES）。

    ★ 只动"数字 + 单位"：'CP13'、'10号电能棒'、'USB4' 里的数字是型号名，改了就不是那个商品。
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


# --- 采样函数 ---


def quantize_price(price: float) -> float:
    """把价格吸附到"心理定价"上：真实电商是 149.9 / 299 / 59.9，不是 187.34。

    小额 → 整数 + 0.9；中额 → 10 的倍数 - 0.1；大额 → 50 的倍数 - 0.1。
    减 0.1 不加 0.1（299.9 比 300.1 更像真实定价，左位数效应）。
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

    ★ 拒绝采样而不是 np.clip：clip 把越界值**堆在边界上**，直方图两端出现尖柱；
    拒绝采样丢弃重抽、分布自然衰减（实测丢弃率约 3.7%）。
    ★ 循环上限 200 轮是保险：正常几轮就收敛，防止参数被改坏时死循环。
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

    ★ 热度分与价格**完全解耦**（不接收 prices 参数）；随机性放在【排名】这一步，
    "第 k 名该卖多少"是确定的，幂律形状不被破坏。返回数组按【原名次】排列。
    """
    heat = rng.lognormal(0.0, 1.0, size=n)

    # 按热度排名（order[0] 最热），再映射成 1 开始的名次
    order = np.argsort(-heat)
    ranks = np.empty(n, dtype=int)
    ranks[order] = np.arange(1, n + 1)

    sales = SALES_TOP * ranks.astype(float) ** (-SALES_ALPHA)

    noise = 1.0 + rng.uniform(-SALES_NOISE, SALES_NOISE, size=n)
    sales = sales * noise

    # ★ 下限 1：0 与 NULL 语义不同（"卖光了" vs "不知道"），且前端对数轴/占比会除零。
    return np.maximum(np.round(sales), 1).astype(np.int64)


def gen_price_series(
    rng: np.random.Generator, base: float, days: int
) -> list[float]:
    """生成一个商品 days 天的价格序列。

    ★ 所有价格围绕【固定的 base】波动，绝不围绕前一天累乘：
    错法 p[t] = p[t-1] * (1 + ε) 的乘性误差会累积——实测日波动 σ=0.012 时，
    90 天后价格中位数跌到初始值的 12%。对法 p[t] = base * (1 + ε) 促销后自然回归。
    """
    prices: list[float] = []
    promo_left = 0          # 当前促销还剩几天
    promo_drop = 0.0        # 当前促销的降幅

    for t in range(days):
        # ---- 促销开始判定 ----
        # ★ 大促必须【无条件覆盖】进行中的普通促销（判断在 promo_left == 0 之外），
        # 否则被跳过时"第 30/75 天全品类价格低谷"不成立——两个大促日合计约 30% 商品缺至少一个。
        if t in BIG_PROMO_DAYS:
            promo_drop = float(rng.uniform(*BIG_PROMO_DROP))
            promo_left = BIG_PROMO_LEN
        elif promo_left == 0 and rng.random() < PROMO_PROB:
            promo_drop = float(rng.uniform(*PROMO_DROP))
            promo_left = int(rng.integers(PROMO_LEN[0], PROMO_LEN[1] + 1))

        # ---- 计算当天价格 ----
        if promo_left > 0:
            noise = float(rng.normal(0.0, DAILY_SIGMA * 0.3))
            price = base * (1.0 - promo_drop) * (1.0 + noise)
            promo_left -= 1
        else:
            noise = float(rng.normal(0.0, DAILY_SIGMA))
            price = base * (1.0 + noise)

        # ★ 下限 1 元：极端噪声 + 大促叠加会算出负价格，price_history 出现负值图表直接崩。
        prices.append(round(max(price, 1.0), 2))

    return prices


# ★ 标题去重的最大重抽次数：池子实测约 4000 个不同标题，count=1000 几次就够；
# 取 200 是留足余量又能快速失败，而不是无限循环挂在那里。
_MAX_TITLE_RETRIES = 200


def _pick_title(
    rng: np.random.Generator, seen: set[str]
) -> tuple[str, str, str, str]:
    """抽一个**没出现过**的标题，返回 (品类, 品牌中文, 品牌英文, 标题)。

    ★ 必须保证唯一：有放回采样时 count=1000 里约 42% 重名，直到 mock 数据
    送进采集管道、归并逻辑首次看到它才暴露（100 条里合并掉 36 条）——
    一个有 42% 重名的商品目录本身就是不真实的数据。
    """
    for _ in range(_MAX_TITLE_RETRIES):
        category, brand_cn, brand_en, titles = PRODUCT_TEMPLATES[
            int(rng.integers(0, len(PRODUCT_TEMPLATES)))
        ]
        title = f"{brand_cn} {titles[int(rng.integers(0, len(titles)))]}"
        title = vary_specs(title, rng)
        # ★ 已有颜色就不再追加，避免拼出 "冰莓紫 星光金"
        if not any(c in title for c in KNOWN_COLORS):
            title += COLOR_SUFFIXES[int(rng.integers(0, len(COLOR_SUFFIXES)))]
        title = title.strip()

        if title not in seen:
            seen.add(title)
            return category, brand_cn, brand_en, title

    # ★ 抽不出来就报错，不静默放弃唯一性——"通常唯一"会在数据量大时才出问题。
    raise RuntimeError(
        f"连抽 {_MAX_TITLE_RETRIES} 次都没抽到不重复的标题，"
        f"已用 {len(seen)} 个。说明请求的数量超过了标题池的大小——"
        f"要么调小 count，要么给 PRODUCT_TEMPLATES 加更多标题片段。"
    )


def build_products(
    rng: np.random.Generator, n: int, days: int
) -> list[dict]:
    """生成 n 个商品的完整数据（含 90 天价格序列），标题保证互不重复（见 _pick_title）。

    返回纯 dict 而不是 ORM 对象：要一次插入上万行，ORM 实例化开销在这个量级上是浪费。
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
            # ★ source_id 用固定前缀+序号，保证同一商品每次 seed 得到同样的 id。
            "source": "mock",
            "source_channel": "mock",
            "source_id": f"mock-{i:05d}",
            "title": title,
            # ★ 与 dedup 共用同一个归一化函数：Mock 数据也能跑归并逻辑；另写一套会悄悄分叉。
            "title_norm": normalize_title(title),
            # 双语品牌格式，与源站的 "HLA/海澜之家" 保持一致
            "brand": f"{brand_en}/{brand_cn}",
            "category": category,
            "url": f"https://www.smzdm.com/p/mock{i:05d}/",
            "image_url": None,   # Mock 不造图片，避免前端代理层去请求假域名

            # ---- 业务数据 ----
            # ★ price 是【当前价】= 序列最后一天价格，不是基准价：列表页与趋势图
            # 末端必须是同一个数，否则"表格写 199、曲线末端 249"。
            "price": series[-1],

            # ★ price_max 与 price 相等（单值成交），语义与采集路径（cleaner.parse_price
            # 的"爆料价区间上限"）一致，不能写成"90 天最高价"——否则两个数据源同列不同义
            # 且不报错；历史最高价随时可从 price_history 现算（SELECT MAX(price)…）。
            "price_max": series[-1],
            "sales": int(sales[i]),
            "last_crawled_at": now,

            # ---- 内部使用，不直接入库 ----
            "_series": series,
            "_start": start,

            # ★ 店铺由【品牌 + 平台】决定（平台仍随机，理由见 resolve_shop）；
            # 这里必须仍只消费**一个**随机数且位置不变——随机数流的消耗与旧实现
            # 完全一致，价格序列、销量、标题逐字节不变，影响面严格限制在 shop_id。
            "_shop": resolve_shop(brand_cn, int(rng.integers(0, 2))),
        })

    return out


# --- 幂等：清空后重灌 ---

# ★ 先清空：Mock 数据可再生；不清的话第二次跑会撞 (source, source_id) 唯一约束，
# 报错却像代码 bug。删除顺序受外键约束（foreign_keys=ON）：price_history / alerts
# → alert_rules / products → shops，反向会被挡。★ suspected_duplicate_of 是自引用
# 外键，不属于"顺序"能解决的一类，只能显式置空（详见函数内）——只删 source='mock'。


def reset_mock_data(db: Session) -> dict[str, int]:
    """清空所有 Mock 数据，返回删除的行数。

    ★ 只删 Mock（source='smzdm' 绝不受影响），所以"重新 seed"不需要重新抓数据。
    """
    # 1) price_history：通过 product_id 关联到 mock 商品
    mock_ids = select(Product.id).where(Product.source == "mock").scalar_subquery()
    deleted_ph = db.execute(
        delete(PriceHistory).where(PriceHistory.product_id.in_(mock_ids))
    ).rowcount

    # 2) alerts → alert_rules：**顺序不能反**——alerts.rule_id 指向 alert_rules.id，
    # 先删规则会被外键挡住（foreign_keys=ON，见 app/db/base.py）。给 mock 商品建过
    # 规则之后，任何一次 seed 都会死在这一步。
    mock_rule_ids = (
        select(AlertRule.id).where(AlertRule.product_id.in_(mock_ids)).scalar_subquery()
    )
    deleted_alerts = db.execute(
        delete(Alert).where(Alert.rule_id.in_(mock_rule_ids))
    ).rowcount

    deleted_rules = db.execute(
        delete(AlertRule).where(AlertRule.product_id.in_(mock_ids))
    ).rowcount

    # 3) ★ 先解开"指向 mock 商品的疑似重复指针"再删商品：suspected_duplicate_of 是
    # 自引用外键且建表时没有 ondelete，指针所在行不在被删批次里——没有先后可调，
    # 只能显式置空（这条线索按定义已失效）。不给 FK 加 ondelete 一劳永逸的理由：
    # SQLite 改不了已有约束（要重建整张表），新库老库行为会悄悄分叉（docs 16.10）。
    db.execute(
        update(Product)
        .where(Product.suspected_duplicate_of.in_(mock_ids))
        .values(suspected_duplicate_of=None)
    )

    # 3b) products
    deleted_products = db.execute(
        delete(Product).where(Product.source == "mock")
    ).rowcount

    # ★ 那次 update 的行数不取：返回值语义是"删除的行数"（main() 直接打印成清单）。

    # 4) 孤儿店铺：只删没有任何商品引用的——真实数据的店铺可能还在用。
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


# --- 主流程 ---


def ensure_shops(db: Session) -> dict[tuple[str, str], int]:
    """确保店铺存在，返回 (平台, 店名) → id 的映射。

    ★ 用"查一遍再补"而不是无条件插入：shops 有 (platform, name) 唯一约束。
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
    """生成并写入 Mock 数据，可重复调用（幂等）。返回本次写入的行数统计。

    ★ 两个不变量：① 参数校验在任何破坏性动作之前；② 先算出全部数据再删库
    （build_products 是纯函数、不碰 db）——否则 --days 0 这类低级错误会先把
    1000 个商品删光再崩，报错却只提"列表下标越界"，看起来像什么都没发生。
    """
    if count < 1:
        raise ValueError(f"count 必须 ≥ 1，收到 {count}（0 个商品没有意义）")
    if days < 1:
        raise ValueError(
            f"days 必须 ≥ 1，收到 {days}。"
            "每个商品至少要有一个价格点——"
            "商品的当前价（price / price_max）就是从价格序列的最后一天取的，"
            "序列为空就没有当前价可写。"
        )

    rng = np.random.default_rng(seed_value)

    # ★ 先算：任何生成阶段的失败都发生在删库之前（见 docstring）。
    products = build_products(rng, count, days)

    removed = reset_mock_data(db)
    shops = ensure_shops(db)

    # ---- 批量插入 products ----
    # ★ 先构造好全部 dict 再一次性 insert：ORM 的 unit-of-work 每对象一次 flush
    # 和身份检查，1000 个商品时明显变慢；Core 批量 insert 走 executemany。
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
    # ★ 走 store.record_prices 而不是裸 insert：让 Mock 数据流经真实写入路径
    # （NULL source_ref 共存的测试测的就是真写入器），冲突语义被改坏时会立刻暴露。
    # ★ source_ref 保持 None：Mock 没有"爆料 id"，而 SQLite 把多个 NULL 视为
    # 互不相同，同一商品的多条历史不会被 (product_id, source_ref) 唯一键挡住。
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

    # ★ 参数错误在**打开数据库之前**就报掉，而且报成人话：ValueError 的 traceback
    # 让人以为程序坏了；这层校验与 seed() 里那道并存（seed() 也会被别的代码调用）。
    if args.count < 1 or args.days < 1:
        # ★ 退出码 2 而不是 1：Windows 习惯"用法错误 = 2"，1 是"跑起来之后失败"。
        print(
            f"参数不对：count={args.count}，days={args.days}。"
            "两者都必须 ≥ 1。\n"
            "（★ 这里必须在动数据库之前拦住：seed 是【先删后建】的，"
            " 生成阶段失败会留下一个已经被清空、但什么都没生成的库。）",
            file=sys.stderr,
        )
        raise SystemExit(2)

    with SessionLocal() as db:
        print(f"生成 {args.count} 个商品 × {args.days} 天价格历史 ……")
        stats = seed(db, count=args.count, days=args.days, seed_value=args.seed)

    print("\n完成：")
    for k, v in stats.items():
        print(f"  {k:<20} {v}")


if __name__ == "__main__":
    main()
