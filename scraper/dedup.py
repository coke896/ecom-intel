"""去重与事件归并。

================================================================================
这个模块要解决什么问题
================================================================================
SMZDM 抓到的每一条是【爆料】（deal / 价格事件），不是【商品】。
同一个商品可能被人反复爆料，于是产生多篇文章、多个价格、多个时间点。

如果不去重：products 表里躺着一堆"同一个商品的不同文章"，
           统计"有多少个商品在卖"直接错。
如果归并得好：同一商品的多次爆料会自然形成一条【真实的价格时间序列】，
           这正是"价格趋势图"在真实数据上唯一能长出数据点的途径。

================================================================================
★ 核心设计：确定性规则优先，相似度只做确认
================================================================================
我最初的设计是「硬门槛（品牌+规格）+ 软门槛（Dice ≥ 0.75）」，
软门槛负责判定"是不是同一商品"。

**用 277 条真实爆料标题标定后，这个设计被推翻了。** 见 docs/可行性评估.md
的标定章节，这里只记结论：

  三种相似度指标（Dice / Jaccard / 包含度）在中文短标题上
  **都无法区分真阳性和假阳性**：

    真阳性（同商品）'华为 FreeClip 2 长标题' vs '短标题'    Dice = 0.545
    假阳性（不同商品）'斯凯奇 女士跑鞋' vs '男士健步鞋'      Dice = 0.545
                                    ↑ 分数相同，标签相反

  更危险的是 'iPhone 18 Pro' vs 'iPhone Duo'（不同商品）Dice = 0.727，
  **高于**四个真阳性中的三个。任何单一阈值都不可能同时收下真阳性、
  排除这个假阳性。

所以架构改成：

  【第一层 确定性规则】品牌 + 型号 token —— 负责排除，不负责确认
        型号 token 不相等 → 直接判定为不同商品，根本不进相似度计算。
        这一层拦下了 'iPhone 18 Pro vs iPhone Duo'、'FreeClip 2 vs FreeClip 1'
        这类**文本极像但确实是不同商品**的对——它们才是真正的危险。

  【第二层 相似度确认】包含度 —— 只负责确认，不负责判定
        仅在第一层通过后才计算。达到高阈值 → 自动归并；
        落在中间 → 只标记，不合并，交人工核对。

这样分工的理由：**确定性规则可以证明"不是同一个"，但证明不了"是同一个"**
（品牌型号都对也可能只是同系列）；而相似度可以提示"可能是同一个"，
但不足以单独下结论。让两者各做擅长的事。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from scraper.cleaner import normalize_width

# ===========================================================================
# 标题归一化
# ===========================================================================

# 促销噪声词：高频出现但【完全不携带商品身份信息】。
# "今日必买：海澜之家羊毛衫" 和 "海澜之家羊毛衫" 是同一个商品。
#
# ★ 为什么放在代码里而不是配置文件：
#   这份表是【判定逻辑的一部分】——改动它会直接影响归并结果，
#   所以它应该和阈值一起被版本管理、被测试覆盖。
#   （停用词表是另一回事，那个只影响词云展示，放 data/stopwords.txt 让用户改）
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

# ★ 型号后缀词表。
#
# 为什么需要它：这些词都是【纯字母】，不含数字，所以"提取含数字的 token"
# 这个朴素规则抓不到它们。但漏掉它们的后果很严重：
#
#   'iPhone 18 Pro'     型号 token = {18, pro}
#   'iPhone 18 Pro Max' 型号 token = {18, pro, max}
#
# 如果 max 不被算作型号 token，上面两条就会得到相同的型号集合 {18, pro}，
# 通过硬门槛，然后被相似度判定为"同一商品"——而 Pro 和 Pro Max
# 是两个不同的 SKU，价格也不同。这类错误会静默污染价格序列。
#
# 表里的词是"同一系列内用来区分型号"的标识，跨品类通用。
MODEL_SUFFIX_WORDS: frozenset[str] = frozenset({
    "pro", "max", "plus", "ultra", "mini", "air", "lite", "se", "duo",
    "gen", "note", "prime", "edge", "core",
    "青春版", "至尊版", "标准版", "旗舰版", "尊享版", "典藏版", "限定版",
})

# ★ 这里曾经有一条 "pro+",已经删掉了——它是**永远不可能命中**的条目。
#
#   原因：`normalize_title()` 会把 `+` 当标点丢掉，所以标题里的
#   "Pro+" 在进入这张表之前就已经变成 "pro" 了。下面这个分支
#   (`elif tok in MODEL_SUFFIX_WORDS`) 收到的 token 里
#   不存在 "pro+"，那条目一次都跑不到。
#
#   ★ 它比"没写这一条"更坏：它让人以为 Pro+ 被处理了。
#     实际发生的是——`Pro` 和 `Pro+` 归一化后**字符串完全相同**：
#
#         小米 15 Pro 手机  → '小米 15 pro 手机'  型号 {15, pro}
#         小米 15 Pro+ 手机 → '小米 15 pro 手机'  型号 {15, pro}
#
#     型号集合相同 → 硬门槛放行 → 包含度 1.000 → 判定 MERGE。
#     两个不同 SKU 的价格序列被混在一起。
#     （这正是本表开头那段注释想拦的 Pro / Pro Max 情形的同类；
#       区别是 Max 拦住了，而 + 没拦住。）
#
#   ★ 那为什么现在只是删条目、不改归一化：
#     实测真实数据（100 条 smzdm 标题）里带 `+` 的一共 6 条，
#     **没有一条是这个形态**——全是规格分隔和套餐连接：
#         12GB+256GB / 12+512 / 1壶3杯+收纳包 / XQG120-...U1+GA120-...
#     而这些恰好是丢掉 `+` 才对：`12GB+256GB` → {12, 256}，
#     `12GB+512GB` → {12, 512}，两者的区分**保住了**。
#     也就是说 `+` 在真实数据里的主要身份是**分隔符**，不是 SKU 后缀，
#     而当前行为对它是对的。
#
#   ★ 真要修 Pro+ 得改 normalize_title 保留 `+`，那会让
#     `products.title_norm` 这一列出现"新旧数据用两套规则算出来"的问题
#     （models.py 专门为这个存了一列，就是不想让归一化规则改动
#      造成新旧不一致）——需要一次数据回填，不是改一行函数。
#     在没有真实案例的情况下付这个代价不值得。
#     这条行为由 tests/test_dedup.py::test_pro_plus_collapses_into_pro 钉住。
# ★ 另注："plus"（字母拼写）在表里且可达，所以
#   "Pro Plus" 能正确区分，只有符号写法 "Pro+" 不行。

# 含数字的 token 即为型号标识（18 / 12gb / cp13 / 10000毫安 / 2）
RE_HAS_DIGIT = re.compile(r"\d")

# ★ 字母与数字的边界。
#
# 为什么必须切：同一个型号，用户写起来【带不带空格是不确定的】。
# 标定时就撞到了这个：
#
#   '华为 FreeClip 2 耳夹式多单元动圈蓝牙耳机 冰莓紫 Type-C'  → 型号 token {2}
#   '华为 freeclip2 耳夹式蓝牙耳机 冰莓紫'                    → 型号 token {freeclip2}
#
# 两条是同款，但 token 不相等，硬门槛把它们判成了不同商品（假阴性）。
#
# 在字母和数字的交界处插一个空格后，'freeclip2' 变成 'freeclip 2'，
# 两边都得到 {2}。
#
# ★ 这个改动当然有代价：它会让原本不相等的一对变成相等，
#   所以理论上可能制造假阳性（'A1' vs 'A 1' 这种）。
#   接受它的理由是：【型号名里的字母数字边界本来就是书写习惯问题，
#   不是型号差异】——真正不同的型号不会只差一个空格。
#   而它修掉的假阴性（'freeclip2' vs 'FreeClip 2'）在真实数据里反复出现。
RE_ALNUM_BOUNDARY = re.compile(r"(?<=[0-9])(?=[a-z])|(?<=[a-z])(?=[0-9])")


def normalize_title(title: str) -> str:
    """标题归一化：全半角 → 小写 → 去促销噪声词 → 去标点 → 压缩空白。

    ★ 关键约束：**不能丢掉规格 token**（容量/颜色/型号/数字）。
      硬门槛依赖它们工作。这里做的所有操作都是"替换成空格"，
      而不是"删除并拼接"——所以 "12GB+256GB" 会变成 "12gb 256gb"
      两个独立 token，信息完整保留。
    """
    text = normalize_width(title).lower()

    # 噪声词替换成空格而不是删掉。
    # ★ 删掉的话 "百亿补贴酷态科" 会变成一个词，切不出 "酷态科"。
    for w in NOISE_WORDS:
        wl = w.lower()
        if wl in text:
            text = text.replace(wl, " ")

    text = RE_PUNCT.sub(" ", text)
    # 字母数字边界切分（'freeclip2' → 'freeclip 2'），必须在标点处理之后、
    # 空白压缩之前——它插入的是空格，让最后的压缩把连续空格收干净。
    text = RE_ALNUM_BOUNDARY.sub(" ", text)
    return RE_WS.sub(" ", text).strip()


def tokenize(norm_title: str) -> set[str]:
    """把归一化后的标题切成 token 集合。

    ★ 用空格切分而不是 jieba：
      1. jieba 的切分结果随词典版本变化，会让归并结果不可复现——
         同一个商品在不同时间跑出的归并结论可能不同，这在数据管道里是灾难。
      2. 本项目要判定的是"两条爆料是不是同一商品"，
         而中文电商标题里区分度最高的信息（型号、容量、品牌）
         本来就是空格分隔的拉丁字符和数字串。
      3. 少一个依赖，少一份不确定性。
    """
    return set(norm_title.split())


def extract_model_tokens(norm_title: str) -> frozenset[str]:
    """提取型号 token：含数字的 token + 型号后缀词。

    这是硬门槛的核心。返回 frozenset 是为了能直接做集合比较。

    >>> sorted(extract_model_tokens("apple iphone 18 pro 5g 手机"))
    ['18', '5g', 'pro']
    """
    result = set()
    for tok in norm_title.split():
        if RE_HAS_DIGIT.search(tok):
            result.add(tok)
        elif tok in MODEL_SUFFIX_WORDS:
            result.add(tok)
    return frozenset(result)


def display_brand(brand_field: str | None) -> str | None:
    """品牌的**显示形态** —— 原样保留源站的写法。

    这个函数存在的理由是一次真实的不一致：`products.brand` 这一列
    曾经有两种格式，取决于数据从哪个入口进来。

        seed.py 直写   →  "Soundcore/声阔"     （双语、保留大小写，给人看的）
        采集管道写入   →  "soundcore|声阔"     （小写、已排序、给机器比的）

    后果不只是"显示不一样"。`brands_compatible()` 靠 "|" 拆分品牌的两段，
    而 `"Soundcore/声阔"` 里没有 "|"，会被当成**一个整体**去比——
    于是同一品牌的 Mock 商品和真实商品在硬门槛上**永远匹配不上**，
    而这种失败是静默的：归并只是"没合并"，不报错。

    修法是把这两件事彻底分开（和 token 的处理方式完全一样）：

        库里存什么   →  显示形态（这个函数），给人看、给界面用
        比对时用什么 →  规范形态（extract_brand），现算，从不落库

    这样两种入口写进去的都是同一回事，比对也永远一致。
    """
    if not brand_field:
        return None
    b = RE_WS.sub(" ", normalize_width(str(brand_field))).strip()
    return b or None


def extract_brand(brand_field: str | None) -> str | None:
    """确定品牌的**规范形态** —— 比对用。

    只用源站给的品牌字段（SMZDM 的 onclick 里有 'brand':'HLA/海澜之家'）。
    ★ 不从标题里猜品牌——计划里明确否决了这条路：
      猜错了会同时污染硬门槛和品牌分析，而且测试用例根本构造不出来。

    ★ 这个函数以前有一个 `norm_title` 参数，但函数体里**从来没读过它**。
      那是早期"从标题猜品牌"方案的残留。删掉它，是因为一个用不到的参数
      会让读代码的人以为这里会看标题——而"品牌只来自源站字段"
      恰恰是硬门槛能被信任的前提。签名里不该有任何暗示相反行为的痕迹。

    源站品牌格式是 "HLA/海澜之家"（英文名/中文名），
    归一化成小写并取两段，便于和标题里的写法对应。

    ★ 这个函数是**幂等**的：喂规范形态进去，出来的还是它自己。
        "Soundcore/声阔"  → "soundcore|声阔"
        "soundcore|声阔"  → "soundcore|声阔"   （已经是规范形态）

      幂等很重要，因为它让调用方不必知道手上的字符串是哪种形态——
      采集管道拿的是源站原文，`load_existing` 拿的是库里的显示形态，
      两边都直接喂进来就对。少一个"这个字段现在是什么格式"的隐式约定，
      就少一整类只在某个入口才出现的 bug。
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

    ★ 提取不出品牌时【视为通过】——"无证据不否决"。

      不加这条的话，白牌/杂牌商品（标题里没有可识别品牌）永远无法归并，
      因为它们的 brand 永远是 None，而 None != None 在语义上无法确认相同。

      这个判断和"疑罪从无"是同一个道理：
      我们没有证据说它们是不同商品，就不该据此否决。
      代价是可能放进一些假阳性，由后面的相似度确认层兜住。
    """
    if a is None or b is None:
        return True
    # 品牌字段可能是 "huawei|华为"，标题里可能只有其中一个，做包含判断
    set_a, set_b = set(a.split("|")), set(b.split("|"))
    return bool(set_a & set_b)


def models_compatible(a: frozenset[str], b: frozenset[str]) -> bool:
    """型号 token 是否相容（硬门槛第 2 条）。

    ★ 要求【严格相等】，而不是"子集"或"有交集"。这是有意的保守选择。

      标定过程中试过放宽成"一个是另一个的子集"，结果是
      'iPhone 18 Pro' {18,pro} 和 'iPhone 18 Pro Max ... 256GB'
      {18,pro,max,256gb} 会被判定相容——但它们是两个 SKU。

      严格相等会漏掉一些真阳性的归并（比如"长标题带规格" vs "短标题不带规格"），
      但漏归并只是少几个价格点，**错归并会让价格序列里混进别的商品的价格，
      分析结论静默失真**。两害相权，取保守的那个。

      漏掉的这些不会消失——它们会落进灰色地带被标记出来，交人工核对。
    """
    return a == b


# ===========================================================================
# 相似度
# ===========================================================================


def containment(a: set[str], b: set[str]) -> float:
    """包含度 = |A∩B| / min(|A|,|B|)。

    ★ 为什么是包含度而不是 Dice / Jaccard：
      同一商品的两条爆料常常是【包含关系】——一条信息少
      （"华为 FreeClip 2 耳夹式耳机"），一条信息多
      （"...多单元动圈蓝牙耳机 冰莓紫 Type-C"）。
      短标题的词几乎都出现在长标题里 → 包含度接近 1。

      而 Dice 和 Jaccard 都对【长度差异】敏感：
      上面那对因为一条有 11 个 token、另一条只有 5 个，
      Dice 只给到 0.545，Jaccard 更是只有 0.375。
      于是"信息更完整的同一商品"反而拿到低分，被阈值挡在外面。

      标定数据见 docs/可行性评估.md。
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


