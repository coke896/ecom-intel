"""归并逻辑测试。

用例几乎全部来自真实 SMZDM 标题，且来自**一次失败的标定**：最初的「硬门槛 + Dice
软门槛」设计在 277 条真实标题上被推翻——三种相似度指标都分不开真阳和假阳
（'iPhone 18 Pro' vs 'iPhone Duo' 拿到 Dice 0.727），test_never_merges_dangerous_pairs
是那次标定的**回归测试**。★ 浮点比较用 pytest.approx，不要用 ==。
"""

from __future__ import annotations

import pytest

from scraper.dedup import (
    GREY_ZONE_THRESHOLD,
    MERGE_THRESHOLD,
    MODEL_SUFFIX_WORDS,
    Candidate,
    Decision,
    ExistingProduct,
    brands_compatible,
    build_signature,
    containment,
    decide,
    display_brand,
    extract_brand,
    extract_model_tokens,
    models_compatible,
    normalize_title,
    normalize_url,
    tokenize,
)

# 真实语料（从 277 条真实爆料标题里摘出来的）

T_FREECLIP2_LONG = "华为 FreeClip 2 耳夹式多单元动圈蓝牙耳机 冰莓紫 Type-C"
T_FREECLIP2_SHORT = "华为 FreeClip 2 耳夹式耳机"
T_FREECLIP2_NOSPACE = "华为 freeclip2 耳夹式蓝牙耳机 冰莓紫"
T_FREECLIP2_TYPO = "华为 Free chip2 耳夹式无线蓝牙耳机"
T_FREECLIP1_OPEN = "华为 FreeClip 开放式设计 耳夹式蓝牙耳机"
T_FREECLIP1_CHARM = "华为 FreeClip 耳夹耳机 UltraCareX钻扣配饰"

T_IPHONE_18PRO = "Apple 苹果 iPhone 18 Pro 5G手机"
T_IPHONE_18PROMAX = "Apple iPhone 18 Pro Max 5G手机 256GB 勃艮第酒红色"
T_IPHONE_DUO = "Apple 苹果 iPhone Duo 5G手机"

T_KUTAI_10_BEFORE = "百亿补贴：酷态科 充电宝10号电能棒电芯10000毫安"
T_KUTAI_10_AFTER = "酷态科 充电宝10号电能棒电芯10000毫安"
T_KUTAI_CP13 = "百亿补贴：酷态科 CP13电能块自带线电芯10000毫安"

T_SKECHERS_W = "斯凯奇 5折 舒适缓震 女士跑鞋"
T_SKECHERS_M = "斯凯奇 5折 Slip-ins闪穿 男士健步鞋"

T_MILIANG_CART = "米良品 家用便携可折叠拖轮购物车"
T_MILIANG_EAR = "米良品 创意电动发光自吸掏耳神器"


def sig(title: str) -> tuple[str, frozenset[str], frozenset[str]]:
    """从标题算出 (归一化标题, 型号 token, 全部 token)。"""
    return build_signature(Candidate(source="smzdm", source_id="0", title=title))


def existing_of(product_id: int, title: str, brand: str | None = None) -> ExistingProduct:
    """把一条标题包装成"库里已有的商品"，用于比对。"""
    norm, model_toks, toks = sig(title)
    return ExistingProduct(
        product_id=product_id,
        title_norm=norm,
        brand=brand,
        model_tokens=model_toks,
        tokens=toks,
    )


def decide_against(candidate_title: str, existing_title: str, **kw) -> Decision:
    """把两条标题跑一遍完整判定，返回决策。"""
    cand = Candidate(source="smzdm", source_id="1", title=candidate_title, **kw)
    return decide(cand, [existing_of(1, existing_title)]).decision


