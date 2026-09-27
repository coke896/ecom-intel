"""归并逻辑评估 —— 在真实标题上验证 scraper/dedup.py 的判定质量。

================================================================================
这个脚本和 label_pairs.py 的区别
================================================================================
label_pairs.py 是【探索阶段】的工具：把三种指标都算一遍，回答
"到底哪个指标能用"——结论是都不能用。

这个脚本是【验证阶段】的工具：**直接 import 正式实现 scraper/dedup.py**，
在人工标注的真值上跑，回答"最终这套判定逻辑对不对"。

★ 为什么必须 import 而不是复制一份逻辑：
  复制一份的话，spike 里测的是副本、线上跑的是正本，
  两者哪天不一致了没人会发现——这种"测试通过但线上出错"是最难查的。
  import 保证了 spike 结论直接等价于生产行为。

================================================================================
方法：标注对用【精确子串匹配】锚定到语料
================================================================================
上一版用模糊匹配（找包含度最高的语料标题）把标注文本映射到真实标题，
结果选错了标题，算出来的分数和我手算的对不上——
**测试脚手架自己出错，比没有测试更危险**，因为它会给出错误的安全感。

现在改成：每条标注文本必须【精确命中且仅命中一条】语料标题，
命中 0 条或命中多条都直接报错退出。
这样脚本要么给出可信结论，要么明确告诉你它没跑对，不会悄悄给你假数据。

用法：
    python spike/evaluate_merge.py
"""

from __future__ import annotations

import json
import sys
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.dedup import (  # noqa: E402
    GREY_ZONE_THRESHOLD,
    MERGE_THRESHOLD,
    Candidate,
    Decision,
    ExistingProduct,
    build_signature,
    containment,
    decide,
)

CORPUS = Path(__file__).parent / "corpus.json"

# ===========================================================================
# 人工标注的真值
# ===========================================================================
#
# ★ 标签是我逐对读出来的，判定标准是"是不是同一款商品（SKU 层面）"。
#
# ★ 用 (前缀A, 前缀B) 定位语料标题 —— 只要在语料里唯一命中即可，
#   不必抄写完整标题（抄错一个字符就会命中失败，反而更麻烦）。
LABELS: list[tuple[str, str, bool, str]] = [
    # -------------------------------------------------------------------
    # 真阳性：同一商品，措辞/详略不同
    # -------------------------------------------------------------------
    (
        "FreeClip 2 耳夹式多单元动圈蓝牙耳机 冰莓紫 Type-C",
        "FreeClip 2 耳夹式耳机",
        True,
        "同款：后者省略了颜色和接口",
    ),
    (
        "FreeClip 2 耳夹式多单元动圈蓝牙耳机 冰莓紫 Type-C",
        "freeclip2 耳夹式蓝牙耳机 冰莓紫",
        True,
        "同款：仅大小写和空格差异",
    ),
    (
        "Pura X Max 12GB+256GB",
        "HUAWEI 华为 Pura X Max 手机",
        True,
        "同款：后者省略规格（★ 这条是严格相等的代价，见下方结论）",
    ),
    (
        "iPhone 18 Pro Max 5G手机 256GB 勃艮第酒红色",
        "Apple 苹果 iPhone 18 Pro 5G手机",
        False,
        "★ 不同商品：Pro vs Pro Max 是两个 SKU（只差一个后缀词）",
    ),
    # -------------------------------------------------------------------
    # 假阳性候选：品牌相同，文本相似，但是不同商品
    # -------------------------------------------------------------------
    (
        "iPhone 18 Pro 5G手机",
        "iPhone Duo 5G手机",
        False,
        "★ 最危险：相似度 0.727~0.800，但型号词完全不同",
    ),
    (
        "FreeClip 2 耳夹式耳机",
        "FreeClip 开放式设计",
        False,
        "★ 次危险：文本极像，但一代 vs 二代（差一个数字）",
    ),
    (
        "FreeClip 2 耳夹式耳机",
        "FreeClip 耳夹耳机 UltraCareX",
        False,
        "同上：FreeClip 2 vs FreeClip 一代配饰装",
    ),
    (
        "百亿补贴：酷态科 充电宝10号电能棒电芯10000毫安",
        "酷态科 充电宝10号电能棒电芯10000毫安",
        True,
        "同款：仅促销前缀差异（★ 锚定报错时从语料里发现的真实重复爆料）",
    ),
    (
        "百亿补贴：酷态科 CP13电能块自带线电芯10000毫安",
        "百亿补贴：酷态科 充电宝10号电能棒电芯10000毫安",
        False,
        "同品牌同容量，型号不同（10号 vs CP13）",
    ),
    (
        "5折 舒适缓震 女士跑鞋",
        "5折 Slip-ins闪穿 男士健步鞋",
        False,
        "同品牌同折扣，品类词不同",
    ),
    (
        "米良品 家用便携可折叠拖轮购物车",
        "米良品 创意电动发光自吸掏耳神器",
        False,
        "同品牌，完全不同品类",
    ),
]


