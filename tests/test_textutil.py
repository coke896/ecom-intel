"""显示宽度截断的测试。

这个模块看着像一个不起眼的字符串工具，但它守着一个**真实发生过的事故**：

    项目早期画 Mock 数据的 Top20 条形图，y 轴标签用了最朴素的 `t[:14]`。
    图上出现了三条看起来一模一样的「绿联 磁吸手机指环扣 金属 …」，
    销量却是 45794 / 7595 / 7022。

    第一反应是"数据重复了"。去查库才发现 Top20 里 19 个标题都是唯一的——
    那三条是**不同颜色的 SKU**，而颜色恰好写在标题的**最后两个字**，
    被按字符数的截断切掉了。**图看起来在说谎，其实是截断在说谎。**

所以下面这些测试不是为了"覆盖一个工具函数"，而是钉住那个事故的两个根因：
  · 按显示宽度算，不按字符数算
  · 保留尾部，不砍尾部
"""

from __future__ import annotations

import pytest

from app.textutil import display_width, elide_middle

# 一条真实的商品标题。后面很多用例都以它为素材，
# 因为它的结构就是中文商品标题的标准结构：
#     品牌 + 品类 + 规格 + 颜色/款式
# 而**区分 SKU 的颜色恰好在最后**——这正是截断最容易切掉的部分。
TITLE = "绿联 磁吸手机指环扣 金属 超薄 深空灰"


# ===========================================================================
# 一、宽度计算
# ===========================================================================


def test_ascii_counts_as_one() -> None:
    assert display_width("") == 0
    assert display_width("abc") == 3
    assert display_width("iPhone 15") == 9


def test_cjk_counts_as_two() -> None:
    assert display_width("中") == 2
    assert display_width("中abc") == 5
    assert display_width("充电宝") == 6


def test_full_width_punctuation_counts_as_two() -> None:
    """★ 这条专门守 `_DOUBLE_WIDTH_FROM` 那个阈值。

    如果阈值取常见的 0x4E00（汉字起始），下面这些字符会被算成 1 个单位：
      · 全角括号/顿号/书名号（0x3000 区）
      · 全角字母数字（0xFF00 区）
    而它们肉眼看就是双宽的，于是估算宽度比实际窄，标签还是会溢出图表。
    实测：一条「【限时】充电宝（白色）」会被低估 6 个单位。
    """
    assert display_width("（") == 2
    assert display_width("，") == 2
    assert display_width("【】") == 4
    assert display_width("１２３") == 6  # 全角数字
    assert display_width("ＡＢ") == 4

    # 反过来：半角括号必须是 1，不能一并算宽——
    # 那会让所有含括号的英文标签被提前截断。
    assert display_width("()") == 2
    assert display_width(",") == 1


# ===========================================================================
# 二、★ 核心回归：三个 SKU 必须保持可区分
# ===========================================================================


def test_the_three_skus_stay_distinguishable() -> None:
    """★★ 这是本文件存在的理由。

    三个只差最后两个字的标题（真实的三个颜色 SKU），
    截断之后必须仍然是三个**互不相同**的字符串。

    截断到 26 个显示单位时，三条的完整宽度是 36，都会被截。
    只要实现是"保留尾部"的，颜色就还在，三条就必然不同。
    一旦有人把实现改成 `text[:budget]`（很自然的一次"简化"），
    这条测试立刻变红。
    """
    titles = [
        "绿联 磁吸手机指环扣 金属 超薄 深空灰",
        "绿联 磁吸手机指环扣 金属 超薄 银色",
        "绿联 磁吸手机指环扣 金属 超薄 午夜黑",
    ]
    shorts = [elide_middle(t, budget=26) for t in titles]

    assert len(set(shorts)) == 3, f"截断后无法区分这三个 SKU：{shorts}"

    # 顺带证明"按字符数砍尾巴"确实会撞车——
    # 也就是当年那个 bug 的复现，让这条测试的意图一眼可见。
    assert len({t[:14] for t in titles}) == 1, (
        "如果这条断言挂了，说明标题样本被改了，"
        "三条不再是「只在尾部不同」——那上面那条测试就失去意义了"
    )


def test_color_survives_at_the_tail() -> None:
    """更直接的说法：颜色词必须出现在结果里。"""
    assert elide_middle(TITLE, budget=26).endswith("深空灰")
    # 品牌在头部，也要留住
    assert elide_middle(TITLE, budget=26).startswith("绿联")