# 标题归一化


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # 标点切成空格，避免 "iPhone-18-Pro" 和 "iPhone 18 Pro" 被当成不同的词
        ("iPhone-18-Pro", "iphone 18 pro"),
        ("A+B", "a b"),
        ("IPHONE 18 PRO", "iphone 18 pro"),
        ("１９９元", "199元"),
        # 促销噪声词去掉
        ("百亿补贴：酷态科 充电宝", "酷态科 充电宝"),
        ("今日必买：京觅 猕猴桃", "京觅 猕猴桃"),
        ("好价 包邮 苹果", "好价 苹果"),
        ("  a   b  ", "a b"),
    ],
)
def test_normalize_title(title: str, expected: str) -> None:
    assert normalize_title(title) == expected


def test_noise_word_removal_does_not_glue_words_together() -> None:
    """★ 噪声词必须【替换成空格】，不能直接删掉：删掉的话 "百亿补贴酷态科" 会粘成
    一个 token，切不出 "酷态科"，品牌就丢了。"""
    assert "酷态科" in tokenize(normalize_title("百亿补贴酷态科 充电宝"))
    assert "补贴" not in normalize_title("百亿补贴：酷态科")


# 字母数字边界：标定时发现的真 bug


@pytest.mark.parametrize(
    ("title", "expected_tokens"),
    [
        ("华为 freeclip2 耳机", {"华为", "freeclip", "2", "耳机"}),
        ("酷态科 CP13电能块", {"酷态科", "cp", "13电能块"}),
        ("iPhone18Pro", {"iphone", "18", "pro"}),
        ("256GB", {"256", "gb"}),
    ],
)
def test_alnum_boundary_split(title: str, expected_tokens: set[str]) -> None:
    """★ 字母和数字的交界处要切开。
    ★ 标定时撞到的 bug：'FreeClip 2'（带空格）和 'freeclip2'（不带）的型号 token
      分别是 {2} 和 {freeclip2}，硬门槛把同款判成了两个商品。"""
    assert tokenize(normalize_title(title)) == expected_tokens


def test_freClip2_spacing_variants_have_same_model_tokens() -> None:
    """同一型号的带空格/不带空格写法，型号 token 必须一致。"""
    _, model_a, _ = sig("华为 FreeClip 2 耳夹式耳机")
    _, model_b, _ = sig("华为 freeclip2 耳夹式蓝牙耳机")
    assert model_a == model_b == frozenset({"2"})


# 型号 token 提取 —— 硬门槛的核心


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # 含数字的 token。★ '5G手机' 切分后是 '5' + 'g手机'——'g手机' 不进
        #   型号集，'5' 进（4G 和 5G 确实是不同 SKU）。
        (T_IPHONE_18PRO, {"18", "pro", "5"}),
        (T_FREECLIP2_SHORT, {"2"}),
        # ★ 纯字母的型号后缀词也要抓——否则 iPhone 18 Pro 和 18 Pro Max 型号集相同
        ("Apple iPhone 18 Pro 手机", {"18", "pro"}),
        ("Apple iPhone 18 Pro Max 手机", {"18", "pro", "max"}),
        # 中文型号后缀
        ("红米 Note 青春版", {"note", "青春版"}),
        # 完全没有型号信息
        ("斯凯奇 舒适缓震 女士跑鞋", set()),
    ],
)
def test_extract_model_tokens(title: str, expected: set[str]) -> None:
    assert extract_model_tokens(normalize_title(title)) == frozenset(expected)


def test_model_suffix_words_catch_pro_vs_pro_max() -> None:
    """★ 这是 MODEL_SUFFIX_WORDS 存在的唯一理由：'Pro'/'Max' 是纯字母不含
    数字，"只提取含数字的 token"会让 'iPhone 18 Pro' 和 'Pro Max' 型号集都是
    {18}，通过硬门槛后被包含度 0.857 判为同一商品——但它们是两个 SKU。"""
    _, pro, _ = sig("Apple 苹果 iPhone 18 Pro 5G手机")
    _, pro_max, _ = sig("Apple iPhone 18 Pro Max 5G手机 256GB")
    assert pro != pro_max
    assert "max" in pro_max
    assert "max" not in pro


