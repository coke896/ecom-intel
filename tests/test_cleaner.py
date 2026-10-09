"""清洗层测试。

★ 用例取自 spike 阶段采集的真实 SMZDM 数据（543 个价格样本 + 100 条标题），
  真实数据里的格式比想象的多（如 "（需买N件，共N元）"）。
★ 浮点比较一律用 pytest.approx：直接写 == 会因二进制浮点表示问题随机失败。
"""

from __future__ import annotations

import pytest

from scraper.cleaner import (
    clean_text,
    normalize_width,
    parse_price,
    parse_sales,
    parse_timestamp,
)

# parse_price —— 单值


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # ---- 真实 SMZDM 格式（出现频次最高的四种）----
        ("199元", (199.0, 199.0)),
        ("130元（需用券）", (130.0, 130.0)),
        ("18.8元", (18.8, 18.8)),
        ("7940.25元", (7940.25, 7940.25)),
        ("7.82元（需用券）", (7.82, 7.82)),
        ("9.9元包邮", (9.9, 9.9)),
        ("29.9元（需用券）", (29.9, 29.9)),
        # ---- 带货币符号 ----
        ("￥199.00", (199.0, 199.0)),
        ("¥199", (199.0, 199.0)),
        ("￥199元", (199.0, 199.0)),
        # ---- 千分位与全角 ----
        ("1,299.00元", (1299.0, 1299.0)),
        ("１９９元", (199.0, 199.0)),
        # ---- 前面有噪声词 ----
        ("到手价 89元", (89.0, 89.0)),
    ],
)
def test_parse_price_single(raw: str, expected: tuple[float, float]) -> None:
    lo, hi = parse_price(raw)
    assert lo == pytest.approx(expected[0])
    assert hi == pytest.approx(expected[1])


# parse_price —— ★ 真实数据里的"第二个数字"陷阱：字符串里有两个数字时，
# 朴素的"取最小/最大数字"实现会静默返回错值。


@pytest.mark.parametrize(
    ("raw", "expected", "陷阱说明"),
    [
        (
            "193元（淘金币可抵0.97元起）",
            (193.0, 193.0),
            "0.97 是淘金币抵扣额。取 min 会得到 0.97，取'元起'规则会得到 (0.97, None)",
        ),
        (
            "18.5元（需买3件，共55.5元）",
            (18.5, 18.5),
            "55.5 是【3件的总价】。取最后一个数字会得到 55.5，价格虚高 3 倍",
        ),
        (
            "8.01元（需买2件，需用券）",
            (8.01, 8.01),
            "2 是件数。任何'取最小数字'的实现都会得到 2",
        ),
        (
            "42.4元（需买2件，需用券）",
            (42.4, 42.4),
            "同上，换个数值再验一遍",
        ),
        (
            "86.33元（淘金币可抵11.66元起）",
            (86.33, 86.33),
            "抵扣额比价格还大，取 max 也会出错",
        ),
    ],
)
def test_parse_price_multi_number_traps(
    raw: str, expected: tuple[float, float], 陷阱说明: str
) -> None:
    """★ 价格 = 第一个「数字+元」，不是最大/最小/最后一个数字；括号里是成交条件。"""
    lo, hi = parse_price(raw)
    assert lo == pytest.approx(expected[0]), f"失败：{陷阱说明}"
    assert hi == pytest.approx(expected[1]), f"失败：{陷阱说明}"


# parse_price —— 区间与开区间


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("券后199-299", (199.0, 299.0)),
        ("199~299", (199.0, 299.0)),
        ("199—299", (199.0, 299.0)),           # 中文破折号 U+2014
        ("199－299", (199.0, 299.0)),          # 全角连字符
        ("199至299", (199.0, 299.0)),
        ("199.00-299.00元", (199.0, 299.0)),
        ("299-199元", (199.0, 299.0)),         # 源站写反了，规范成 (小, 大)
    ],
)
def test_parse_price_range(raw: str, expected: tuple[float, float]) -> None:
    lo, hi = parse_price(raw)
    assert lo == pytest.approx(expected[0])
    assert hi == pytest.approx(expected[1])


def test_parse_price_open_range() -> None:
    """'199元起' → (199.0, None)。
    ★ 最高价必须是 None：返回 199.0 是在谎称"价格就是 199"，而实际语义是
      "只知道下界"，两者在价格分布分析里含义完全不同。"""
    lo, hi = parse_price("199元起")
    assert lo == pytest.approx(199.0)
    assert hi is None


def test_parse_price_open_range_real_frequency() -> None:
    """'N元起' 在真实数据里出现 50 次，不是罕见格式。"""
    assert parse_price("49元起") == (49.0, None)
    assert parse_price("1299元起") == (1299.0, None)


# parse_price —— 异常输入


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "\t\n",
        "--",
        "-",
        "—",
        "...",
        "？？",
        "暂无报价",          # ★ 真实数据里出现 67 次，是最常见的"无价格"
        "暂无价格",
        "价格保密",
        "面议",
        "已售罄",
        "评论打卡瓜分6万积分！",   # ★ 从真实页面抓到的促销 banner 文本
    ],
)
def test_parse_price_returns_none(raw: str | None) -> None:
    assert parse_price(raw) == (None, None)