def test_noise_in_the_middle_is_what_gets_dropped() -> None:
    """被丢掉的是中间那段——而不是头部或尾部。

    这是有意的取舍：中文商品标题里，品牌（头）回答"这是什么牌子"、
    颜色/容量（尾）回答"具体是哪一个"，
    而中间往往是"金属""超薄""便携"这类**同品牌同品类下高度重复**的描述词。
    砍中间丢的信息最少。
    """
    result = elide_middle(TITLE, budget=26)
    assert "…" in result
    assert "金属" not in result and "超薄" not in result


# ===========================================================================
# 三、预算是硬约束
# ===========================================================================

_TEXT_SAMPLES = [
    "",
    "a",
    "中",
    "iPhone 15 Pro Max 保护壳",
    TITLE,
    "Anker 安克 氮化镓充电器 120W 三口 便携 远峰蓝",
    "【限时】充电宝（20000毫安）白色",
    "ＡＢＣ全角字母测试",
    "a" * 200,
    "中" * 200,
]


@pytest.mark.parametrize("budget", [2, 3, 5, 8, 12, 16, 20, 26, 30, 40, 100])
def test_result_never_exceeds_budget(budget: int) -> None:
    """★ 调用方要能依赖这个保证来算图例宽度，不用再截一次。

    注意参数里有 2 和 3 这种"比省略号长不了多少"的极端值——
    这是最容易写崩的地方（head_budget 或 tail_budget 算成 0 或负数）。
    """
    for text in _TEXT_SAMPLES:
        out = elide_middle(text, budget=budget)
        assert display_width(out) <= budget, (
            f"budget={budget} 时 {text!r} 截出了 {out!r}（宽度 {display_width(out)}）"
        )


@pytest.mark.parametrize("budget", [2, 5, 12, 26])
def test_short_text_is_returned_unchanged(budget: int) -> None:
    """放得下就一个字都不动。★ 不能因为"反正要处理"就统一加省略号。"""
    text = "绿联"
    if display_width(text) <= budget:
        assert elide_middle(text, budget=budget) == text


def test_truncated_result_always_has_an_ellipsis() -> None:
    """★ 反过来也要成立：**没被截断就绝不能出现省略号**。

    省略号是"这里省略了东西"的信号。如果原样返回时也带省略号，
    读者会以为自己看到的是残缺信息，而这个信号一旦不可信，
    它就再也没用了。
    """
    for text in _TEXT_SAMPLES:
        for budget in (2, 5, 12, 26, 60):
            out = elide_middle(text, budget=budget)
            truncated = display_width(text) > budget
            assert ("…" in out) == truncated, (
                f"{text!r} budget={budget} → {out!r}，"
                f"截断状态({truncated})和省略号的存在与否不一致"
            )


def test_result_is_never_empty_for_a_nonempty_input() -> None:
    """再短的预算也得返回点东西。

    ★ 返回空字符串会让 ECharts 的 y 轴出现一行**没有标签**的柱子，
      看起来像渲染错误。省略号本身也是个有效信号。
    """
    for text in ("充电宝", TITLE):
        for budget in (1, 2, 3):
            assert elide_middle(text, budget=budget) != ""


def test_budget_of_zero_returns_empty_rather_than_crashing() -> None:
    """★ budget=0（或者负数的误传）必须安全返回，不能抛异常。

    这个函数在**接口返回路径上**跑，抛异常就是 500。
    一个用来美化标签的工具函数不该有能力让整个接口挂掉。
    """
    assert elide_middle(TITLE, budget=0) == ""
    assert elide_middle(TITLE, budget=-5) == ""


# ===========================================================================
# 四、确定性与幂等
# ===========================================================================


def test_same_input_same_output() -> None:
    """纯函数，反复调用结果必须一致（否则图表每次刷新标签都在变）。"""
    assert elide_middle(TITLE, budget=20) == elide_middle(TITLE, budget=20)


def test_already_elided_text_can_be_elided_again_without_growing() -> None:
    """★ 二次截断不能变长。

    现实中会发生：后端按 60 截一次给表格，前端拿到后又按 20 截一次给图例。
    如果二次截断会把结果变长（比如头尾各取一次把省略号也当内容），
    就会出现"越截越长"这种荒谬的结果。
    """
    once = elide_middle(TITLE, budget=30)
    twice = elide_middle(once, budget=30)
    assert display_width(twice) <= 30
    assert display_width(twice) <= display_width(once)