# 硬门槛


def test_brands_compatible_fail_open_on_none() -> None:
    """★ 提取不出品牌时视为【通过】——无证据不否决；不加这条的话，白牌/杂牌商品
    （brand 永远是 None）永远无法归并。"""
    assert brands_compatible(None, None) is True
    assert brands_compatible(None, "huawei|华为") is True
    assert brands_compatible("huawei|华为", None) is True


def test_brands_compatible_matches_any_alias() -> None:
    """源站品牌是 'HLA/海澜之家' 形式，任一段对上都算相容。"""
    assert brands_compatible("huawei|华为", "huawei|华为") is True
    assert brands_compatible("huawei|华为", "华为|honor") is True   # 有一段重合
    assert brands_compatible("huawei|华为", "apple|苹果") is False


@pytest.mark.parametrize(
    ("brand_field", "expected"),
    [
        ("HLA/海澜之家", "hla|海澜之家"),
        ("华为", "华为"),
        ("", None),
        (None, None),
        ("  ", None),
    ],
)
def test_extract_brand(brand_field: str | None, expected: str | None) -> None:
    """★ 参数名从 `title` 改成 `brand_field`：早期"从标题猜品牌"的方案已被否决
    （猜错会同时污染硬门槛和品牌分析），留着旧名会让人以为这里会看标题。"""
    assert extract_brand(brand_field) == expected


# 品牌的双形态：库里存显示形态，比对时现算规范形态。★ 来源是一次真实的不一致：
# products.brand 同时存在过 "Soundcore/声阔" 和 "soundcore|声阔" 两种格式（取决于
# seed 直写还是采集管道），后果是同一品牌在硬门槛上永远匹配不上——不报错。


@pytest.mark.parametrize(
    "canonical",
    ["soundcore|声阔", "skechers|斯凯奇", "huawei|华为", "华为"],
)
def test_extract_brand_is_idempotent(canonical: str) -> None:
    """★ 幂等：喂规范形态进去，出来的还是它自己。
    ★ 让调用方**不必知道**手上的字符串是哪种形态（采集管道拿源站原文、`load_existing`
      拿库里显示形态），少一个隐式约定就少一类最难复现的 bug。
    ★ 实现靠的是规范形态里没有 "/"。"""
    assert extract_brand(canonical) == canonical


def test_extract_brand_round_trips_from_display_form() -> None:
    """显示形态 → 规范形态：seed 直写（左）和源站字段（右）两种入口
    得到同一个结果。"""
    assert extract_brand("Soundcore/声阔") == extract_brand("soundcore|声阔")
    # 英文名和中文名的先后顺序不影响结果（实现里做了排序）
    assert extract_brand("斯凯奇/SKECHERS") == extract_brand("SKECHERS/斯凯奇")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("SKECHERS/斯凯奇", "SKECHERS/斯凯奇"),   # 大小写、斜杠都保留
        ("  Soundcore/声阔  ", "Soundcore/声阔"),  # 首尾空白压掉
        ("Soundcore / 声阔", "Soundcore / 声阔"),  # ★ 中间空白**不**动
        ("", None),
        (None, None),
        ("   ", None),
    ],
)
def test_display_brand(raw: str | None, expected: str | None) -> None:
    """显示形态只做一件事：去首尾空白。**不做**小写、不换分隔符——显示形态给
    人看，超出这个的改写会让界面显示源站从没写过的品牌名；归一化是
    extract_brand 的活，两者各管各的。"""
    assert display_brand(raw) == expected


