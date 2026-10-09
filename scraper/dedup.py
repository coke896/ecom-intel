"""去重与事件归并。

抓到的每一条是【爆料】（价格事件）而不是【商品】：同一商品被反复爆料时，
归并它们就自然形成价格时间序列——价格趋势图在真实数据上唯一的数据来源。

设计：**确定性规则优先，相似度只做确认**（原设计的"相似度判定"在 277 条
真实标题标定中被数据推翻，全过程见 docs/可行性评估.md §12.4）：
  第一层 硬门槛（品牌 + 型号 token）——只负责排除"不是同一个"；
  第二层 包含度相似度——只在第一层通过后确认，高分归并、中间只标记交人工。
确定性规则能证明"不是同一个"、证明不了"是同一个"；相似度反之。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from scraper.cleaner import normalize_width

# --- 标题归一化 ---

# 促销噪声词：高频出现但完全不携带商品身份信息。★ 这份表是【判定逻辑的一部分】——
# 改动会直接影响归并结果，所以和阈值一起被版本管理、被测试覆盖，不放进配置文件。
# （停用词表是另一回事，只影响词云展示，在 data/stopwords.txt 里）
NOISE_WORDS: tuple[str, ...] = (
    # 促销前缀
    "今日必买", "百亿补贴", "移动端", "京东自营", "天猫精选", "小编精选",
    "值友专享", "会员专享", "手慢无", "绝对值", "历史低价", "限时",
    "限量", "秒杀", "抢购", "预售", "定金", "尾款",
    # 促销后缀
    "需用券", "领券", "优惠券", "下单", "到手价", "包邮", "直降",
    "门店有售", "国补", "补贴", "plus会员", "会员",
    # 营销形容词（对身份判定无信息量）
    "新品", "新款", "爆款", "热卖", "特价", "清仓", "超值", "划算",
)

# 标点：全部换成空格，避免 "iPhone18Pro" 和 "iPhone-18-Pro" 被当成不同的词。
RE_PUNCT = re.compile(r"[（）()\[\]【】<>《》,，.。、;；:：!！?？\"'“”‘’/\\|~—\-+*#@&%￥$]+")

RE_WS = re.compile(r"\s+")

# ★ 型号后缀词表：这些词是纯字母、不含数字，"提取含数字的 token" 抓不到。
# 漏掉它们会让 'iPhone 18 Pro' 和 'iPhone 18 Pro Max' 得到相同的型号集合
# {18, pro}、通过硬门槛被误判为同一商品——而 Pro 和 Pro Max 是两个 SKU。
MODEL_SUFFIX_WORDS: frozenset[str] = frozenset({
    "pro", "max", "plus", "ultra", "mini", "air", "lite", "se", "duo",
    "gen", "note", "prime", "edge", "core",
    "青春版", "至尊版", "标准版", "旗舰版", "尊享版", "典藏版", "限定版",
})

# ★ 已知行为：`+` 被归一化当标点丢掉，所以 "Pro+" 与 "Pro" 坍缩成同一个 token
# （小米 15 Pro / Pro+ 会被判成同一商品）。真实数据带 `+` 的 6 条全是规格分隔
# （12GB+256GB），丢掉 `+` 恰好保住了区分；测试 test_pro_plus_collapses_into_pro 钉住。

# 含数字的 token 即为型号标识（18 / 12gb / cp13 / 10000毫安 / 2）
RE_HAS_DIGIT = re.compile(r"\d")

# ★ 字母与数字的边界：同款型号写起来带不带空格不确定（'freeclip2' 与
# 'FreeClip 2' 是同款；不切分则型号 token 不相等 → 硬门槛判成不同商品）。
# 代价是可能制造 'A1' vs 'A 1' 这类假阳性，但边界是书写习惯、不是型号差异。
RE_ALNUM_BOUNDARY = re.compile(r"(?<=[0-9])(?=[a-z])|(?<=[a-z])(?=[0-9])")


def normalize_title(title: str) -> str:
    """标题归一化：全半角 → 小写 → 去促销噪声词 → 去标点 → 压缩空白。

    ★ 关键约束：不能丢掉规格 token（容量/颜色/型号/数字），硬门槛依赖它们——
      所有操作都是"替换成空格"而不是"删除并拼接"（"12GB+256GB" → "12gb 256gb"）。
    """
    text = normalize_width(title).lower()

    # 噪声词替换成空格而不是删掉，否则 "百亿补贴酷态科" 会变成一个词切不出来。
    for w in NOISE_WORDS:
        wl = w.lower()
        if wl in text:
            text = text.replace(wl, " ")

    text = RE_PUNCT.sub(" ", text)
    # 边界切分（'freeclip2' → 'freeclip 2'）必须在标点处理之后、空白压缩之前——
    # 它插入的是空格，让最后的压缩把连续空格收干净。
    text = RE_ALNUM_BOUNDARY.sub(" ", text)
    return RE_WS.sub(" ", text).strip()


def tokenize(norm_title: str) -> set[str]:
    """把归一化后的标题切成 token 集合。

    ★ 用空格切分而不是 jieba：jieba 的切分随词典版本变化、归并结果不可复现；
      而中文电商标题里区分度最高的信息（型号、容量）本来就是空格分隔的。
    """
    return set(norm_title.split())


def extract_model_tokens(norm_title: str) -> frozenset[str]:
    """提取型号 token：含数字的 token + 型号后缀词（硬门槛的核心）。

    返回 frozenset 是为了能直接做集合比较。
    """
    result = set()
    for tok in norm_title.split():
        if RE_HAS_DIGIT.search(tok):
            result.add(tok)
        elif tok in MODEL_SUFFIX_WORDS:
            result.add(tok)
    return frozenset(result)


def display_brand(brand_field: str | None) -> str | None:
    """品牌的**显示形态** —— 原样保留源站的写法（库里存这个，给人看）。

    比对用的规范形态由 extract_brand 现算、从不落库。二者必须分开，否则
    "Soundcore/声阔" 会被当成一个整体去比，同品牌商品永远匹配不上（静默）。
    """
    if not brand_field:
        return None
    b = RE_WS.sub(" ", normalize_width(str(brand_field))).strip()
    return b or None


def extract_brand(brand_field: str | None) -> str | None:
    """确定品牌的**规范形态** —— 比对用（小写、两段排序后用 "|" 连接）。

    ★ 只用源站字段、不从标题猜（猜错会同时污染硬门槛和品牌分析）。
    ★ 幂等：喂规范形态进去出来的还是它自己，所以调用方不必知道手上的
      字符串是源站原文还是库里的显示形态。
    """
    if not brand_field:
        return None
    b = normalize_width(str(brand_field)).strip().lower()
    if not b:
        return None
    # "HLA/海澜之家" → 取两段都保留，用 | 连接作为规范形式
    parts = [p.strip() for p in b.split("/") if p.strip()]
    return "|".join(sorted(parts)) if parts else None


def brands_compatible(a: str | None, b: str | None) -> bool:
    """品牌是否相容（硬门槛第 1 条）。

    ★ 提取不出品牌时**视为通过**（"无证据不否决"）：否则白牌商品
      （brand 恒为 None）永远无法归并。代价是可能放进假阳性，由相似度层兜住。
    """
    if a is None or b is None:
        return True
    # 品牌字段可能是 "huawei|华为"，标题里可能只有其中一个，做包含判断
    set_a, set_b = set(a.split("|")), set(b.split("|"))
    return bool(set_a & set_b)


def models_compatible(a: frozenset[str], b: frozenset[str]) -> bool:
    """型号 token 是否相容（硬门槛第 2 条）。

    ★ 要求**严格相等**，不接受子集/交集：放宽会让 'iPhone 18 Pro' 和
      'iPhone 18 Pro Max'（两个 SKU）相容。漏归并只是少几个价格点，
      错归并会让价格序列混进别的商品的价格——取保守的，漏的进人工核对。
    """
    return a == b


# --- 相似度 ---


def containment(a: set[str], b: set[str]) -> float:
    """包含度 = |A∩B| / min(|A|,|B|)。

    ★ 用它而不是 Dice / Jaccard：同商品的两条爆料常是包含关系（一长一短），
      短标题的词几乎都在长标题里；Dice/Jaccard 对长度差异敏感，会把
      "信息更完整的同一商品"挡在阈值外。标定数据见 docs/可行性评估.md。
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


