# -*- coding: utf-8 -*-
r"""归并决策普查 —— 277 条真实标题里，到底有多少对"值得人工标注"？

================================================================================
这个脚本要回答什么
================================================================================
外部评审看完了标定那一节，给了个建议：

    "花半小时人工标注补到 40~50 对，把数字从 11 换成 47，说服力上一个台阶。"

"补标注"的方向是对的，但"补到 40~50"这个数字**得先问语料答不答应**。
这句话有两种读法，差的是一整个方法论：

  · 凑数：从 38,226 对里挑 40~50 对标上，报"标注了 N 对"。
    挑法不可复现，剩下没标的那三万八千对里还有没有同款——
    不知道，而且这个问题无法回答。
  · 穷举：先定一条可复现的线，把线上的**全部**标掉，
    并且知道线上总共就这么些。
    报出去的是"值得判断的对，一对不漏"，不是"标了 N 对"。
    线怎么定？先取系统自己的灰色地带下界 0.50；再对线下扫查——
    发现同款真能跌破 0.50（"Free chip2" 讹写，0.400），标注线于是下探到 0.40。
    线的最终位置是被数据逼出来的，不是拍的。

数字从 11 涨到 47 只改变了样本量，不改变"它仍然是个抽样"这个性质；
把线上标空，才把这一问从抽样变成穷举。所以先数"值得判断的到底有几对"。

实测（见文末结论）：38,226 对里值得判断的是 123 对 = 系统线（0.50）以上的
全部 100 对 + [0.40, 0.50) 敏感性带 23 对；0.40 是实测的同款最低分。

================================================================================
两个集合，定义必须写清楚
================================================================================
【集合 A：系统问过意见的对】
    调正式实现 decide()，结论不是 NEW 的对（即 MERGE 或 MARK）。
    这是**系统自己认为**需要拿主意的对。

【集合 B：值得判断的对】★ 本脚本的主集合
    **不看系统的结论**，只看客观量：两条标题的包含度 >= LABEL_LINE（0.40）。
    0.40 不是拍的：它是这份语料里**实测的同款最低分**（"Free chip2" 讹写
    那一对，恰好 0.400；[0.28, 0.40) 的 166 对逐对扫过，无同款）。

    为什么不直接用系统的灰色地带下界 0.50：0.50 以上是"系统会看一眼"的
    区域，但同款真能跌破它——跌下去就完全沉默：门槛拦下 + 分数不到 0.75
    的对，decide() 给 NEW **且不标记**。标定里那对 "Pura X Max 长标题 vs
    短标题"（同款！0.667）就是这样被系统一声不吭放走的；敏感性带里
    还藏着两对。
    只看集合 A 会把这些"沉默的漏网"整个漏掉——所以主集合用 B，
    并且把 A 和 B 的差单独报出来。

    两条线（0.50 系统线 / 0.40 标注线）不是"两个数字讲同一件事"：
    一个决定系统行为，一个决定标注范围；差的这一档里恰好有两对同款——
    这个差本身就是结论的一部分。

================================================================================
方法
================================================================================
277 条标题两两配对：C(277,2) = 38,226 对。每对做三件事：
  1. 两个方向各调一次 decide()（顺手验证判定是否与方向无关）
  2. 算包含度
  3. 记型号 token 门槛过没过

★ 为什么 import 正式实现而不是自己重写规则：见 spike/evaluate_merge.py ——
  重写一遍等于测自己抄的副本，副本和正本分叉了没人会发现。
★ 每个方向里"库"只有对方一个商品，且 brand=None（最悲观设定：
  品牌硬门槛完全不参与）。本脚本问的是**这一对本身**像不像同一商品；
  生产场景"新爆料 vs 全库"由 tests/test_ingest.py 覆盖。
★ 标注覆盖检查直接 import evaluate_merge.LABELS —— 标签只有一份，
  连脚本都不留第二份（这正是标定过程中踩出来的教训）。

用法：
    python spike/decision_region_census.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from itertools import combinations
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))          # 为了 import evaluate_merge（同目录）

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

import evaluate_merge  # noqa: E402

CORPUS = HERE / "corpus.json"

BAND_LOW = f"<{GREY_ZONE_THRESHOLD:.2f}"
BAND_MID = f"[{GREY_ZONE_THRESHOLD:.2f},{MERGE_THRESHOLD:.2f})"
BAND_HIGH = f">={MERGE_THRESHOLD:.2f}"

# ★ 标注覆盖的线 —— 不是系统灰区线（0.50），而是**实测的同款最低分**：
#   线下扫查逮到"Free chip2"讹写那一对，恰好 0.400，系统对它完全沉默
#   （门槛过 + 分数 < 0.50 → NEW 且不标记），所以标注必须覆盖到 0.40。
#   两条线各司其职：0.50 决定系统行为，0.40 决定标注该覆盖到哪儿。
LABEL_LINE = 0.40
SCAN_FLOOR = 0.28        # 线下扫查的下界（含）
SCAN_RECORDED = 166      # [SCAN_FLOOR, LABEL_LINE) 逐对扫过的对数（2026-10-08）


def load_titles() -> list[str]:
    raw = json.loads(CORPUS.read_text(encoding="utf-8"))
    return [c["title"] for c in raw if c.get("title")]


def band_of(score: float) -> str:
    if score < GREY_ZONE_THRESHOLD:
        return BAND_LOW
    if score < MERGE_THRESHOLD:
        return BAND_MID
    return BAND_HIGH


def as_fraction(v: float) -> str:
    """0.3333 → '1/3'。这些分数值全是短分数（k/n），写成分数比四位小数好读。"""
    for d in range(2, 13):
        if abs(v * d - round(v * d)) < 1e-9:
            return f"{round(v * d)}/{d}"
    return f"{v:g}"


def decide_one_direction(existing_sig, existing_id: int, cand_title: str, cand_id: int):
    """把 existing 放进"库"、cand_title 当候选，问 decide() 一次。"""
    norm_e, model_e, toks_e = existing_sig
    existing = ExistingProduct(
        product_id=existing_id,
        title_norm=norm_e,
        brand=None,               # ★ 品牌不参与：最悲观设定（同 evaluate_merge.py）
        model_tokens=model_e,
        tokens=toks_e,
        source_id=None,           # ★ 保持 None —— decide() 里那条"不能跟自己比"
                                  #   的守卫会跳过 source_id 相同的对，
                                  #   而我们**就是要**它认真比这一对。
    )
    cand = Candidate(source="smzdm", source_id=f"c{cand_id}", title=cand_title)
    return decide(cand, [existing])


def labeled_pairs(titles: list[str]) -> dict[tuple[str, str], bool]:
    """把 evaluate_merge 的标注集解析成"语料标题对 → 是否同款"（顺序无关）。"""
    out = {}
    for pa, pb, label, _note in evaluate_merge.LABELS:
        ta, tb = evaluate_merge.resolve(titles, pa), evaluate_merge.resolve(titles, pb)
        out[tuple(sorted((ta, tb)))] = label
    return out


def main() -> None:
    titles = load_titles()
    n = len(titles)
    sigs = [
        build_signature(Candidate(source="smzdm", source_id=str(i), title=t))
        for i, t in enumerate(titles)
    ]
    total_pairs = n * (n - 1) // 2

    print("=" * 100)
    print("归并决策普查 —— 语料里的近重复候选到底有多少对")
    print("=" * 100)
    print(f"语料：{n} 条真实标题（spike/corpus.json）")
    print(f"两两配对：C({n},2) = {total_pairs:,} 对，每对跑两个方向")
    print(f"阈值：MERGE >= {MERGE_THRESHOLD}   GREY_ZONE >= {GREY_ZONE_THRESHOLD}")
    print("设定：brand=None（品牌硬门槛不参与），只有型号 token 在工作\n")

    directed = Counter()
    undirected = Counter()
    asym: list[tuple[int, int, str, str]] = []
    crosstab: Counter[tuple[bool, str]] = Counter()
    attention: list[tuple[int, int, Decision, float, bool]] = []
    scanned: list[tuple[float, int, int]] = []   # [SCAN_FLOOR, LABEL_LINE) 线下已扫段

    for i, j in combinations(range(n), 2):
        model_i, toks_i = sigs[i][1], sigs[i][2]
        model_j, toks_j = sigs[j][1], sigs[j][2]

        res_ij = decide_one_direction(sigs[i], i, titles[j], j)
        res_ji = decide_one_direction(sigs[j], j, titles[i], i)
        directed[res_ij.decision] += 1
        directed[res_ji.decision] += 1
        if res_ij.decision is not res_ji.decision:
            asym.append((i, j, res_ij.decision.value, res_ji.decision.value))

        gate_ok = model_i == model_j
        score = containment(toks_i, set(toks_j))
        undirected[res_ij.decision] += 1
        crosstab[(gate_ok, band_of(score))] += 1

        if score >= LABEL_LINE:                   # ★ 集合 B：不看系统结论（见 LABEL_LINE 注释）
            attention.append((i, j, res_ij.decision, score, gate_ok))
        elif score >= SCAN_FLOOR:                 # 线下已扫段（人工逐对看过、无同款）
            scanned.append((score, i, j))

    # ------------------------------------------------------------------
    # 1. 对称性
    # ------------------------------------------------------------------
    print("-" * 100)
    print("1. 对称性：a→b 与 b→a 的判定是否一致")
    print("-" * 100)
    if asym:
        print(f"  ★ 不对称：{len(asym)} 对（列前 10）")
        for i, j, d1, d2 in asym[:10]:
            print(f"    {d1:<6} vs {d2:<6}  {titles[i][:36]} || {titles[j][:36]}")
    else:
        print(f"  ✓ 全部 {total_pairs:,} 对两个方向结论一致（判定与方向无关）")

    # ------------------------------------------------------------------
    # 2. 判定分布
    # ------------------------------------------------------------------
    print()
    print("-" * 100)
    print("2. 系统给出的判定分布")
    print("-" * 100)
    print(f"{'判定':<8}{'有向（两个方向都数）':>22}{'无向（每对算一次）':>22}")
    for dec in (Decision.MERGE, Decision.MARK, Decision.NEW):
        print(f"{dec.value:<8}{directed[dec]:>22,}{undirected[dec]:>22,}")
    print(f"{'合计':<8}{sum(directed.values()):>22,}{sum(undirected.values()):>22,}")
    n_asked = undirected[Decision.MERGE] + undirected[Decision.MARK]
    print(f"\n  【集合 A：系统问过意见的对】MERGE + MARK = {n_asked} 对")

    # ------------------------------------------------------------------
    # 3. 交叉表
    # ------------------------------------------------------------------
    print()
    print("-" * 100)
    print("3. 交叉表：型号门槛 × 包含度（每对算一次）")
    print("-" * 100)
    print(f"{'':<16}{BAND_LOW:>16}{BAND_MID:>18}{BAND_HIGH:>16}{'小计':>12}")
    for gate_ok in (True, False):
        row = [crosstab[(gate_ok, b)] for b in (BAND_LOW, BAND_MID, BAND_HIGH)]
        label = "型号门槛通过" if gate_ok else "型号门槛拦下"
        print(f"{label:<16}{row[0]:>16,}{row[1]:>18,}{row[2]:>16,}{sum(row):>12,}")

    n_att = len(attention)
    silent = [r for r in attention if r[2] is Decision.NEW]
    in_sys_line = [r for r in attention if r[3] >= GREY_ZONE_THRESHOLD]
    print()
    print(f"  【集合 B：包含度 >= {LABEL_LINE:.2f} 的对（值得判断）】= {n_att} 对")
    print(f"    其中在系统灰区线 {GREY_ZONE_THRESHOLD:.2f} 以上（系统会看一眼）："
          f"{len(in_sys_line)} 对")
    print(f"    其中系统给出非 NEW 结论（= 集合 A）：{n_asked} 对")
    print(f"    系统给 NEW **且一声不吭**的：{len(silent)} 对  ← 只看集合 A 会漏掉这些")
    print(f"    其余 {total_pairs - n_att:,} 对包含度 < {LABEL_LINE:.2f}："
          f"[{SCAN_FLOOR:.2f}, {LABEL_LINE:.2f}) 已逐对扫过、无同款（见 3.5），")
    print(f"    更深处的对越来越无关——标'不同'不会对任何阈值产生一个比特的信息。")

    # ------------------------------------------------------------------
    # 3.5 线下扫查记录（人工）—— 标注线为什么是 0.40 而不是 0.50
    # ------------------------------------------------------------------
    print()
    print("-" * 100)
    print(f"3.5 线下扫查：[{SCAN_FLOOR:.2f}, {LABEL_LINE:.2f}) 逐对看过"
          f"（2026-10-08），同款 0 对")
    print("-" * 100)
    n_scan = len(scanned)
    assert n_scan == SCAN_RECORDED, (
        f"[{SCAN_FLOOR}, {LABEL_LINE}) 现在有 {n_scan} 对，扫查时是 {SCAN_RECORDED} 对"
        f"——语料变了，这段扫查记录要重做。"
    )
    # ★ 按原始浮点分组，不 round：round 到 4 位后 0.3333×3 ≠ 1，as_fraction 认不出来。
    #   这些分数值都是同一个除法算出来的，相同的分数值必然得到完全相同的 double。
    by_score = Counter(s for s, _i, _j in scanned)
    detail = " + ".join(f"{as_fraction(v)}×{c}"
                        for v, c in sorted(by_score.items(), reverse=True))
    print(f"  该段共 {n_scan} 对（{detail}），逐对读过，全部是不同商品")
    print(f"  （同品牌不同型号 / 同品类不同品牌 / 完全无关品类）。")
    print(f"  再往下（< {SCAN_FLOOR:.2f}）随机抽 20 对，同样无同款——")
    print(f"  不能排除更深处存在同款，但实测的同款最低分就是 0.400。")
    print(f"  → 标注线定在 {LABEL_LINE:.2f}：覆盖全部已知同款，且比系统线 "
          f"{GREY_ZONE_THRESHOLD:.2f} 低一档，")
    print(f"    把'系统沉默但值得判断'的 {n_att - len(in_sys_line)} 对也纳进来。")

    # ------------------------------------------------------------------
    # 4. 标注覆盖
    # ------------------------------------------------------------------
    lab_map = labeled_pairs(titles)
    lab = set(lab_map)
    att_set = {tuple(sorted((titles[i], titles[j]))) for i, j, *_ in attention}
    print()
    print("-" * 100)
    print("4. 标注集 vs 集合 B —— 双向必须相等（不等就当场报错）")
    print("-" * 100)
    missing = att_set - lab
    outside = lab - att_set
    # 先做去重检查：LABELS 里若有两个条目解析成同一对（锚点写岔了就是这种），
    # 集合会悄悄变小而条目数看着正常——先把这个堵上，再谈覆盖。
    assert len(lab) == len(evaluate_merge.LABELS), (
        f"LABELS 有 {len(evaluate_merge.LABELS)} 条，只解析出 {len(lab)} 个不同的对 "
        f"——有锚点指向了同一对标题（多半是锚点写岔了）。"
    )
    print(f"  标注集（spike/evaluate_merge.py::LABELS）：{len(lab)} 对")
    print(f"  集合 B（包含度 >= {LABEL_LINE:.2f}）：{len(att_set)} 对")
    print(f"  交集：{len(lab & att_set)} 对")
    # ★ 两条守门断言，而不是两句"我们标全了"的声明。
    #   "标全了"是个会过期的说法——语料一变就有了新的对，而声明不会自己长。
    #   断言会在那一刻失败，并且说清楚该往哪补。
    assert not missing, (
        f"集合 B 里有 {len(missing)} 对没进标注集 —— 语料变了或者标注漏了：\n"
        + "\n".join(f"    {a[:44]} || {b[:44]}" for a, b in sorted(missing)[:10])
        + "\n  → 逐对判定后补进 spike/evaluate_merge.py 的 LABELS。"
    )
    assert not outside, (
        f"标注集里有 {len(outside)} 对不在集合 B 里 —— "
        f"包含度 < {GREY_ZONE_THRESHOLD}，它们约束不到任何阈值：\n"
        + "\n".join(f"    {a[:44]} || {b[:44]}" for a, b in sorted(outside)[:10])
    )
    print(f"  ✓ 双向相等：标注集 = 集合 B，一对不多、一对不少")

    # ★ 标注线的两条守门断言（见 LABEL_LINE 注释）：
    #   ① 同款的分数都 >= 标注线——否则说明有同款掉在线下、标注该往下探；
    #   ② 至少有一对同款 < 系统线——否则敏感性带白标了、该把线收回 0.50。
    idx_of = {t: i for i, t in enumerate(titles)}
    true_scores = [
        containment(sigs[idx_of[ta]][2], set(sigs[idx_of[tb]][2]))
        for (ta, tb), label in lab_map.items() if label
    ]
    assert min(true_scores) >= LABEL_LINE - 1e-9, (
        f"有同款包含度 {min(true_scores):.3f} < 标注线 {LABEL_LINE} —— "
        f"标注要往下探（先扫查再补标），不能装作没看见。"
    )
    assert min(true_scores) < GREY_ZONE_THRESHOLD, (
        f"所有同款都 >= {GREY_ZONE_THRESHOLD} —— 敏感性带白标了？"
        f"若语料确实如此，把标注线收回 0.50 并删掉这条断言。"
    )
    n_low = sum(1 for s in true_scores if s < GREY_ZONE_THRESHOLD)
    print(f"  同款最低分 {min(true_scores):.3f}；其中 {n_low} 对在系统线 "
          f"{GREY_ZONE_THRESHOLD} 以下（就是敏感性带的全部意义）")

    # ------------------------------------------------------------------
    # 5. 逐对列出集合 B
    # ------------------------------------------------------------------
    print()
    print("-" * 100)
    print(f"5. 集合 B 的全部 {n_att} 对，逐对列出（按包含度降序）")
    print("-" * 100)
    lab_by_pair = lab_map                 # 上面已解析好，不重新构造
    for i, j, dec, score, gate_ok in sorted(attention, key=lambda r: -r[3]):
        key = tuple(sorted((titles[i], titles[j])))
        mark = {(True): "同款", (False): "不同", None: "未标"}[lab_by_pair.get(key)]
        print(f"  [{dec.value:<5}] 包含度 {score:.3f}  门槛{'过' if gate_ok else '拦'}  "
              f"标注={mark}  {titles[i][:40]}")
        print(f"  {'':<40}{titles[j][:40]}")

    involved = {i for i, _j, *_r in attention} | {j for _i, j, *_r in attention}
    print()
    print(f"  涉及的不同标题：{len(involved)} 条（占语料的 {len(involved) / n:.1%}）")

    # ------------------------------------------------------------------
    # 6. 结论
    # ------------------------------------------------------------------
    print()
    print("=" * 100)
    print("结论")
    print("=" * 100)
    print(f"  1. 值得人工判断的对 = 包含度 >= {LABEL_LINE:.2f} = {n_att} 对"
          f"（系统线以上的 {len(in_sys_line)} + 敏感性带 {n_att - len(in_sys_line)}），")
    print(f"     这是穷举，不是抽样。评审建议的 40~50 对是抽样口径；")
    print(f"     普查给出的是覆盖口径——报出去的是")
    print(f"     **'值得判断的对，一对不漏'**，比'标了 47 对'强，")
    print(f"     也顺手堵掉了'你挑的是不是好看的那几对'这个质疑。")
    print(f"  2. 主集合必须用 B 而不是 A：只看集合 A（系统问过意见的 {n_asked} 对），")
    print(f"     会漏掉 {len(silent)} 对静默候选（decide() 给 NEW 且不标记）。")
    print(f"     同款漏网恰恰藏在这里——包括系统线以下那两对（见 3.5）。")
    print(f"  3. 标注线 ({LABEL_LINE:.2f}) 低于系统线 ({GREY_ZONE_THRESHOLD:.2f}) 不是错位：")
    print(f"     0.40 是实测的同款最低分；[0.28, 0.40) 的 {len(scanned)} 对逐对扫过无同款。")
    print(f"     瓶颈是语料组成（近重复天然稀少：{n_att} 对只牵涉 {len(involved)} 条标题），")
    print(f"     不是标注工时。要更多带信息的对，得靠更多天真实采集——")
    print(f"     采集已因合规停止，所以这条作为已知边界写进文档。")


if __name__ == "__main__":
    main()