def test_display_brand_is_idempotent() -> None:
    """★ 幂等：显示形态喂进去，出来的还是它自己。
    ★ `ingest` 会对已经洗过一次的值再调一次（clean_deal 洗、写库前又洗），不幂等会
      被反复加工（实测出问题的是全角/半角转换）。"""
    for raw in ["SKECHERS/斯凯奇", "Soundcore/声阔", "华为", "ＨＬＡ"]:
        once = display_brand(raw)
        assert display_brand(once) == once, f"{raw!r} 洗两次和洗一次不一样"


def test_models_compatible_requires_exact_equality() -> None:
    """★ 型号 token 要求【严格相等】，不是子集、不是有交集。
    ★ 放宽成子集相容：'iPhone 18 Pro' {18,pro} 会与 'Pro Max … 256GB'
      {18,pro,max,256,gb} 相容——但它们是两个 SKU。代价是漏掉"长标题带规格"的同款，
      见下方 test_known_cost_of_strict_equality。"""
    assert models_compatible(frozenset({"18"}), frozenset({"18"})) is True
    assert models_compatible(frozenset(), frozenset()) is True
    # 子集不算相容
    assert models_compatible(frozenset({"18"}), frozenset({"18", "256"})) is False
    assert models_compatible(frozenset({"18", "pro"}), frozenset({"duo"})) is False


def test_known_cost_of_strict_equality() -> None:
    """★ 严格相等的【已知代价】，明确记下来而不是假装没有。
    '华为 Pura X Max 12GB+256GB' 与 'HUAWEI 华为 Pura X Max 手机' 是同款，但后者
    省略规格，型号集 {max,12,gb,256} vs {max} 不相等 → 漏归并。代价是【主动接受】：
    漏归并只少几个价格点，放宽却会让 iPhone 18 Pro / Pro Max 被错归并、静默污染
    价格序列。★ 想让这类对被人看到，改 GREY_ZONE 那段逻辑。"""
    _, a, _ = sig("华为 Pura X Max 12GB+256GB 橄榄金小艺伴随式AI 华为大阔折叠手机")
    _, b, _ = sig("HUAWEI 华为 Pura X Max 手机")
    assert a != b
    assert decide_against(
        "HUAWEI 华为 Pura X Max 手机",
        "华为 Pura X Max 12GB+256GB 橄榄金小艺伴随式AI 华为大阔折叠手机",
    ) in (Decision.NEW, Decision.MARK)


# 包含度


def test_containment_bounds() -> None:
    assert containment(set(), set()) == 0.0
    assert containment({"a"}, set()) == 0.0
    assert containment({"a", "b"}, {"a", "b"}) == pytest.approx(1.0)
    # 完全包含 → 1.0
    assert containment({"a"}, {"a", "b", "c"}) == pytest.approx(1.0)
    # 分母是较短的那个
    assert containment({"a", "b"}, {"a", "b", "c", "d"}) == pytest.approx(1.0)


def test_containment_beats_dice_on_subset_relations() -> None:
    """★ 为什么选包含度而不是 Dice / Jaccard：同一商品的两条爆料常常是【包含
    关系】（短标题的词几乎都在长标题里），而 Dice/Jaccard 对长度差异敏感，会
    把这种关系压到阈值以下。"""
    short = {"华为", "freeclip", "2", "耳夹式耳机"}
    long = {"华为", "freeclip", "2", "耳夹式多单元动圈蓝牙耳机", "冰莓紫", "type", "c"}

    dice = 2 * len(short & long) / (len(short) + len(long))
    jaccard = len(short & long) / len(short | long)

    # 交集 3 个；|short|=4，|long|=7
    assert containment(short, long) == pytest.approx(3 / 4)      # 0.750 刚过阈值
    assert dice == pytest.approx(2 * 3 / (4 + 7))                # 0.545 过不了
    assert jaccard == pytest.approx(3 / (4 + 7 - 3))             # 0.375 差得更远


# ★★★ 核心回归：致命假阳性永远不能被自动归并