# parse_sales


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # ---- 纯数字 ----
        ("9999", 9999),
        ("0", 0),
        ("1", 1),
        ("100000", 100000),
        # ---- 万 ----
        ("1万", 10000),
        ("1万+", 10000),          # '+' 取下限，保持单调可比
        ("已售1万+", 10000),
        ("1.2万", 12000),
        ("1.25万", 12500),
        ("10万+", 100000),
        # ---- 千 / 百 / 亿 ----
        ("3千+", 3000),
        ("5百", 500),
        ("2亿", 200000000),
        ("1.5亿", 150000000),
        # ---- 中文数字 ----
        ("一万", 10000),
        ("三千", 3000),
        ("十", 10),
        ("十万", 100000),
        ("十万+", 100000),
        ("已售十万", 100000),
        # ---- ★ 两个字的单位（这一组是后补的）----
        # ★ 原清单只有单字单位（万/千/百/亿），是照着实现的 _UNIT 键写的，
        #   漏掉了千万/百万/万亿；实测错过 1千万→1000、2百万→200、1万亿→10000。
        ("1千万", 10_000_000),
        ("3千万+", 30_000_000),
        ("1.5千万", 15_000_000),
        ("已售1千万", 10_000_000),
        ("2百万", 2_000_000),
        ("5百万", 5_000_000),
        ("1万亿", 10**12),
        # ---- 千分位与全角 ----
        ("1,234", 1234),
        ("１００", 100),
        # ---- 前后缀噪声 ----
        (" 已售 1万+ ", 10000),
        ("销量1万", 10000),
        ("月销1万+", 10000),
        ("约1万", 10000),
        ("超过1万", 10000),
        ("1万件", 10000),
    ],
)
def test_parse_sales(raw: str, expected: int) -> None:
    assert parse_sales(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "\t\n",
        "--",
        "-",
        "—",
        "暂无",
        "无",
        "暂无数据",
        "N/A",
        "n/a",
        "null",
        "None",
        "abc",
        "万",        # 只有单位没有数字
        "/",
        "...",
        "？？",
        "-1",        # 负数不是合法销量
    ],
)
def test_parse_sales_returns_none(raw: str | None) -> None:
    assert parse_sales(raw) is None


# ★ 核心承诺：永不抛异常


def test_never_raises_on_garbage() -> None:
    """清洗函数对【任何】输入都不抛异常。
    清洗跑在采集管道内部，第 501 条数据触发异常会让前面抓好的 500 条一起白费，
    所以契约是"解析不出来就返回 None"。输入刻意挑了极端值。"""
    garbage = [
        "", " ", "\x00", "\x01\x02", "\n\r\t",
        "🙂", "🀄", "​",           # 零宽字符
        "a" * 10000,
        "１２３万",
        "1..2", "1.2.3", "-", "+", "--",
        "NaN", "inf", "-inf", "Infinity", "1e999", "0x1F", "0b101",
        "9" * 400,
        "元元元", "万万万",
        "199元199元199元",
        "￥￥￥199",
        "\ud800",                        # 孤立代理对
    ]
    for g in garbage:
        # 只要能返回而不抛异常，就算通过——返回值本身不重要
        parse_sales(g)
        parse_price(g)
        clean_text(g)


def test_never_raises_on_non_string() -> None:
    """非字符串输入也不能炸：JSON 里的价格字段可能是 int / float / None，
    调用方不该为此写一堆类型判断。"""
    for v in [None, 0, 1, -1, 3.14, True, False, [], {}, (), set()]:
        parse_sales(v)   # type: ignore[arg-type]
        parse_price(v)   # type: ignore[arg-type]
        clean_text(v)    # type: ignore[arg-type]


# 文本与工具函数


def test_normalize_width() -> None:
    assert normalize_width("１９９") == "199"
    assert normalize_width("１２．５") == "12.5"
    # 全角括号【不】转换——它是标题里的语义边界，转了会改变视觉形态
    assert normalize_width("（门店有售）") == "（门店有售）"


def test_clean_text() -> None:
    assert clean_text(None) is None
    assert clean_text("") is None
    assert clean_text("   ") is None
    assert clean_text("  a  b  ") == "a b"
    assert clean_text("a\n\nb") == "a b"
    # HTML 注释是真实页面里大量存在的噪声，必须去掉
    assert clean_text("<!--<span></span>-->标题") == "标题"


def test_clean_text_returns_none_not_empty_string() -> None:
    """★ 空串和 None 必须区分开：空串会污染唯一约束（'' == ''）和指纹计算，
    None 的语义更干净——字段确实没有值。"""
    assert clean_text("<!-- 只有注释 -->") is None


# 时间戳


def test_parse_timestamp() -> None:
    """SMZDM 的 timesort 是秒级 unix 时间戳。"""
    dt = parse_timestamp(1790545506)
    assert dt is not None
    assert dt.year == 2026
    # ★ 必须是不带时区的 UTC，与 db/base.py 的 utcnow() 保持一致
    assert dt.tzinfo is None


def test_parse_timestamp_rejects_implausible() -> None:
    """毫秒时间戳要拒绝，不能静默变成公元 58000 年——不检查的话误传
    1790545506000（毫秒）会得到公元 58717 年的日期，安静写进数据库，直到前端
    画图时才崩。"""
    assert parse_timestamp(1790545506000) is None    # 毫秒
    assert parse_timestamp(0) is None
    assert parse_timestamp(-1) is None
    assert parse_timestamp(None) is None
    assert parse_timestamp("abc") is None
    assert parse_timestamp("") is None
