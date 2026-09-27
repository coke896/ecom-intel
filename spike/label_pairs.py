"""输出待人工标注的标题对，并对比几种相似度指标的区分能力。

配合 calibrate_dice.py 使用：那个脚本算 Dice，这个脚本回答
"到底哪个指标能把真阳性和假阳性分开"。

用法：
    python spike/label_pairs.py          # 打印候选对
    python spike/label_pairs.py --eval   # 跑指标对比
"""

from __future__ import annotations

import json
import re
import sys
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.cleaner import normalize_width  # noqa: E402

CORPUS = Path(__file__).parent / "corpus.json"

# --- 复用 calibrate_dice 里的归一化逻辑（保持一致，不能两套）------------------
NOISE_WORDS = [
    "今日必买", "百亿补贴", "移动端", "京东自营", "天猫精选", "小编精选",
    "手慢无", "绝对值", "历史低价", "限量", "秒杀", "抢购", "预售",
    "需用券", "领券", "优惠券", "下单", "到手价", "包邮", "直降",
    "门店有售", "新品", "新款", "爆款", "热卖", "特价", "清仓",
    "补贴", "国补", "plus会员", "PLUS会员", "会员",
]
RE_PUNCT = re.compile(r"[（）()\[\]【】<>《》,，.。、;；:：!！?？\"'“”‘’/\\|~—\-+*#@&%￥$]+")


def normalize_title(t: str) -> str:
    s = normalize_width(t).lower()
    for w in NOISE_WORDS:
        s = s.replace(w.lower(), " ")
    s = RE_PUNCT.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def toks(t: str) -> set[str]:
    return set(normalize_title(t).split())


# --- 三种指标 ---------------------------------------------------------------

def dice(a: set[str], b: set[str]) -> float:
    """Dice = 2|A∩B| / (|A|+|B|)。对长度差异敏感。"""
    return 0.0 if not a or not b else 2 * len(a & b) / (len(a) + len(b))


def containment(a: set[str], b: set[str]) -> float:
    """包含度 = |A∩B| / min(|A|,|B|)。

    ★ 也叫 overlap coefficient / Szymkiewicz–Simpson 系数。
      含义是"较短的那个标题有多少比例被较长的那个覆盖了"。
      同一商品的两条爆料常常正是【包含关系】——
      一条信息少（"华为 FreeClip 2 耳夹式耳机"），
      一条信息多（"...多单元动圈蓝牙耳机 冰莓紫 Type-C"）。
      短标题的每个词几乎都出现在长标题里 → 包含度接近 1。

      而两个不同商品即使共享品牌和几个通用词，
      较短的标题里总会有长标题没有的词（"女士跑鞋" vs "男士健步鞋"）
      → 包含度明显偏低。
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard = |A∩B| / |A∪B|。保留它是为了说明为什么不用它。"""
    return 0.0 if not a or not b else len(a & b) / len(a | b)


# --- 人工标注的真值 ----------------------------------------------------------
#
# ★ 这些标签是我逐对读出来的，不是算法生成的。
#
#   判定标准："这两条爆料说的是不是同一款商品（同一 SKU 层面）"。
#   同品牌不同型号 → False（这是最关键的一类，模型最容易搞混）
#   同商品措辞不同/信息详略不同 → True
#
#   键是两条标题的归一化后的前缀，用 (id_a, id_b) 索引不方便写。
#   所以在 main 里用标题包含关系匹配。

LABELS: list[tuple[str, str, bool, str]] = [
    # ---- 真阳性：同一商品 ----
    (
        "华为 FreeClip 2 耳夹式多单元动圈蓝牙耳机 冰莓紫 Type-C",
        "华为 FreeClip 2 耳夹式耳机",
        True,
        "同款，后者省略了颜色和接口描述",
    ),
    (
        "华为 FreeClip 2 耳夹式多单元动圈蓝牙耳机 冰莓紫 Type-C",
        "华为 freeclip2 耳夹式蓝牙耳机 冰莓紫",
        True,
        "同款，仅大小写和空格差异",
    ),
    (
        "华为 Pura X Max 12GB+256GB 橄榄金小艺伴随式AI 华为大阔折叠手机",
        "HUAWEI 华为 Pura X Max 手机",
        True,
        "同款，后者省略规格",
    ),
    (
        "Apple iPhone 18 Pro Max 5G手机 256GB 勃艮第酒红色",
        "Apple 苹果 iPhone 18 Pro Max 5G手机 256GB",
        True,
        "同款，后者省略颜色",
    ),
    (
        "百亿补贴：酷态科 充电宝10号电能棒电芯10000毫安",
        "酷态科 充电宝10号电能棒电芯10000毫安",
        True,
        "同款，仅促销前缀差异",
    ),
    (
        "百亿补贴：酷态科 CP13电能块自带线电芯10000毫安",
        "酷态科 CP13电能块自带线电芯10000毫安",
        True,
        "同款，仅促销前缀差异",
    ),
    # ---- 假阳性：不同商品，但品牌/部分词重合 ----
    (
        "Apple 苹果 iPhone 18 Pro 5G手机",
        "Apple 苹果 iPhone Duo 5G手机",
        False,
        "★ 最危险的一对：型号词不同（18 Pro vs Duo）",
    ),
    (
        "斯凯奇 5折 舒适缓震 女士跑鞋",
        "斯凯奇 5折 Slip-ins闪穿 男士健步鞋",
        False,
        "同品牌同折扣，品类词不同",
    ),
    (
        "百亿补贴：酷态科 充电宝10号电能棒电芯10000毫安",
        "百亿补贴：酷态科 CP13电能块自带线电芯10000毫安",
        False,
        "同品牌同容量，型号完全不同（10号 vs CP13）",
    ),
    (
        "米良品 家用便携可折叠拖轮购物车",
        "米良品 创意电动发光自吸掏耳神器",
        False,
        "同品牌，完全不同品类",
    ),
    (
        "米良品 家用多档位桌面吸尘清洁神器",
        "米良品 创意电动发光自吸掏耳神器",
        False,
        "同品牌，完全不同品类",
    ),
]