@pytest.mark.parametrize(
    ("title_a", "title_b", "why"),
    [
        (
            T_IPHONE_18PRO,
            T_IPHONE_DUO,
            "型号词完全不同（18 Pro vs Duo），包含度却有 0.833",
        ),
        (
            T_IPHONE_18PROMAX,
            T_IPHONE_18PRO,
            "Pro 与 Pro Max 是两个 SKU，只差一个纯字母后缀词，包含度 0.857",
        ),
        (
            T_FREECLIP2_SHORT,
            T_FREECLIP1_OPEN,
            "一代 vs 二代，文本极像，只差一个数字，包含度 0.500",
        ),
        (
            T_FREECLIP2_SHORT,
            T_FREECLIP1_CHARM,
            "FreeClip 2 vs FreeClip 一代配饰装",
        ),
        (
            T_KUTAI_CP13,
            T_KUTAI_10_BEFORE,
            "同品牌同容量，型号不同（10号 vs CP13）",
        ),
        (
            T_SKECHERS_W,
            T_SKECHERS_M,
            "同品牌同折扣，品类词不同（女士跑鞋 vs 男士健步鞋）",
        ),
        (
            T_MILIANG_CART,
            T_MILIANG_EAR,
            "同品牌，完全不同品类",
        ),
    ],
)
def test_never_merges_dangerous_pairs(title_a: str, title_b: str, why: str) -> None:
    """★ 这个文件最重要的一条：不同商品永远不能被自动归并。
    这些对全部来自真实语料，前两对的文本相似度高于多个真阳性——把决策交给相似度
    阈值它们必然被错并。允许 NEW 或 MARK，唯独不能是 MERGE。"""
    result = decide(
        Candidate(source="smzdm", source_id="1", title=title_b),
        [existing_of(1, title_a)],
    )
    assert result.decision is not Decision.MERGE, (
        f"错误归并了两条不同商品！\n  A: {title_a}\n  B: {title_b}\n  原因: {why}"
    )
    assert result.target_id is None or result.decision is Decision.MARK


def test_dangerous_pairs_are_flagged_not_silently_dropped() -> None:
    """★ 硬门槛拦下但文本高度相似的对应被【标记】而不是静默丢弃：漏归并可
    人工恢复，直接丢弃则永远发现不了。"""
    for a, b in [
        (T_IPHONE_18PROMAX, T_IPHONE_18PRO),
        (T_IPHONE_18PRO, T_IPHONE_DUO),
    ]:
        result = decide(
            Candidate(source="smzdm", source_id="1", title=b),
            [existing_of(1, a)],
        )
        assert result.decision is Decision.MARK, f"{a} / {b} 应该被标记待核对"
        assert "硬门槛" in result.reason


# ★ 已知的漏网之鱼：Pro+ 与 Pro 会被当成同一款


def test_every_model_suffix_word_can_actually_be_matched() -> None:
    """★ `MODEL_SUFFIX_WORDS` 里不许有**永远命中不了**的条目。
    ★ 判据"从上游推导"而不是照抄清单：构造只含该词的输入、跑真实归一化，看它能
      不能活到比对那一步。
    ★ 测一般性质：写死断言 "pro+" 不在表里的话，下一个 "ultra+" 照样能进——原来
      那条 "pro+" 就是这么进的（`+` 在**归一化阶段**就被丢掉了）。"""
    unreachable = sorted(
        word
        for word in MODEL_SUFFIX_WORDS
        if word not in normalize_title(f"某商品 {word} 型号").split()
    )
    assert not unreachable, (
        f"这些词永远命不中，应该删掉（它们只会让人以为处理过了）：{unreachable}"
    )