# ===========================================================================
# 归并决策
# ===========================================================================

# ★ 自动归并的包含度阈值。
#
# 标定依据（277 条真实标题、123 对人工标注，见 spike/evaluate_merge.py）：
#   - 通过硬门槛后，需要【拒绝】的对，包含度最高 0.667（斯凯奇不同鞋）
#   - 通过硬门槛后，需要【接受】的对，包含度最低只有 0.400（"Free chip2" 讹写）
#   - 0.857 / 0.833 那些最像的假阳性（iPhone Pro Max vs Pro、Pro vs Duo）
#     已经被硬门槛拦下，到不了这一层——这是零错归并的前提。
#   → 两类【仍然重叠】，0.75 不是天然分界，而是"高于假阳性上限"的保守线：
#     0.75 - 0.667 = 0.083 的裕度，是这批语料的实测性质，换语料要重新量。
#     重叠区里的同款落到灰色地带（≥0.50）被标记进人工队列；
#     低于 0.50 的同款（如讹写到 0.400 的两对）会静默漏掉——已知代价。
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
    """一条待归并的爆料。

    frozen=True：这个对象在归并过程中会被反复比对，
    做成不可变能防止意外修改导致的诡异 bug。
    """

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
    # ★ 源站 id。**唯一的作用是让 decide() 能认出自比对**（见下面的守卫）。
    #   默认 None 是为了兼容不关心这条的调用方与测试。
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
    """判定这条爆料应该归并到哪个已有商品，还是新建。

    这是本模块的主入口。调用方（采集器）拿到结果后：
      MERGE → INSERT 一条 price_history（ON CONFLICT DO NOTHING）
              + UPDATE products 的 price / price_max / last_crawled_at
      MARK  → INSERT 新 product，把 suspected_duplicate_of 指向 target_id
      NEW   → INSERT 新 product

    ★ 注意 MERGE 分支里那个 UPDATE 不能省：
      只写 price_history 不更新 products 的话，列表页显示的还是旧价，
      趋势图和商品列表对不上——而且这种不一致很难被察觉。
    """
    norm, model_toks, toks = build_signature(candidate)
    cand_brand = extract_brand(candidate.brand_field)

    # 两个"待人工核对"候选，优先级不同：
    #   near_miss —— 硬门槛过了但相似度差一点（差 0.75 一点点）
    #   conflict  —— 相似度很高但硬门槛拦下了（型号 token 不同）
    # 前者更值得看：它在我们自己的判定体系里只差最后一步；
    # 后者虽然文本更像，但型号不同往往说明确实是两个商品。
    near_miss: tuple[float, int] | None = None
    conflict: tuple[float, int] | None = None

    for prod in existing:
        # ---- 第 0 层：不能跟自己比对 ----
        # ★ 这条防线在写测试时才被发现是必需的。
        #
        #   池子里装的是"库里已有的商品"，而**这条爆料自己可能就在里面**
        #   （第二轮采集同一篇爆料时会是这样）。不排除的话，
        #   候选跟它自己比对：品牌相同、型号 token 相同、包含度 1.0
        #   → 判定 MERGE，归并到它自己。
        #
        #   数据上无害（价格没变、价格点被唯一约束挡掉），
        #   **所以它完全静默** —— 只是采集报告会变成"新商品 0，归并 100"，
        #   而这恰好是阶段 4 验收要看的那份报告。
        #
        #   主防线在 scraper/ingest.py（那条路径压根不会把已有主的爆料送进来），
        #   这里是纵深防御：decide() 是公开函数，任何调用方都不该踩这个坑。
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
            # ★ 硬门槛拦下的正是最危险的那些对：
            #   'iPhone 18 Pro' vs 'iPhone Duo'（包含度 0.800，但型号词完全不同）
            #   'FreeClip 2' vs 'FreeClip'（文本极像，差一个数字）
            #   它们【绝不自动合并】。
            #
            #   但如果文本仍然高度相似，说明这是"看着像但不是"的典型情况——
            #   恰恰是最该让人看一眼的。漏归并是可以人工恢复的，
            #   直接丢弃则永远发现不了。
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


