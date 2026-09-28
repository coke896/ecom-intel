"""字段清洗：把源站的脏字符串解析成结构化数据。

================================================================================
核心承诺：**对任何输入都不抛异常。**
================================================================================

这不是"防御性编程"的教条，而是这个模块在管道里的位置决定的：
它跑在采集流程内部，一条脏数据抛异常会让整个采集任务挂掉——
抓了 500 条，第 501 条有个奇怪的字符，前 500 条也白抓了。

所以设计原则是：**解析不出来就返回 None，让上层决定怎么处理**
（跳过、记 NULL、进人工核对队列），而不是把异常抛出去。

--------------------------------------------------------------------------------
为什么不用 pandas
--------------------------------------------------------------------------------
网上同类项目的清洗层基本都是 `pd.read_html` + `df['price'].str.extract(...)`。
本项目刻意不用，有三个理由：

1. **pandas 3.0 的 dtype 语义变了**：字符串列 dtype 是 `str` 而不是 `object`
   （`s.dtype == 'object'` 恒为 False）；`None` 被强制转成 `NaN`；
   字符串列塞 float 直接 `TypeError`。网上教程全是 2.x 写法，会一路踩雷。

2. **清洗是逐条字符串解析，向量化的优势完全用不上**，
   却要背上 30MB 依赖和上面那一整套 dtype 语义。

3. **纯函数好测**：`parse_price("29.9元（需用券）") -> (29.9, 29.9)`
   这种签名，pytest 参数化写起来干净十倍，也不需要在测试里造 DataFrame。

pandas 在本项目只出现在 `app/api/export.py`（导出 Excel 时确实需要它）。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

# ---------------------------------------------------------------------------
# 全角 → 半角
# ---------------------------------------------------------------------------

# 中文电商页面里全角数字很常见（"１９９元"），不归一化的话正则匹配不到。
# 用 str.translate 一次建表，比逐字符 replace 快，也更清晰。
_WIDTH_TABLE = str.maketrans(
    "０１２３４５６７８９．，",  # 全角
    "0123456789.,",            # 半角
)


def normalize_width(text: str) -> str:
    """全角数字/标点转半角。

    只转数字和数值相关的标点。★ 不转全角字母、全角括号——
    因为标题里的全角括号「（门店有售）」是有意义的语义边界，
    转成半角会改变标题的视觉形态，而这个函数也会用在标题上。
    """
    return text.translate(_WIDTH_TABLE)


# ---------------------------------------------------------------------------
# 数字
# ---------------------------------------------------------------------------

# 支持千分位（1,299.00）和小数（29.9）。
# ★ 千分位必须支持："1,234" 用朴素正则 \d+\.?\d* 会匹配到 "1"，
#   于是 1234 元被解析成 1 元——差三个数量级，而且不报错。
_NUM = r"\d+(?:,\d{3})*(?:\.\d+)?"


def _to_float(raw: str) -> float | None:
    """把匹配到的数字串转成 float，去掉千分位逗号。

    用 Decimal 中转而不是直接 float()：
    直接 float("0.1") 这类转换本身没问题，但 Decimal 对超长数字
    （比如 "1e999" 之外的 "9"*400）不会抛 OverflowError，
    能兜住更多边界情况。转完立刻换回 float，不把 Decimal 带出这个函数。
    """
    try:
        return float(Decimal(raw.replace(",", "")))
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


# ---------------------------------------------------------------------------
# 价格
# ---------------------------------------------------------------------------

# 区间分隔符。★ 中文破折号 "—"（U+2014）和连字符 "-" 是不同字符，
# 真实页面两种都有，必须都覆盖，否则 "199—299" 会被当成两个独立数字。
_RANGE_SEP = r"[-~—－–至]"

# 带单位的区间：199-299元 / 199~299元 / 199—299元
_RE_RANGE_YUAN = re.compile(
    rf"({_NUM})\s*{_RANGE_SEP}\s*({_NUM})\s*(?:元|块)"
)

# 不带单位的区间："券后199-299"。
# ★ 这条没有「元」做锚点，ambiguity 更高（"2-3件" 也长得像），
#   所以优先级排在带单位的规则【之后】。
_RE_RANGE_BARE = re.compile(rf"({_NUM})\s*{_RANGE_SEP}\s*({_NUM})")

# ★ 核心规则：价格 = 【最早出现的】「数字 + 元」。
#
# 为什么必须是"最早出现的"而不是"任意一个"：
# 真实数据里有大量「数字 + 元」出现在括号内的干扰项：
#
#   '193元（淘金币可抵0.97元起）'   ← 0.97 是抵扣额，不是价格
#   '18.5元（需买3件，共55.5元）'   ← 55.5 是 3 件的总价，不是单价
#   '8.01元（需买2件，需用券）'      ← 2 是件数
#
# 如果写成"提取所有数字取最小/最大"，上面第一条会得到
# (0.97, 193) 这个荒谬的区间。
#
# ★ 这里踩过一个坑：最初的实现把"开区间规则"（X元起）放在单值规则之前，
#   结果 '193元（淘金币可抵0.97元起）' 被 "0.97元起" 抢先匹配，
#   返回了 (0.97, None)。
#   正确做法是【按优先级分流 + 每档取最早匹配】：
#     1. 带单位的区间（最明确）
#     2. 单个「数字+元」（取最早）
#     3. 不带单位的区间
#     4. 货币符号
#   "起" 不是独立的一档，而是第 2 档匹配到之后的一个后缀判断。
_RE_PRICE_YUAN = re.compile(rf"({_NUM})\s*元")

# 「数字+元」紧跟一个"起"字 → 开区间，最高价未知
_RE_TRAILING_QI = re.compile(r"^\s*起")

# 带货币符号但不带「元」的写法：￥199 / ¥199.5
_RE_PRICE_SYMBOL = re.compile(rf"[￥¥]\s*({_NUM})")

# 命中这些词说明这条根本不是价格，是占位符或说明文字。
# 提前返回 None，避免被后面的数字规则误伤
# （比如 "价格待定 3 件起售" 里的 3）。
_NO_PRICE_WORDS = (
    "暂无报价", "暂无价格", "暂无", "价格保密", "面议", "待定",
    "已售罄", "售罄", "下架",
)


def parse_price(raw: str | None) -> tuple[float | None, float | None]:
    """解析价格字符串，返回 (最低价, 最高价)。

    单值时两者相同；开区间（"199元起"）时最高价为 None。

    ★ 返回二元组而不是单值，是本项目一开始就定下的接口：
      SMZDM 的爆料价格可能是区间（"券后199-299"），
      如果只返回一个数，products 表就只有一个 price 列，
      等到阶段③ 写 prompt 时才发现价格有区间，就要改表 + 改 prompt + 重跑。
      现在定成二元组，products.price_max 列从一开始就存在。

    真实数据（SMZDM 精选页 + 类目页，共 543 个样本）的实际分布：
        'N元'                     217   普通价格
        'N元（需用券）'            171   券后价
        '暂无报价'                 67   脏数据 → (None, None)
        'N元起'                    50   开区间 → (N, None)
        'N元（需买N件，需用券）'     20   件数干扰
        'N元（淘金币可抵N元起）'     17   抵扣额干扰
        '评论打卡瓜分N万积分！'       1   促销 banner 文本 → (None, None)

    >>> parse_price("29.9元（需用券）")
    (29.9, 29.9)
    >>> parse_price("18.5元（需买3件，共55.5元）")
    (18.5, 18.5)
    >>> parse_price("199元起")
    (199.0, None)
    >>> parse_price("券后199-299")
    (199.0, 299.0)
    >>> parse_price("暂无报价")
    (None, None)
    """
    # ★ 兜底 try/except 是刻意的，不是偷懒。
    #   这个函数的契约是"永不抛异常"，而调用方（采集管道）会信任这个契约。
    #   与其让某个没想到的输入把整批采集搞挂，不如在这里兜住。
    #   下面的测试用例里有专门的 test_never_raises_on_garbage 守着这条契约。
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
                # 容忍源站把区间写反（"299-199"），规范成 (小, 大)。
                # 不纠正的话，前端画价格分布图时会出现负宽度。
                return (min(lo, hi), max(lo, hi))

        # 优先级 2：单个「数字+元」，取【最早】出现的那一个。
        m = _RE_PRICE_YUAN.search(text)
        if m:
            v = _to_float(m.group(1))
            if v is not None:
                # 匹配位置之后紧跟"起" → 开区间。
                # 例如 "199元起" → (199.0, None)，
                # 表示"只知道下界"，而不是谎称"价格就是 199"。
                if _RE_TRAILING_QI.match(text[m.end():]):
                    return (v, None)
                return (v, v)

        # 优先级 3：不带单位的区间（"券后199-299"）。
        # 排在带单位规则之后，避免 "2-3件 199元" 里的 "2-3" 被当成价格。
        m = _RE_RANGE_BARE.search(text)
        if m:
            lo, hi = _to_float(m.group(1)), _to_float(m.group(2))
            if lo is not None and hi is not None:
                return (min(lo, hi), max(lo, hi))

        # 优先级 4：货币符号写法（"￥199"，没有"元"字）。
        # 放在最后是因为 "￥199元" 这种冗余写法应该让「元」规则先命中。
        m = _RE_PRICE_SYMBOL.search(text)
        if m:
            v = _to_float(m.group(1))
            if v is not None:
                return (v, v)

        return (None, None)
    except Exception:
        # 连兜底都兜不住的极端情况，仍然遵守契约返回 None
        return (None, None)


# ---------------------------------------------------------------------------
# 销量
# ---------------------------------------------------------------------------

# 中文数量单位。★ 注意顺序：必须「万亿」在前、「万」在后，
# 否则交替匹配时 "亿" 会被 "万" 抢先，或者 "万亿" 只匹配到 "万"。
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
#
# ★ "十" 是补上的。它不在这里的时候，下面正则里的中文数字分支
#   （[零一二两三四五六七八九十百千万亿]+）**照样会接受它**，
#   然后 _cn_to_int 走到"没有数字前缀就按 1 处理"那条兜底去，
#   于是 "十万" 被算成 1×10^4 = 10000，而正确答案是 10×10^4 = 100000。
#   差 10 倍，不报错，看起来完全合理。
#   ★ 这类缺口的特点是：**喂进来的正则认它，处理它的表里没有它**——
#     两边对"合法输入"的定义不一致，而中间那段兜底会把差异悄悄吃掉。
_CN_DIGITS = {
    "零": 0, "十": 10, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}

# 销量数字：阿拉伯数字（可带千分位和小数）或中文数字。
# ★ 千分位必须支持："1,234" 用朴素的 \d+ 会匹配到 "1"，
#   于是 1234 的销量被解析成 1——差三个数量级，而且不报错。
_SALES_NUM = r"\d+(?:,\d{3})*(?:\.\d+)?|[零一二两三四五六七八九十百千万亿]+"

# 单位要用**交替**写，不能写成字符类。
#
# ★ 这里原来写的是 `[{''.join(_UNIT)}]?`——一个字符类，展开成 [万亿千万百万万千百]。
#   字符类里每个字符是**平等的**，而且只吃掉**一个**字符，于是 _UNIT 里
#   那两个字的键（万亿 / 千万 / 百万）**永远匹配不到**：
#
#       "1千万"  按字符类只吃到 "千" → 1×10^3   = 1000     （应为 10^7）
#       "2百万"  只吃到 "百"        → 2×10^2   = 200      （应为 2×10^6）
#       "1万亿"  只吃到 "万"        → 1×10^4   = 10000    （应为 10^12）
#
#   差 10^4 到 10^8 倍，而且**不报错**——解析出来的还是个正整数。
#   更讽刺的是：上面 _UNIT 那段注释专门解释了"必须万亿在前、万在后，
#   否则交替匹配时会被抢先"——**那段话描述的正是交替，而代码写的是字符类**。
#   注释比代码多承诺了一件事，于是没人再去看代码。
#
#   交替的顺序就是 _UNIT 的插入顺序（长单位在前），正是那段注释要的。
_UNIT_ALT = "|".join(re.escape(u) for u in _UNIT)

# 负号。销量不可能是负数，命中说明这是别的字段混进来了。
_RE_MINUS = re.compile(r"[-−–—]")


def _cn_to_int(text: str) -> int | None:
    """中文数字转整数。只处理商品场景会出现的简单写法。

    ★ 不做完整的汉语数字解析（那要处理"一千二百三十四万五千六百七十八"
      这种嵌套结构，代码量不划算）。这里覆盖的是实际电商文案里
      真正会出现的："一万"、"三千"、"五万"。

    >>> _cn_to_int("一万")
    10000
    >>> _cn_to_int("三千")
    3000
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

    真实电商的销量在网页端不公开，所以这个函数在当前数据源上
    实际用不到——保留它是为了 Mock 数据生成和未来的数据源扩展，
    也因为"已售1万+"这类脏数据的解析是面试常问的点。

    三个需要拍板的语义（都有测试守着）：
        '约1万'   → 10000   模糊词仍含有效数值，丢掉会损失数据
        '1万+'    → 10000   '+' 取下限，保持单调可比
        '1,234'   → 1234    千分位是合法格式

    >>> parse_sales("已售1万+")
    10000
    >>> parse_sales("1.2万")
    12000
    >>> parse_sales("暂无数据")
    """
    try:
        if raw is None:
            return None

        text = normalize_width(str(raw)).strip()
        if not text:
            return None

        # 先去噪声词。注意 "暂无" 这类词要在提取数字【之前】判定，
        # 否则 "暂无数据" 里没数字还好，但 "无销量" 的 "无" 会被当成中文数字。
        if any(w in text for w in ("暂无", "无", "面议", "保密", "N/A", "n/a", "null", "None")):
            return None

        # ★ 找「数字 + 可选中文字单位」的组合。
        #   单位必须在数字【之后】才算数，否则 "万" 单独出现（如 "万"）会误判。
        #   单位部分是交替而不是字符类，理由见 _UNIT_ALT 的注释。
        m = re.search(rf"({_SALES_NUM})\s*({_UNIT_ALT})?", text)
        if not m:
            return None

        # ★ 负号检查：销量不可能是负数。
        #   不做这个检查的话，"-1" 会被正则匹配到中间的 "1"，
        #   于是返回 1——一个错误的、看起来合理的数字。
        #   检查匹配位置【前面】的字符，而不是整串里有没有负号，
        #   否则 "已售1万-2万" 这种区间写法会被误杀。
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


# ---------------------------------------------------------------------------
# 文本 & 时间
# ---------------------------------------------------------------------------

_RE_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_RE_WHITESPACE = re.compile(r"\s+")


def clean_text(raw: str | None) -> str | None:
    """清洗文本字段：去 HTML 注释、压缩空白、去首尾空格。

    返回 None 而不是空串，是为了让「字段缺失」和「字段为空」在数据层可区分。
    空串会污染唯一约束和指纹计算（'' 和 '' 相等，None 和 None 不等），
    None 语义更干净。
    """
    if raw is None:
        return None
    text = _RE_HTML_COMMENT.sub("", str(raw))
    text = _RE_WHITESPACE.sub(" ", text).strip()
    return text or None


def parse_timestamp(raw: str | int | None) -> datetime | None:
    """把源站的 unix 时间戳转成本项目的 UTC datetime。

    ★ 返回不带时区的 UTC，与 db/base.py 的 utcnow() 保持一致。
      混用带时区和不带时区的值会让 SQLite 的字符串比较排错序，
      具体原因见 db/base.py 里 utcnow() 的注释。

    SMZDM 的 timesort 是秒级 unix 时间戳（如 1790545506）。
    """
    if raw is None:
        return None
    try:
        ts = int(str(raw).strip())
        # 合理性检查：拒绝明显不是秒级时间戳的值。
        # 2001-09-09 到 2033-05-18 之间，覆盖本项目所有合理场景。
        # 不做检查的话，误传毫秒时间戳（1790545506000）会得到
        # 公元 58000 年的日期，然后静默写进数据库。
        if not (10**9 <= ts <= 2 * 10**9):
            return None
        return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError, OSError, OverflowError):
        return None