def test_pro_plus_collapses_into_pro() -> None:
    """★ 记录一个**已知的、被接受的**漏网之鱼：Pro 和 Pro+ 会被当成同一款。
    `normalize_title()` 把 `+` 当标点丢掉，两者归一化后字符串完全相同 → 包含度 1.000
    → 归并。★ 不值得改：实测 100 条真实标题带 `+` 的只有 6 条且全是分隔符
    （`12GB+256GB`），丢掉恰好是对的；改动要回填已入库的 1100 条 `title_norm`——这条
    测试是**路标**：哪天改得能区分，它会红并提示回填。
    ★ 断言 1.000 这个**数字**：只断言 MERGE 的话，"勉强归并"和"完全相同"长得一样。"""
    a = "小米 15 Pro 手机 12GB 256GB 白色"
    b = "小米 15 Pro+ 手机 12GB 256GB 白色"

    norm_a = normalize_title(a)
    norm_b = normalize_title(b)
    assert norm_a == norm_b, (
        "归一化规则变了——现在 Pro+ 和 Pro 能区分开了。"
        "那正是好事，但请一并处理 products.title_norm 的回填"
        "（见 scraper/dedup.py 里 MODEL_SUFFIX_WORDS 上方那段说明），"
        "然后回来把这条测试改成断言它们不被归并。"
    )

    result = decide(
        Candidate(source="smzdm", source_id="new", title=b),
        [existing_of(1, a)],
    )
    assert result.decision is Decision.MERGE
    assert result.score == pytest.approx(1.0)


# 真阳性必须能归并


@pytest.mark.parametrize(
    ("title_a", "title_b", "why"),
    [
        (T_FREECLIP2_LONG, T_FREECLIP2_SHORT, "同款，后者省略颜色和接口"),
        (T_FREECLIP2_LONG, T_FREECLIP2_NOSPACE, "同款，仅大小写和空格差异"),
        (T_KUTAI_10_BEFORE, T_KUTAI_10_AFTER, "同款，仅促销前缀差异"),
    ],
)
def test_merges_true_positives(title_a: str, title_b: str, why: str) -> None:
    """真阳性必须被自动归并——否则真实数据上价格曲线还是单点。"""
    result = decide(
        Candidate(source="smzdm", source_id="1", title=title_b),
        [existing_of(1, title_a)],
    )
    assert result.decision is Decision.MERGE, (
        f"漏归并了同款！\n  A: {title_a}\n  B: {title_b}\n  原因: {why}\n"
        f"  实际: {result.decision} / {result.reason}"
    )
    assert result.target_id == 1


def test_merge_updates_target_and_reports_score() -> None:
    """归并结果要带目标 id 和分数，调用方靠它写 price_history。"""
    result = decide(
        Candidate(source="smzdm", source_id="1", title=T_KUTAI_10_AFTER),
        [existing_of(7, T_KUTAI_10_BEFORE)],
    )
    assert result.target_id == 7
    assert result.score == pytest.approx(1.0)
    assert "包含度" in result.reason


# 三档决策


def test_new_when_dissimilar() -> None:
    """完全不相干的商品 → 新建，不标记。"""
    result = decide(
        Candidate(source="smzdm", source_id="1", title="罗技 MX Master 3S 无线鼠标"),
        [existing_of(1, T_SKECHERS_W)],
    )
    assert result.decision is Decision.NEW


def test_mark_when_gate_passes_but_similarity_is_grey() -> None:
    """硬门槛过了但相似度落在灰色地带 → 标记待人工核对，不自动合并。
    斯凯奇那对：品牌和型号 token 都相同（型号只有 '5折'），但品类词不同——正是
    "规则看不出来、得靠人"的情况。"""
    result = decide(
        Candidate(source="smzdm", source_id="1", title=T_SKECHERS_M),
        [existing_of(3, T_SKECHERS_W)],
    )
    assert result.decision is Decision.MARK
    assert result.target_id == 3
    assert GREY_ZONE_THRESHOLD <= result.score < MERGE_THRESHOLD


def test_new_when_empty_existing_list() -> None:
    """库里没有任何商品 → 必然新建。"""
    result = decide(Candidate(source="smzdm", source_id="1", title="任意标题"), [])
    assert result.decision is Decision.NEW
    assert result.target_id is None