# ===========================================================================
# URL 归一化
# ===========================================================================

# 追踪参数。不带这些参数的 URL 和带参数的指向同一篇文章，
# 不剥离的话同一条爆料会被当成两条。
TRACKING_PARAMS: frozenset[str] = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "from", "share_token", "ref", "spm", "scm", "suen", "s",
})


def normalize_url(url: str | None) -> str | None:
    """URL 归一化：统一协议、去追踪参数、去尾部斜杠、统一大小写。

    不做这一步的后果是同一商品被当成两个：
      http://www.smzdm.com/p/123/
      https://www.smzdm.com/p/123/?utm_source=weixin
    这两条 URL 指向同一篇文章，字符串却不相等。

    ★ 注意：本项目实际上【不用 URL 做去重主键】——
      用的是 (source, source_id)。URL 归一化是给"源站没给 id"的场景兜底的，
      以及用于展示和图片代理的域名校验。
    """
    if not url:
        return None
    try:
        p = urlparse(url.strip())

        # 统一协议：http 和 https 视为同一个页面
        scheme = "https"

        # 域名小写，去掉默认端口
        netloc = p.netloc.lower()
        if netloc.endswith(":80"):
            netloc = netloc[:-3]
        elif netloc.endswith(":443"):
            netloc = netloc[:-4]

        # 去追踪参数，其余参数按 key 排序保证顺序无关
        query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                 if k.lower() not in TRACKING_PARAMS]
        query.sort()

        # 去尾部斜杠（根路径 "/" 除外）
        path = p.path.rstrip("/") or "/"

        return urlunparse((scheme, netloc, path, "", urlencode(query), ""))
    except (ValueError, AttributeError):
        return None