# --- 归并决策 ---

# ★ 自动归并的包含度阈值。标定依据（277 条真实标题、123 对人工标注）：
#   硬门槛内需拒绝的对最高 0.667、需接受的对最低 0.400——两类仍重叠，
#   0.75 是"高于假阳性上限"的保守线，裕度 0.083，换语料要重新量。
#   重叠区同款落灰色地带（≥0.50）标进人工队列；更低者静默漏掉（已知代价）。
MERGE_THRESHOLD = 0.75

# 灰色地带下界。低于它的直接当新商品处理，不做标记——
# 否则 suspected_duplicate_of 会充满假阳性，人工永远核对不完。
GREY_ZONE_THRESHOLD = 0.50


class Decision(str, Enum):
    """归并决策。用枚举而不是字符串常量，避免拼写错误静默通过。"""

    MERGE = "merge"   # 判定为同一商品，归并（挂到已有 product_id）
    MARK = "mark"     # 灰色地带：新建商品，但记录 suspected_duplicate_of
    NEW = "new"       # 判定为新商品，不标记


@dataclass(frozen=True, slots=True)
class Candidate:
    """一条待归并的爆料。frozen=True：比对过程中会被反复读，防意外修改。"""

    source: str
    source_id: str
    title: str
    brand_field: str | None = None


@dataclass(frozen=True, slots=True)
class ExistingProduct:
    """已在库里的商品（归并比对的目标）。"""

    product_id: int
    title_norm: str
    brand: str | None
    model_tokens: frozenset[str]
    tokens: frozenset[str]
    # 源站 id，唯一作用是让 decide() 能认出自比对（见下面的守卫）；默认 None。
    source_id: str | None = None


