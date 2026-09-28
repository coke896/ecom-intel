"""中文标题分词 —— 词云和 AI 报告共用。

================================================================================
为什么这个文件必须存在
================================================================================

阶段 3 的 GLM 报告里要包含"标题关键词"这个维度。最省事的写法是在
`analysis/payload.py` 里再写一遍分词，但那会造成两个后果：

  ① **层次倒置**：如果反过来让 payload.py 去 import `app.api.stats`，
     "分析层依赖 Web 层"就成了一个说不通的方向——
     分析层应该能脱离 HTTP 单独跑（就像现在的 `build_payload(db, source)`）。
  ② **两份分词结果对不上**：词云图上最大的词是「充电宝」，
     而报告里说「快充」是最高频词——两边都不报错。

  这和 `app/aggregate.py` 是同一个故事（那一次抽的是分桶和分位数）。
  阶段 2 已经因为"同一条规则写在两个地方"栽过两次
  （见 docs/可行性评估.md 14.1 和 14.4），这几次都是在**写之前**就抽出来。

================================================================================
分词规则里最容易写错的一条
================================================================================

    保留**长度 >= 3** 的 ASCII 词 —— 不是"丢弃纯 ASCII 词"。

早期草稿写的是后者，那会亲手杀掉 `iPhone` / `WiFi` / `Type-C` / `USB`，
而它们恰恰是数码品类里信息量最高的词。
真正的噪声（`LED`、`GB`、`mAh`）交给 `data/stopwords.txt` 按需处理，
不用长度一刀切。
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

# jieba 的初始化只需要做一次，但它不是线程安全的"只做一次"。
#
# ★ 为什么要自己加锁：FastAPI 的同步端点跑在线程池里，
#   服务刚起来时几个请求会并发进来，同时走到这里。
#   jieba 内部对词典加载有锁，但 `load_userdict` **没有**——
#   两个线程同时往同一个 trie 里写自定义词，行为未定义。
#   加锁的成本是几十纳秒，不加的后果是"偶发地少几个自定义词"，
#   而那种 bug 你查一整天都想不到是并发。
_jieba_lock = threading.Lock()
_jieba_ready = False


def ensure_jieba() -> None:
    """加载自定义词典（幂等）。"""
    global _jieba_ready
    if _jieba_ready:
        return
    with _jieba_lock:
        # 双重检查：拿到锁之后要再判一次，否则并发时后面的线程
        # 会白等一次锁然后重复加载。
        if _jieba_ready:
            return

        # ★ 关掉 jieba 的 DEBUG 日志。
        #   默认级别会在首次分词时打印
        #   "Building prefix dict from ... Loading model from cache ..."
        #   这几行会出现在服务日志里，让第一次请求看起来像是卡住了。
        jieba.setLogLevel(logging.INFO)

        # 词典文件缺失不报错，只是词云会难看一些。
        # ★ 这里刻意不做"文件不存在就抛异常"：本项目承诺
        #   `git clone` 下来不配任何东西就能跑起来，而 data/ 是
        #   gitignore 的候选目录。缺词典应该是"效果打折"而不是"服务起不来"。
        if settings.userdict_file.exists():
            jieba.load_userdict(str(settings.userdict_file))

        jieba.initialize()
        _jieba_ready = True


def load_stopwords() -> frozenset[str]:
    """读停用词表。每次调用都重读文件——**故意不缓存**。

    ★ 缓存的诱惑很大（几百行的文件，每次请求读一遍），但代价是：
      调词云的时候要重启服务才生效，而调词云恰恰是一个
      "改一个词、看一眼图、再改"的高频循环。
      这里文件只有几十 KB，OS 的页缓存早就把它放在内存里了，
      open + splitlines 的实际耗时在微秒级。
      用可测量的微小开销换"改完即生效"，这个交易很划算。

    ★ 哈希值算不到一秒，而 AI 报告的一次调用要几秒到几十秒，
      所以这条开销在报告链路上更加可以忽略。
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
# 纯数字（含小数）。"20000" 这种规格数字不该进词云——
# 它没有词义，而且会让词云上飘着一堆数字。
_IS_NUMBER = re.compile(r"^\d+(\.\d+)?$")
# 至少含一个"有意义"的字符，否则整个 token 是标点/符号。
_HAS_WORDCHAR = re.compile(r"[0-9a-zA-Z一-鿿]")


