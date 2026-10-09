"""字段清洗：把源站的脏字符串解析成结构化数据。

核心承诺：**对任何输入都不抛异常。** 清洗跑在采集流程内部，一条脏数据
抛异常会让整轮采集挂掉；所以解析不出来就返回 None，由上层决定
（跳过 / 记 NULL / 进人工核对队列）。

刻意不用 pandas：清洗是逐条字符串解析、向量化用不上，却要背 30MB 依赖和
pandas 3.0 的 dtype 语义变更（字符串列 dtype 是 str、None 被转成 NaN）。
pandas 只出现在 app/api/export.py，纯函数签名也让参数化测试干净得多。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

# --- 全角 → 半角 ---

# 中文电商页面里全角数字很常见（"１９９元"），不归一化的话正则匹配不到。
# 用 str.translate 一次建表。
_WIDTH_TABLE = str.maketrans(
    "０１２３４５６７８９．，",  # 全角
    "0123456789.,",            # 半角
)


def normalize_width(text: str) -> str:
    """全角数字/标点转半角。

    只转数字和数值相关的标点：不转全角字母/括号——标题里的「（门店有售）」
    是有意义的语义边界，而这个函数也会用在标题上。
    """
    return text.translate(_WIDTH_TABLE)


# --- 数字 ---

# 支持千分位（1,299.00）和小数（29.9）。★ 千分位必须支持：朴素正则
# \d+\.?\d* 会把 "1,234" 匹配成 "1"——差三个数量级，而且不报错。
_NUM = r"\d+(?:,\d{3})*(?:\.\d+)?"


def _to_float(raw: str) -> float | None:
    """把匹配到的数字串转成 float，去掉千分位逗号。

    用 Decimal 中转：它对超长数字不会抛 OverflowError，能兜住更多边界情况；
    转完立刻换回 float，不把 Decimal 带出这个函数。
    """
    try:
        return float(Decimal(raw.replace(",", "")))
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


# --- 价格 ---

# 区间分隔符。中文破折号 "—"（U+2014）和连字符 "-" 是不同字符，
# 真实页面两种都有，否则 "199—299" 会被当成两个独立数字。
_RANGE_SEP = r"[-~—－–至]"

# 带单位的区间：199-299元 / 199~299元 / 199—299元
_RE_RANGE_YUAN = re.compile(
    rf"({_NUM})\s*{_RANGE_SEP}\s*({_NUM})\s*(?:元|块)"
)

# 不带单位的区间："券后199-299"。没有「元」做锚点、歧义更高（"2-3件"也像），
# 所以优先级排在带单位的规则之后。
_RE_RANGE_BARE = re.compile(rf"({_NUM})\s*{_RANGE_SEP}\s*({_NUM})")

# ★ 核心规则：价格 = 【最早出现的】「数字 + 元」——真实数据括号里常有干扰项
#   （'193元（淘金币可抵0.97元起）' 的 0.97 是抵扣额），取最早匹配才不会拿错。
#   优先级：带单位区间 → 单值（最早）→ 无单位区间 → 货币符号；"起" 是第 2 档的后缀判断。
_RE_PRICE_YUAN = re.compile(rf"({_NUM})\s*元")

# 「数字+元」紧跟一个"起"字 → 开区间，最高价未知
_RE_TRAILING_QI = re.compile(r"^\s*起")

# 带货币符号但不带「元」的写法：￥199 / ¥199.5
_RE_PRICE_SYMBOL = re.compile(rf"[￥¥]\s*({_NUM})")

# 命中这些词说明不是价格，是占位符/说明文字。提前返回 None，
# 避免被后面的数字规则误伤（比如 "价格待定 3 件起售" 里的 3）。
_NO_PRICE_WORDS = (
    "暂无报价", "暂无价格", "暂无", "价格保密", "面议", "待定",
    "已售罄", "售罄", "下架",
)


def parse_price(raw: str | None) -> tuple[float | None, float | None]:
    """解析价格字符串，返回 (最低价, 最高价)。

    单值时两者相同；开区间（"199元起"）时最高价为 None。
    ★ 返回二元组是定死的接口（SMZDM 的价格可能是区间，"券后199-299"）：
      products.price_max 列按它设计，从一开始就存在。
    """
    # ★ 兜底 try/except 是刻意的：契约是"永不抛异常"，调用方（采集管道）信任它。
    #   test_never_raises_on_garbage 守着这条契约。
    try:
        if raw is None:
            return (None, None)

        text = normalize_width(str(raw)).strip()
        if not text:
            return (None, None)

        # 占位符先排除，避免被后续的数字规则误伤
        if any(w in text for w in _NO_PRICE_WORDS):
            return (None, None)

        # 优先级 1：带单位的区间（"199-299元"）。最明确，先判。
        m = _RE_RANGE_YUAN.search(text)
        if m:
            lo, hi = _to_float(m.group(1)), _to_float(m.group(2))
            if lo is not None and hi is not None:
                # 容忍源站把区间写反（"299-199"）：不纠正的话，
                # 前端画价格分布图时会出现负宽度。
                return (min(lo, hi), max(lo, hi))

        # 优先级 2：单个「数字+元」，取【最早】出现的那一个。
        m = _RE_PRICE_YUAN.search(text)
        if m:
            v = _to_float(m.group(1))
            if v is not None:
                # 匹配位置后紧跟"起" → 开区间：(199.0, None) 表示
                # "只知道下界"，而不是谎称"价格就是 199"。
                if _RE_TRAILING_QI.match(text[m.end():]):
                    return (v, None)
                return (v, v)

        # 优先级 3：不带单位的区间（"券后199-299"）。排在带单位规则之后，
        # 避免 "2-3件 199元" 里的 "2-3" 被当成价格。
        m = _RE_RANGE_BARE.search(text)
        if m:
            lo, hi = _to_float(m.group(1)), _to_float(m.group(2))
            if lo is not None and hi is not None:
                return (min(lo, hi), max(lo, hi))

        # 优先级 4：货币符号写法（"￥199"）。放最后，让 "￥199元" 先被「元」规则命中。
        m = _RE_PRICE_SYMBOL.search(text)
        if m:
            v = _to_float(m.group(1))
            if v is not None:
                return (v, v)

        return (None, None)
    except Exception:
        # 连兜底都兜不住的极端情况，仍然遵守契约返回 None
        return (None, None)


# --- 销量 ---

# 中文数量单位。★ 顺序必须长单位在前（"万亿" 在 "万" 前），
# 否则交替匹配时会被短单位抢先。
_UNIT = {
    "万亿": 10**12,
    "亿": 10**8,
    "千万": 10**7,
    "百万": 10**6,
    "万": 10**4,
    "千": 10**3,
    "百": 10**2,
}

# 中文数字。"一万" / "三千" 这类写法在商品标题里偶尔出现。
# ★ "十" 必须在表里：正则的字符类认它，但表里没有它时，
#   兜底会把 "十万" 算成 1×10^4（应为 10×10^4）——差 10 倍且不报错。
_CN_DIGITS = {
    "零": 0, "十": 10, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}

# 销量数字：阿拉伯数字（可带千分位和小数）或中文数字。
# ★ 千分位必须支持："1,234" 用朴素 \d+ 会匹配成 "1"——差三个数量级且不报错。
_SALES_NUM = r"\d+(?:,\d{3})*(?:\.\d+)?|[零一二两三四五六七八九十百千万亿]+"

# 单位必须用**交替**写，不能写成字符类：字符类每个字符只吃一个字符，
# 两位的单位（万亿/千万/百万）永远匹配不到——"1万亿" 会被算成 1×10^4
# 而不是 10^12，差 10^8 倍且不报错。交替顺序即 _UNIT 的插入顺序（长单位在前）。
_UNIT_ALT = "|".join(re.escape(u) for u in _UNIT)

# 负号。销量不可能是负数，命中说明这是别的字段混进来了。
_RE_MINUS = re.compile(r"[-−–—]")


def _cn_to_int(text: str) -> int | None:
    """中文数字转整数。只处理商品场景会出现的简单写法。

    ★ 不做完整的汉语数字解析（"一千二百三十四万…" 的嵌套结构不划算），
      覆盖的是电商文案里真正会出现的："一万"、"三千"、"五万"。
    """
    if not text:
        return None

    # 纯中文数字直接查表
    if len(text) == 1:
        return _CN_DIGITS.get(text)

    total = 0
    for unit_str, unit_val in _UNIT.items():
        if unit_str in text:
            head = text.split(unit_str)[0]
            mul = _CN_DIGITS.get(head) if len(head) == 1 else None
            if mul is None:
                # "十万" 这种没有数字前缀的写法，按 1 处理
                mul = 1
            total += mul * unit_val
            text = text.split(unit_str, 1)[1]
            break

    # 处理单位之后的零头："一万五" → 10000 + 5000
    # （口语里"一万五"是 15000 不是 10005）
    if text and total:
        if len(text) == 1 and text in _CN_DIGITS:
            # 判断数量级：跟着"万"的单个数字是千位
            return total + _CN_DIGITS[text] * (total // 10)
    if total:
        return total
    return None


def parse_sales(raw: str | None) -> int | None:
    """解析销量字符串，返回整数。解析不出来返回 None。

    三个拍过板的语义（都有测试守着）：'约1万'→10000（模糊词仍含有效数值）、
    '1万+'→10000（'+' 取下限，保持单调可比）、'1,234'→1234（千分位合法）。
    当前源用不到（真实电商销量不在网页端公开），保留给 Mock 与未来数据源。
    """
    try:
        if raw is None:
            return None

        text = normalize_width(str(raw)).strip()
        if not text:
            return None

        # 噪声词要在提取数字【之前】判定：否则 "无销量" 的 "无" 会被当成中文数字。
        if any(w in text for w in ("暂无", "无", "面议", "保密", "N/A", "n/a", "null", "None")):
            return None

        # ★ 找「数字 + 可选中文字单位」的组合。单位必须在数字之后才算数，
        # 否则 "万" 单独出现会误判；单位部分用交替，理由见 _UNIT_ALT。
        m = re.search(rf"({_SALES_NUM})\s*({_UNIT_ALT})?", text)
        if not m:
            return None

        # ★ 负号检查：不做的话 "-1" 会匹配到中间的 "1"，返回一个看起来合理的
        # 错误值；只查匹配位置前一个字符，整串查找会误杀 "已售1万-2万"。
        if m.start() > 0 and _RE_MINUS.match(text[m.start() - 1]):
            return None

        num_str, unit_str = m.group(1), m.group(2)

        # 阿拉伯数字分支
        if re.match(r"^\d", num_str):
            value = _to_float(num_str)
            if value is None:
                return None
            if unit_str:
                value *= _UNIT[unit_str]
            return int(value)

        # 中文数字分支
        if re.fullmatch(r"[零一二两三四五六七八九十百千万亿]+", num_str):
            return _cn_to_int(num_str)

        return None
    except Exception:
        return None


# --- 文本 & 时间 ---

_RE_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_RE_WHITESPACE = re.compile(r"\s+")


def clean_text(raw: str | None) -> str | None:
    """清洗文本字段：去 HTML 注释、压缩空白、去首尾空格。

    返回 None 而不是空串：「字段缺失」和「字段为空」在数据层因此可区分，
    空串还会污染唯一约束和指纹计算。
    """
    if raw is None:
        return None
    text = _RE_HTML_COMMENT.sub("", str(raw))
    text = _RE_WHITESPACE.sub(" ", text).strip()
    return text or None


def parse_timestamp(raw: str | int | None) -> datetime | None:
    """把源站的 unix 时间戳转成本项目的 UTC datetime（不带时区，与 utcnow() 一致）。

    ★ 混用带不带时区的值会让 SQLite 的字符串比较排错序（见 db/base.py）。
      SMZDM 的 timesort 是秒级 unix 时间戳（如 1790545506）。
    """
    if raw is None:
        return None
    try:
        ts = int(str(raw).strip())
        # 合理性检查：范围约 2001-09-09 ~ 2033-05-18。不做的话，误传毫秒
        # 时间戳（1790545506000）会得到公元 58000 年的日期，然后静默写进数据库。
        if not (10**9 <= ts <= 2 * 10**9):
            return None
        return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError, OSError, OverflowError):
        return None