def test_picks_best_candidate_among_many() -> None:
    """库里有多个候选时，取分数最高的那个。"""
    existing = [
        existing_of(1, T_SKECHERS_W),
        existing_of(2, T_KUTAI_10_BEFORE),
        existing_of(3, T_MILIANG_CART),
    ]
    result = decide(
        Candidate(source="smzdm", source_id="9", title=T_KUTAI_10_AFTER),
        existing,
    )
    assert result.decision is Decision.MERGE
    assert result.target_id == 2


def test_brand_mismatch_blocks_even_identical_titles() -> None:
    """★ 品牌不同即使标题一模一样也不归并：品牌来自源站的结构化字段而非标题，
    是比标题更可信的信号——两个不同品牌的同款白牌商品确实存在。"""
    result = decide(
        Candidate(
            source="smzdm", source_id="1",
            title=T_KUTAI_10_AFTER, brand_field="倍思/Baseus",
        ),
        [existing_of(1, T_KUTAI_10_BEFORE, brand="酷态科/Cuktech")],
    )
    assert result.decision is Decision.NEW


# 阈值标定的回归保护


def test_thresholds_match_calibration() -> None:
    """★ 阈值是标定出来的，不是拍脑袋的（277 条真实标题、123 对人工标注，见
    spike/evaluate_merge.py）：通过硬门槛后需【拒绝】的对包含度最高 0.667、
    需【接受】的最低 0.400——0.75 是高于假阳性上限的保守线（裕度 0.083），
    重叠区的同款由灰色地带（≥0.50）标记进人工队列。"""
    assert MERGE_THRESHOLD == pytest.approx(0.75)
    assert GREY_ZONE_THRESHOLD == pytest.approx(0.50)
    assert GREY_ZONE_THRESHOLD < MERGE_THRESHOLD


def test_mixed_shop_suffix_is_covered_by_threshold() -> None:
    """『XX京东自营旗舰店』这类店铺后缀不该把同款挡在阈值外。"""
    a = "酷态科 充电宝10号电能棒电芯10000毫安"
    b = "京东自营 酷态科 充电宝10号电能棒电芯10000毫安"
    assert decide_against(b, a) is Decision.MERGE


# URL 归一化


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://www.smzdm.com/p/123", "https://www.smzdm.com/p/123"),
        ("https://www.smzdm.com/p/123", "https://www.smzdm.com/p/123"),
        # 去尾部斜杠
        ("https://www.smzdm.com/p/123/", "https://www.smzdm.com/p/123"),
        # 根路径的斜杠要保留
        ("https://www.smzdm.com/", "https://www.smzdm.com/"),
        # 去追踪参数
        (
            "https://www.smzdm.com/p/123?utm_source=weixin&utm_medium=share",
            "https://www.smzdm.com/p/123",
        ),
        ("https://WWW.SMZDM.COM/p/123", "https://www.smzdm.com/p/123"),
        ("https://www.smzdm.com:443/p/123", "https://www.smzdm.com/p/123"),
        # 非追踪参数保留，且顺序无关
        (
            "https://www.smzdm.com/p/123?b=2&a=1",
            "https://www.smzdm.com/p/123?a=1&b=2",
        ),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_normalize_url_makes_tracking_variants_equal() -> None:
    """★ 这是 URL 归一化存在的意义：同一篇文章的不同分享链接必须归一成同一个。"""
    a = normalize_url("http://www.smzdm.com/p/123/")
    b = normalize_url("https://www.smzdm.com/p/123?utm_source=weixin&from=timeline")
    assert a == b


@pytest.mark.parametrize("raw", [None, "", "   ", "not a url", "://", "http://"])
def test_normalize_url_never_raises(raw: str | None) -> None:
    """★ 与 cleaner 同样的契约：不抛异常。"""
    normalize_url(raw)