def tokenize_titles(
    titles: list[str], stopwords: frozenset[str] | None = None
) -> tuple[Counter[str], dict[str, Counter[str]]]:
    """分词。返回 `(归一化词频, {归一化词: {原始写法: 次数}})`。

    ★ 归一化规则（每条都对应一个具体的坏结果）：
      · 大小写归一下沉 —— 不归一的话 `iPhone` / `iphone` / `IPHONE`
        会作为三个不同的词在词云上各占一块，同一个概念被拆成三份
      · 中文词长度 >= 2 —— 单字（"宝""器"）没有信息量，
        而且它们几乎都是分词切错的产物
      · ASCII 词长度 >= 3 —— ★ 这条不能写成"丢弃纯 ASCII 词"。
        早期草稿就是这么写的，那会亲手杀掉 iPhone / WiFi / Type-C / USB，
        而它们恰恰是数码品类里信息量最高的词。
        真正的噪声（LED、GB）交给停用词表按需处理，不用长度一刀切。
      · 丢掉纯数字和纯符号

    ★ 为什么同时返回"原始写法"的计数：
      归一化之后词云上会显示 `iphone`（全小写），而人写的是 `iPhone`。
      要还原成人类写法，就得知道每种写法各出现过几次。
      这件事**必须在分词这一趟里顺手记下来**——
      第一版是事后拿着每个关键词再去 `title.lower().find(key)` 扫一遍全部标题，
      120 个词 × 1000 条标题 = 12 万次字符串查找，纯粹是白花的开销，
      而信息在分词的时候本来就摆在手边。

    ★ `stopwords=None` 时自己去读词表。让调用方可以显式传入是为了测试——
      测试想验"某个词因为进了停用词表而被过滤掉"时，
      传一个自造的词表比去改 `data/stopwords.txt` 可靠得多。
    """
    if stopwords is None:
        stopwords = load_stopwords()

    counter: Counter[str] = Counter()
    forms: dict[str, Counter[str]] = {}

    for title in titles:
        if not title:
            continue
        # cut（精确模式）而不是 cut_for_search：
        # 后者会额外吐出长词的子串（"蓝牙耳机" → 还给"蓝牙""耳机"），
        # 词云上会同时出现整体词和它的碎片，看起来像重复统计。
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
    """把 `iphone` 还原成 `iPhone` 这类人类写法。

    取原文里出现次数最多的那种写法。
    ★ 为什么不取"第一次出现的"：标题是按 id 排的，
      而排在前面的可能恰好是一条全大写的促销标题（"IPHONE 15 保护壳"），
      于是整个词云里这个词都会显示成全大写。
      取众数稳得多，而且这正是"归一化"本该有的逆运算。
    """
    candidates = forms.get(key)
    if not candidates:
        return key
    # ★ max 的 key 里带上原文做第二排序键：出现次数相同时，
    #   结果不能取决于 Counter 的插入顺序（那就又变成不确定了）。
    best_form, _ = max(candidates.items(), key=lambda kv: (kv[1], kv[0]))
    return best_form


def rank_keywords(
    titles: list[str], limit: int = 120
) -> list[tuple[str, str, int]]:
    """一步到位：分词 → 排序 → 还原人类写法。

    返回 `[(显示形式, 归一化键, 次数), ...]`，按次数降序、同分按字典序。

    ★ 提供这个"打包"函数，是为了让两个调用方（词云接口和 AI 报告）
      不可能因为漏掉其中一步而产生不同的结果——
      漏掉 `display_form` 会让报告里出现全小写的 `iphone`，
      漏掉排序会让同分词的顺序随机（进而让 AI 报告缓存永远不命中，
      因为关键词列表是 payload 的一部分）。

    ★ 排序必须完全确定：`Counter.most_common()` 在计数相同时的返回顺序
      取决于字典的插入顺序，也就是取决于 SQL 返回标题的顺序。
      于是"给词云加个排序"这种无关改动会让输出悄悄变样，测试也跟着飘。
      显式按 `(-次数, 词)` 排，同分时按字典序，结果就唯一了。
    """
    ensure_jieba()
    counter, forms = tokenize_titles(titles)
    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [(display_form(k, forms), k, int(v)) for k, v in ranked]
