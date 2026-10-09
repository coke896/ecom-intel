"""中文标题分词 —— 词云和 AI 报告共用。

必须独立成模块：分析层不能反向 import Web 层（它本该能脱离 HTTP 单独跑），
而且两侧各写一份分词，结果会对不上（词云最大的词是「充电宝」、报告说是
「快充」，**两边都不报错**）。同一条规则写两处迟早分叉，见
docs/可行性评估.md 14.1、14.4。
"""

from __future__ import annotations

import logging
import re
import threading
from collections import Counter

import jieba

from app.config import settings

# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------

# ★ 必须自己加锁：同步端点跑在线程池里，服务刚起来时几个请求会并发走到这里，
#   而 jieba 的 `load_userdict` **没有**内部锁——两个线程同时往同一个 trie 里
#   写自定义词，行为未定义（症状是"偶发地少几个自定义词"，极难排查）。
_jieba_lock = threading.Lock()
_jieba_ready = False


def ensure_jieba() -> None:
    """加载自定义词典（幂等）。"""
    global _jieba_ready
    if _jieba_ready:
        return
    with _jieba_lock:
        # 双重检查：并发时后面的线程拿到锁后要再判一次，避免重复加载。
        if _jieba_ready:
            return

        # ★ 关掉 jieba 的 DEBUG 日志：首次分词打印的加载信息会让第一次请求看起来像卡住。
        jieba.setLogLevel(logging.INFO)

        # ★ 刻意不做"文件不存在就抛异常"：承诺 git clone 下来不配任何东西也能跑——
        #   缺词典应该是"效果打折"，不是"服务起不来"。
        if settings.userdict_file.exists():
            jieba.load_userdict(str(settings.userdict_file))

        jieba.initialize()
        _jieba_ready = True


def load_stopwords() -> frozenset[str]:
    """读停用词表。每次调用都重读文件——**故意不缓存**：调词云是"改一个词、看
    一眼图"的高频循环，缓存会让它必须重启服务才生效；文件几十 KB，读它是微秒级。
    """
    path = settings.stopwords_file
    if not path.exists():
        return frozenset()
    words = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        words.add(line.lower())
    return frozenset(words)


# ---------------------------------------------------------------------------
# 分词
# ---------------------------------------------------------------------------

_HAS_CJK = re.compile(r"[一-鿿]")
# 纯数字（含小数）："20000" 这种规格数字没有词义，让词云上飘着一堆数字很难看。
_IS_NUMBER = re.compile(r"^\d+(\.\d+)?$")
# 至少含一个"有意义"的字符，否则整个 token 是标点/符号。
_HAS_WORDCHAR = re.compile(r"[0-9a-zA-Z一-鿿]")


def tokenize_titles(
    titles: list[str], stopwords: frozenset[str] | None = None
) -> tuple[Counter[str], dict[str, Counter[str]]]:
    """分词。返回 `(归一化词频, {归一化词: {原始写法: 次数}})`。

    ★ 归一化规则（每条都对应一个具体的坏结果）：大小写归一下沉（否则
      iPhone/iphone/IPHONE 在词云上各占一块）；中文词长度 >= 2；ASCII 词长度
      >= 3 ——这条**不能**写成"丢弃纯 ASCII 词"，那会杀掉 iPhone / WiFi / Type-C；
      丢掉纯数字和纯符号。
    ★ 同时记"原始写法"是为了把 `iphone` 还原成 `iPhone`，在分词这一趟顺手记下
      最便宜；`stopwords=None` 时自己读词表，可传入是为了测试。
    """
    if stopwords is None:
        stopwords = load_stopwords()

    counter: Counter[str] = Counter()
    forms: dict[str, Counter[str]] = {}

    for title in titles:
        if not title:
            continue
        # cut（精确模式）而不是 cut_for_search：后者会额外吐出长词的子串
        # （"蓝牙耳机" → 还给"蓝牙""耳机"），词云上整体词和碎片同时出现。
        for raw in jieba.cut(title):
            token = raw.strip()
            if not token or not _HAS_WORDCHAR.search(token):
                continue

            key = token.lower()
            if key in stopwords or _IS_NUMBER.match(key):
                continue

            if _HAS_CJK.search(key):
                if len(key) < 2:
                    continue
                # 中文词没有大小写，不需要记原始写法
            elif len(key) < 3:
                continue

            counter[key] += 1
            if not _HAS_CJK.search(key):
                forms.setdefault(key, Counter())[token] += 1

    return counter, forms


def display_form(key: str, forms: dict[str, Counter[str]]) -> str:
    """把 `iphone` 还原成 `iPhone` 这类人类写法，取出现次数最多的那种。

    ★ 不取"第一次出现的"：标题按 id 排，排前面的可能恰好是全大写促销标题。
    """
    candidates = forms.get(key)
    if not candidates:
        return key
    # ★ 次数相同时按原文排序：结果不能取决于 Counter 的插入顺序（那又是不确定的）。
    best_form, _ = max(candidates.items(), key=lambda kv: (kv[1], kv[0]))
    return best_form


def rank_keywords(
    titles: list[str], limit: int = 120
) -> list[tuple[str, str, int]]:
    """一步到位：分词 → 排序 → 还原人类写法，返回 [(显示形式, 键, 次数), ...]。

    ★ 打包成一个函数，是为了让词云和 AI 报告不可能因漏掉某一步而产生不同结果
      （漏 `display_form` 报告里就是全小写 iphone；漏排序会让同分词顺序随机，
      进而让 AI 报告缓存永远不命中——关键词列表是 payload 的一部分）。
    """
    ensure_jieba()
    counter, forms = tokenize_titles(titles)
    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [(display_form(k, forms), k, int(v)) for k, v in ranked]