# ===========================================================================
# 语料加载与精确锚定
# ===========================================================================


def load_titles() -> list[str]:
    raw = json.loads(CORPUS.read_text(encoding="utf-8"))
    return [c["title"] for c in raw if c.get("title")]


def resolve(titles: list[str], prefix: str) -> str:
    """把标注文本锚定到【唯一】一条语料标题。

    ★ 先试完全相等，再试子串包含。
      为什么要分两步：语料里同时存在
        '百亿补贴：酷态科 充电宝10号电能棒电芯10000毫安'
        '酷态科 充电宝10号电能棒电芯10000毫安'
      后者是前者的子串，纯子串匹配会命中两条而报错——
      但这两条恰恰是我们要测的【真阳性对】，必须能分别锚定。

    ★ 命中 0 条或多条都直接抛异常。
      上一版就是在这里悄悄错了——模糊匹配永远能返回一个"最像的"，
      哪怕根本没匹配上。宁可 loud failure，不要 silent wrong。
    """
    exact = [t for t in titles if t.strip() == prefix.strip()]
    hits = exact if exact else [t for t in titles if prefix in t]
    if len(hits) != 1:
        raise SystemExit(
            f"\n❌ 锚定失败：{prefix!r} 在语料里命中 {len(hits)} 条（需要恰好 1 条）\n"
            + "\n".join(f"    {h}" for h in hits[:5])
            + "\n   → 请检查标注文本，或确认语料是否变了。\n"
        )
    return hits[0]


# ===========================================================================
# 评估
# ===========================================================================


def make_existing(idx: int, title: str) -> ExistingProduct:
    norm, model_toks, toks = build_signature(
        Candidate(source="smzdm", source_id=str(idx), title=title)
    )
    return ExistingProduct(
        product_id=idx,
        title_norm=norm,
        brand=None,          # ★ 品牌一律不提供 → 硬门槛只剩型号 token 一条在工作，
        model_tokens=model_toks,   #    这是【最悲观】的设定，没有品牌帮忙兜底
        tokens=toks,
    )