@dataclass(frozen=True, slots=True)
class MergeResult:
    """归并判定的结果。"""

    decision: Decision
    target_id: int | None = None
    score: float = 0.0
    reason: str = ""


def build_signature(candidate: Candidate) -> tuple[str, frozenset[str], frozenset[str]]:
    """从候选爆料算出 (归一化标题, 型号 token, 全部 token)。"""
    norm = normalize_title(candidate.title)
    return norm, extract_model_tokens(norm), frozenset(tokenize(norm))


def decide(candidate: Candidate, existing: list[ExistingProduct]) -> MergeResult:
    """判定这条爆料应该归并到哪个已有商品，还是新建（本模块主入口）。

    调用方语义：MERGE → 写价格点 + 更新 products 的价格字段；
    MARK → 新建商品并把 suspected_duplicate_of 指向 target_id；NEW → 新建商品。
    ★ MERGE 分支里那个 UPDATE 不能省——列表页显示的价格来自 products。
    """
    norm, model_toks, toks = build_signature(candidate)
    cand_brand = extract_brand(candidate.brand_field)

    # 两个"待人工核对"候选：near_miss（硬门槛过了但相似度差一点，更值得看）
    # 与 conflict（相似度很高但型号 token 不同——更可能确实是两个商品）。
    near_miss: tuple[float, int] | None = None
    conflict: tuple[float, int] | None = None

    for prod in existing:
        # ---- 第 0 层：不能跟自己比对（纵深防御，主防线在 ingest.resolve_target）----
        # ★ 池子里可能有这条爆料自己（第二轮会这样），不排除则包含度 1.0
        # 会归并到它自己——数据无害所以完全静默，只让报告变成假数字。
        if prod.source_id is not None and prod.source_id == candidate.source_id:
            continue

        # ---- 第一层：确定性规则，负责排除 ----
        if not brands_compatible(cand_brand, prod.brand):
            continue

        score = containment(toks, set(prod.tokens))
        gate_ok = models_compatible(model_toks, prod.model_tokens)

        if gate_ok:
            if score >= MERGE_THRESHOLD:
                return MergeResult(
                    decision=Decision.MERGE,
                    target_id=prod.product_id,
                    score=score,
                    reason=f"硬门槛通过 + 包含度 {score:.3f} ≥ {MERGE_THRESHOLD}",
                )
            if score >= GREY_ZONE_THRESHOLD and (
                near_miss is None or score > near_miss[0]
            ):
                near_miss = (score, prod.product_id)
        else:
            # ★ 硬门槛拦下的是最危险的对（'iPhone 18 Pro' vs 'iPhone Duo'）
            # ——绝不自动合并；但文本仍高度相似时说明"看着像但不是"，
            # 正是最该让人看一眼的：漏归并可人工恢复，直接丢弃则永远发现不了。
            if score >= MERGE_THRESHOLD and (
                conflict is None or score > conflict[0]
            ):
                conflict = (score, prod.product_id)

    if near_miss is not None:
        return MergeResult(
            decision=Decision.MARK,
            target_id=near_miss[1],
            score=near_miss[0],
            reason=(
                f"硬门槛通过但包含度 {near_miss[0]:.3f} 落在 "
                f"[{GREY_ZONE_THRESHOLD}, {MERGE_THRESHOLD})，不自动合并，待人工核对"
            ),
        )

    if conflict is not None:
        return MergeResult(
            decision=Decision.MARK,
            target_id=conflict[1],
            score=conflict[0],
            reason=(
                f"包含度 {conflict[0]:.3f} 很高但型号 token 不同被硬门槛拦下，"
                f"不自动合并，待人工核对"
            ),
        )

    return MergeResult(decision=Decision.NEW, reason="硬门槛未通过或无相似候选")


# --- URL 归一化 ---

# 追踪参数。不带这些参数的 URL 和带参数的指向同一篇文章，
# 不剥离的话同一条爆料会被当成两条。
TRACKING_PARAMS: frozenset[str] = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "from", "share_token", "ref", "spm", "scm", "suen", "s",
})


def normalize_url(url: str | None) -> str | None:
    """URL 归一化：统一协议、去追踪参数、去尾部斜杠、统一大小写。

    ★ 本项目不用 URL 做去重主键（用的是 (source, source_id)）——它给
      "源站没给 id"的场景兜底，并用于展示和图片代理的域名校验。
    """
    if not url:
        return None
    try:
        p = urlparse(url.strip())

        # http 和 https 视为同一个页面
        scheme = "https"

        netloc = p.netloc.lower()
        if netloc.endswith(":80"):
            netloc = netloc[:-3]
        elif netloc.endswith(":443"):
            netloc = netloc[:-4]

        # 其余参数按 key 排序，保证顺序无关
        query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                 if k.lower() not in TRACKING_PARAMS]
        query.sort()

        # 去尾部斜杠（根路径 "/" 除外）
        path = p.path.rstrip("/") or "/"

        return urlunparse((scheme, netloc, path, "", urlencode(query), ""))
    except (ValueError, AttributeError):
        return None