def _find_pair(corpus: list[str], a: str, b: str) -> tuple[str, str] | None:
    """在语料里找到与给定文案最接近的两条真实标题。"""
    def best(target: str) -> str | None:
        tn = normalize_title(target)
        tt = set(tn.split())
        best_t, best_s = None, -1.0
        for c in corpus:
            cn = normalize_title(c)
            if not cn:
                continue
            s = containment(tt, set(cn.split()))
            if s > best_s:
                best_s, best_t = s, c
        return best_t

    ra, rb = best(a), best(b)
    if ra and rb and ra != rb:
        return ra, rb
    return None


def main() -> None:
    corpus_raw = json.loads(CORPUS.read_text(encoding="utf-8"))
    titles = [c["title"] for c in corpus_raw if c.get("title")]
    print(f"语料：{len(titles)} 条标题\n")

    if "--eval" in sys.argv:
        evaluate(titles)
        return

    prepared = [(t, toks(t)) for t in titles]
    rows = []
    for (ta, sa), (tb, sb) in combinations(prepared, 2):
        if not sa or not sb:
            continue
        d, c_, j = dice(sa, sb), containment(sa, sb), jaccard(sa, sb)
        if d >= 0.45 or c_ >= 0.60:
            rows.append((c_, d, j, ta, tb))
    rows.sort(key=lambda r: -r[0])
    print(f"候选对：{len(rows)}\n")
    print(f"{'包含度':>7} {'Dice':>7} {'Jacc':>7}  标题对")
    print("-" * 110)
    for c_, d, j, ta, tb in rows[:45]:
        print(f"{c_:>7.3f} {d:>7.3f} {j:>7.3f}  {ta[:44]}")
        print(f"{'':>23}  {tb[:44]}")
    return


def evaluate(titles: list[str]) -> None:
    """★ 核心产出：对比三种指标在人工标注真值上的区分能力。"""
    print("=" * 96)
    print("指标对比（在人工标注的 %d 对真实标题上）" % len(LABELS))
    print("=" * 96)
    print(f"{'标签':<6} {'包含度':>8} {'Dice':>8} {'Jaccard':>8}  说明")
    print("-" * 96)

    rows: list[tuple[bool, float, float, float, str]] = []
    for a, b, label, note in LABELS:
        found = _find_pair(titles, a, b)
        if not found:
            print(f"  ⚠️ 语料里找不到匹配对：{a[:30]} / {b[:30]}")
            continue
        ra, rb = found
        sa, sb = toks(ra), toks(rb)
        c_, d, j = containment(sa, sb), dice(sa, sb), jaccard(sa, sb)
        tag = "同款" if label else "不同"
        print(f"{tag:<6} {c_:>8.3f} {d:>8.3f} {j:>8.3f}  {note}")
        rows.append((label, c_, d, j, note))

    print()
    for name, idx in [("包含度", 1), ("Dice", 2), ("Jaccard", 3)]:
        pos = [r[idx] for r in rows if r[0]]
        neg = [r[idx] for r in rows if not r[0]]
        if not pos or not neg:
            continue
        print(f"--- {name} ---")
        print(f"  真阳性（同款）最低分：{min(pos):.3f}")
        print(f"  假阳性（不同）最高分：{max(neg):.3f}")
        gap = min(pos) - max(neg)
        if gap > 0:
            print(f"  ✅ 可分：存在阈值区间 [{max(neg):.3f}, {min(pos):.3f}]，取中点 {(max(neg)+min(pos))/2:.3f}")
        else:
            print(f"  ❌ 【不可分】：真阳性最低分 {min(pos):.3f} < 假阳性最高分 {max(neg):.3f}")
            print(f"     任何单一阈值都无法既收下全部真阳性、又排除全部假阳性。")
        print()


if __name__ == "__main__":
    main()