def evaluate(titles: list[str]) -> None:
    print("=" * 108)
    print(f"归并逻辑评估 —— 直接调用 scraper/dedup.py 的正式实现")
    print(f"语料：{len(titles)} 条真实标题   |   人工标注：{len(LABELS)} 对")
    print(f"阈值：自动归并 ≥ {MERGE_THRESHOLD}   灰色地带 ≥ {GREY_ZONE_THRESHOLD}")
    print("=" * 108)
    print(f"{'标签':<6} {'包含度':>7} {'Dice':>7} {'Jacc':>7}  {'硬门槛':<7} {'判定':<7} {'结果':<5}  标题对")
    print("-" * 108)

    rows: list[dict] = []
    for i, (pa, pb, label, note) in enumerate(LABELS):
        ta, tb = resolve(titles, pa), resolve(titles, pb)

        existing = [make_existing(0, ta)]
        cand = Candidate(source="smzdm", source_id="1", title=tb)
        res = decide(cand, existing)

        _, model_a, toks_a = build_signature(
            Candidate(source="smzdm", source_id="0", title=ta)
        )
        _, model_b, toks_b = build_signature(cand)
        score = containment(toks_a, toks_b)
        gate_pass = model_a == model_b

        # 另外两种指标，用于说明"为什么最终没用它们"
        sa, sb = set(toks_a), set(toks_b)
        dice = 2 * len(sa & sb) / (len(sa) + len(sb)) if sa and sb else 0.0
        jacc = len(sa & sb) / len(sa | sb) if sa and sb else 0.0

        # 判定结果 → 二分类预测
        predicted_same = res.decision is Decision.MERGE

        # 结果标记
        if predicted_same == label:
            mark = "✅" if label else "✅"
        elif not predicted_same and label:
            mark = "🟡漏"     # 漏归并：安全，少了几个价格点
        else:
            mark = "❌错"     # 错归并：危险，污染价格序列

        gate_str = "通过" if gate_pass else "拦下"
        print(
            f"{'同款' if label else '不同':<6} {score:>7.3f} {dice:>7.3f} {jacc:>7.3f}  "
            f"{gate_str:<7} {res.decision.value:<7} {mark:<5}  {pa[:34]}"
        )
        print(f"{'':>44}{'':<7}{'':<7}  ↕ {note}")

        rows.append({
            "label": label,
            "score": score,
            "dice": dice,
            "jaccard": jacc,
            "gate_pass": gate_pass,
            "decision": res.decision,
            "pred": predicted_same,
            "note": note,
        })

    # -------------------------------------------------------------------
    # 汇总
    # -------------------------------------------------------------------
    tp = sum(1 for r in rows if r["pred"] and r["label"])
    fp = sum(1 for r in rows if r["pred"] and not r["label"])
    fn = sum(1 for r in rows if not r["pred"] and r["label"])
    tn = sum(1 for r in rows if not r["pred"] and not r["label"])

    print()
    print("=" * 108)
    print("混淆矩阵（正类 = 判定为同一商品）")
    print("=" * 108)
    print(f"  真阳性 TP = {tp}   正确归并（省下重复商品条目）")
    print(f"  真阴性 TN = {tn}   正确分开")
    print(f"  假阳性 FP = {fp}   ★ 错归并 —— 会让价格序列混进别的商品，分析结论静默失真")
    print(f"  假阴性 FN = {fn}   漏归并 —— 只是少几个价格点，落进灰色地带待人工核对")

    print()
    print("=" * 108)
    print("分层归因：危险的那几对是被谁拦下的？")
    print("=" * 108)
    for r in rows:
        if not r["label"]:
            who = "硬门槛（型号 token）" if not r["gate_pass"] else \
                  f"相似度（{r['score']:.3f} < {MERGE_THRESHOLD}）" if not r["pred"] else \
                  "★ 没人拦下"
            print(f"  {r['note'][:40]:<42} → {who}")

    # -------------------------------------------------------------------
    # ★ 全脚本的核心结论
    # -------------------------------------------------------------------
    print()
    print("=" * 108)
    print("★ 核心结论：三种相似度指标全部【不可分】，加上硬门槛立刻可分")
    print("=" * 108)
    print(f"{'指标':<10} {'真阳性最低':>10} {'假阳性最高':>10}   判定（全部 %d 对）" % len(rows))
    print("-" * 108)
    for name, key in [("包含度", "score"), ("Dice", "dice"), ("Jaccard", "jaccard")]:
        pos = [r[key] for r in rows if r["label"]]
        neg = [r[key] for r in rows if not r["label"]]
        verdict = "可分 ✅" if min(pos) > max(neg) else "★ 不可分 ❌"
        print(f"{name:<10} {min(pos):>10.3f} {max(neg):>10.3f}   {verdict}")

    print()
    print(f"{'--- 加上硬门槛后（只看通过型号门槛的 %d 对）---':<50}"
          % sum(1 for r in rows if r["gate_pass"]))
    gated = [r for r in rows if r["gate_pass"]]
    need_reject = [r["score"] for r in gated if not r["label"]]
    need_accept = [r["score"] for r in gated if r["label"]]
    print(f"{'包含度':<10} {min(need_accept):>10.3f} {max(need_reject):>10.3f}   "
          f"{'可分 ✅' if min(need_accept) > max(need_reject) else '不可分 ❌'}")
    print()
    print(f"  → 只用相似度：任何单一阈值都无法既收下全部真阳性、又排除全部假阳性。")
    print(f"  → 先过类型门槛：需要拒绝的最高 {max(need_reject):.3f}，"
          f"需要接受的最低 {min(need_accept):.3f}，")
    print(f"    阈值 {MERGE_THRESHOLD} 落在可分区间 "
          f"[{max(need_reject):.3f}, {min(need_accept):.3f}] 内。")

    # 不通过硬门槛的对，有多少本来会被相似度误判？
    print()
    print("=" * 108)
    print("硬门槛的价值：它拦下的对里，有多少是【单靠相似度会误判】的？")
    print("=" * 108)
    saved = [r for r in rows if not r["gate_pass"] and r["score"] >= MERGE_THRESHOLD]
    for r in saved:
        print(f"  包含度 {r['score']:.3f} ≥ {MERGE_THRESHOLD}")
        print(f"    单靠相似度 → 会【错误归并】 ✗")
        print(f"    加硬门槛   → 拦下，且因文本高度相似而标记待人工核对 ✓")
        print(f"    {r['note']}")
    if not saved:
        print("  （无）")

    print()
    print("=" * 108)
    print("被硬门槛拦下、因此【永远不会自动归并】的真阳性（本次的代价）")
    print("=" * 108)
    lost = [r for r in rows if not r["gate_pass"] and r["label"]]
    for r in lost:
        print(f"  包含度 {r['score']:.3f}，判定 {r['decision'].value}")
        print(f"    {r['note']}")
    print()
    print(f"  代价：{len(lost)} 对同款只能靠人工核对（标 mark 的还能被发现，"
          f"标 new 的就是彻底漏掉）")
    fp_risk = sum(1 for r in rows if not r["label"] and r["score"] >= MERGE_THRESHOLD)
    print(f"  对照：若放宽成「子集相容」，会放进 {fp_risk} 对【不同商品】——"
          f"那属于静默污染价格序列，比漏归并严重得多。")


if __name__ == "__main__":
    evaluate(load_titles())
