"""文本显示工具：按**显示宽度**把长标题截断成图例能放下的长度。

两条规则（见 docs/可行性评估.md 13.3）：① 按显示宽度而不是字符数——中文占两个
英文字符宽，`t[:14]` 在纯中文标题上实际显示 28 个字符宽；② 保留尾部而不是砍掉
——中文标题的 SKU 信息**几乎总在最后**，砍尾巴正好把最有区分度的部分切掉。

固定截断策略**无法**保证"不同 SKU 一定长得不一样"（真实标题尾部挂着营销语），
所以完整 `title` 由接口一并返回给 tooltip——那才是"信息一点没丢"的保证。
"""

from __future__ import annotations

# 判定"双宽字符"的码点下界。★ 取 0x2E80 而不是汉字起始 0x4E00：中文标题里还有
# 大量**全角标点**（，。：（）【】）和全角字母数字（１２３ＡＢ），它们肉眼同样
# 双宽、码点却更低——用 0x4E00 会低估 5~6 个宽度单位，图上还是溢出。
_DOUBLE_WIDTH_FROM = 0x2E80


def display_width(text: str) -> int:
    """文本的显示宽度：CJK 及全角标点算 2，其余算 1。"""
    return sum(2 if ord(ch) >= _DOUBLE_WIDTH_FROM else 1 for ch in text)


def _take_head(text: str, budget: int) -> str:
    """从头开始取，直到再加一个字符就会超出 budget。"""
    out: list[str] = []
    width = 0
    for ch in text:
        w = 2 if ord(ch) >= _DOUBLE_WIDTH_FROM else 1
        if width + w > budget:
            break
        out.append(ch)
        width += w
    return "".join(out)


def _take_tail(text: str, budget: int) -> str:
    """从尾开始倒着取，直到再加一个字符就会超出 budget。"""
    out: list[str] = []
    width = 0
    for ch in reversed(text):
        w = 2 if ord(ch) >= _DOUBLE_WIDTH_FROM else 1
        if width + w > budget:
            break
        out.append(ch)
        width += w
    out.reverse()
    return "".join(out)


def elide_middle(text: str, budget: int = 26, head_ratio: float = 0.6) -> str:
    """把长文本截成 `头…尾`，总显示宽度不超过 budget。

    head_ratio 默认 0.6：前面多留，因为品牌+品类承载"这是什么"，尾部只需放下
    "哪个 SKU"（通常两三个字）。
    ★ 保证：返回值宽度一定 <= budget，且不会出现"截了但没省略号"。
    """
    # 先处理退化输入。budget <= 0 返回空串而不抛异常：它跑在接口的返回路径上，
    # 一个美化标签的工具不该有能力让整个请求 500。
    if budget <= 0:
        return ""

    # ★ "放得下就原样返回" 必须排在预算判断之前，否则 budget=1 时一个宽度为 1 的
    #   字符串会被换成省略号——装得下却给了个"这里省略了东西"的假信号。
    if display_width(text) <= budget:
        return text

    avail = budget - 1  # 给省略号留 1 个单位

    # ★ 预算窄到放不下"头+尾"时，把全部预算让给尾部：原写法 head/tail 各
    #   `max(1, ...)` 保底一个字符，budget=2 时算出 1+1(省略号)+1=3 ——**超出
    #   budget**，等于假承诺。让给尾部是因为区分 SKU 的信息在尾部。
    if avail < 2:
        return "…" + _take_tail(text, avail)

    head_budget = max(1, int(avail * head_ratio))
    tail_budget = max(1, avail - head_budget)

    return _take_head(text, head_budget) + "…" + _take_tail(text, tail_budget)
