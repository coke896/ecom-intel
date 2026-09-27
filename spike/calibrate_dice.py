"""Dice 相似度阈值标定 —— 用真实爆料标题，不拍脑袋。

================================================================================
为什么必须标定
================================================================================
归并逻辑的软门槛是「Dice 系数 ≥ 阈值 → 判为同一商品」。
这个阈值决定了归并的质量：
  阈值太高 → 同一商品的两条爆料归并不了，真实数据仍然全是单点，
             "价格事件流"这条叙事线落空
  阈值太低 → 不同商品被错误归并，价格序列里混进了别的商品的价格，
             分析结论直接失真

早期版本拍脑袋定过 0.95（这是网上博客最常抄的数），后来又试过 0.8。
两者都没有依据。这个脚本用真实数据算出依据。

================================================================================
方法
================================================================================
1. 抓取真实爆料标题（/jingxuan/ 多页 + /fenlei/ 类目页）
2. 两两计算 Dice 系数
3. 输出【相似度最高的一批对】——这些是最容易混淆的对，
   人眼判断它们是不是同一商品，就得到了标注数据
4. 看标注结果在哪个阈值上分开

★ 诚实说明本方法的局限：
   单次快照里"同一商品被多次爆料"的样本天然稀少（这正是问题本身），
   所以正样本主要来自规格相同、措辞不同的标题对。
   真实生产中正样本会随采集天数增加而变多，阈值可以在那时再校准一次。
   这一点会如实写进 docs/可行性评估.md，不假装标定是完美的。
"""

from __future__ import annotations

import json
import random
import re
import sys
import time
from collections import Counter
from itertools import combinations
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.cleaner import clean_text, normalize_width  # noqa: E402

SPIKE_DIR = Path(__file__).parent
RAW_DIR = SPIKE_DIR / "_raw"
CORPUS_FILE = SPIKE_DIR / "corpus.json"

PAGES = list(range(6, 21))  # 第 1~5 页已抓过
BASE = "https://www.smzdm.com/jingxuan/"
DELAY_RANGE = (2.0, 3.0)

# ★ 默认离线运行（不联网）。
#   原因：实测发现 SMZDM 在约 50~100 次请求后会从"正常返回"升级到
#   【腾讯验证码挑战】，再继续就变成 403 全站拦截。
#   标定工作用的是已经落盘的 10MB 页面，不需要再联网。
#   需要补抓时显式传 --fetch。
OFFLINE = "--fetch" not in sys.argv

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

CARD_RE = re.compile(
    r'<li class="J_feed_za feed-row-wide"(?P<attrs>[^>]*)>(?P<body>.*?)</li>', re.S
)
RE_TITLE = re.compile(r'<h5 class="feed-block-title">\s*<a[^>]*>\s*(.*?)\s*</a>', re.S)
RE_BRAND = re.compile(r"'brand':'(.*?)'")
RE_CATE = re.compile(r"'cate_level1':'(.*?)'")

# 类目页的卡片（结构不同，标题在 side-line-title 里）
RE_SIDE = re.compile(
    r'<a class="side-line-title"[^>]*>([^<]+)</a>', re.S
)


def fetch_more_jingxuan() -> list[dict]:
    """补抓 /jingxuan/ 第 6~20 页。"""
    items: list[dict] = []
    with httpx.Client(headers=HEADERS, timeout=30.0, follow_redirects=True) as client:
        for page in PAGES:
            url = f"{BASE}p{page}/"
            try:
                resp = client.get(url)
            except Exception as e:  # noqa: BLE001
                print(f"  p{page} 请求失败：{e}")
                continue
            if resp.status_code != 200 or len(resp.text) < 5000:
                print(
                    f"  p{page} 异常 HTTP {resp.status_code} "
                    f"len={len(resp.text)} ctype={resp.headers.get('content-type')!r} "
                    f"head={resp.text[:100]!r}"
                )
                continue
            (RAW_DIR / f"jx_cal_p{page}.html").write_text(resp.text, encoding="utf-8")

            found = 0
            for m in CARD_RE.finditer(resp.text):
                body = m.group("body")
                t = RE_TITLE.search(body)
                if not t:
                    continue
                brand = RE_BRAND.search(body)
                cate = RE_CATE.search(body)
                items.append(
                    {
                        "title": clean_text(t.group(1)),
                        "brand": clean_text(brand.group(1)) if brand else None,
                        "category": clean_text(cate.group(1)) if cate else None,
                    }
                )
                found += 1
            print(f"  p{page}: {found} 条")
            time.sleep(random.uniform(*DELAY_RANGE))
    return items


def load_local_corpus() -> list[dict]:
    """把已经抓下来的 HTML 全部解析一遍，凑成语料。"""
    items: list[dict] = []

    # 1) spike/titles.json（前 5 页精选页）
    tj = SPIKE_DIR / "titles.json"
    if tj.exists():
        for it in json.loads(tj.read_text(encoding="utf-8")):
            if it.get("title"):
                items.append(
                    {"title": it["title"], "brand": it.get("brand"),
                     "category": it.get("category")}
                )

    # 2) 所有 _raw/*.html 里的精选页卡片
    for f in sorted(RAW_DIR.glob("*.html")):
        html = f.read_text(encoding="utf-8", errors="ignore")
        for m in CARD_RE.finditer(html):
            body = m.group("body")
            t = RE_TITLE.search(body)
            if not t:
                continue
            brand = RE_BRAND.search(body)
            cate = RE_CATE.search(body)
            items.append(
                {
                    "title": clean_text(t.group(1)),
                    "brand": clean_text(brand.group(1)) if brand else None,
                    "category": clean_text(cate.group(1)) if cate else None,
                }
            )
        # 3) 类目页的商品名（这些正是项目的目标品类）
        for t in RE_SIDE.findall(html):
            title = clean_text(t)
            if title:
                items.append({"title": title, "brand": None, "category": None})

    return items


# ---------------------------------------------------------------------------
# 标题归一化 + 分词
# ---------------------------------------------------------------------------

# 促销噪声词。这些词在标题里高频出现但【完全不携带商品身份信息】——
# "今日必买：海澜之家羊毛衫" 和 "海澜之家羊毛衫" 是同一个商品。
NOISE_WORDS = [
    "今日必买", "百亿补贴", "移动端", "京东自营", "天猫精选", "小编精选",
    "手慢无", "绝对值", "历史低价", "限量", "秒杀", "抢购", "预售",
    "需用券", "领券", "优惠券", "下单", "到手价", "包邮", "直降",
    "门店有售", "新品", "新款", "爆款", "热卖", "特价", "清仓",
    "补贴", "国补", "plus会员", "PLUS会员", "会员",
]

RE_PUNCT = re.compile(r"[（）()\[\]【】<>《》,，.。、;；:：!！?？\"'“”‘’/\\|~—\-+*#@&%￥$]+")


def normalize_title(title: str) -> str:
    """标题归一化：去噪声词、统一大小写与全半角、去标点。"""
    text = normalize_width(title).lower()
    for w in NOISE_WORDS:
        text = text.replace(w.lower(), " ")
    text = RE_PUNCT.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


# 规格 token：数字 + 可选单位。★ 这是硬门槛的核心，必须原样保留。
RE_SPEC = re.compile(r"\d+(?:\.\d+)?\s*(?:gb|g|tb|t|ml|l|mah|mm|cm|m|w|v|a|hz|寸|英寸|匹|核|代|件|支|枚|粒|片|条|只|个|袋|盒|瓶|包)?")


def tokens(title: str) -> list[str]:
    """把归一化后的标题切成 token。

    这里【故意】不引入 jieba：
    标定阶段要的是可复现、无外部依赖的切分。
    jieba 分词结果会随词典版本变化，而那会让阈值标定失去可复现性。
    用空格切分 + 规格 token 提取，对"同商品判定"这个任务足够了。
    """
    norm = normalize_title(title)
    # 保住规格 token：数字和单位之间的空格不能切
    specs = RE_SPEC.findall(norm)
    parts = [p for p in norm.split() if p]
    return parts + [s.strip() for s in specs if s.strip()]


def dice(a: set[str], b: set[str]) -> float:
    """Dice 系数 = 2|A∩B| / (|A|+|B|)

    ★ 为什么用 Dice 而不是 Jaccard：
      Jaccard = |A∩B| / |A∪B|。在短集合上，两者数值差异巨大。
      考虑 {京觅,猕猴桃,24枚,礼盒装} vs {京觅,猕猴桃,24枚,礼盒装,顺丰}：
          Jaccard = 4/5 = 0.80
          Dice    = 8/9 = 0.89
      Dice 对"包含关系"更宽容，而同一商品的两条爆料常常正是包含关系
      （一条多带了"顺丰包邮"这类信息）。
    """
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return 2 * inter / (len(a) + len(b))


def main() -> None:
    print("=== 1. 载入本地语料 ===")
    corpus = load_local_corpus()
    print(f"  本地 {len(corpus)} 条")

    if OFFLINE:
        print("\n=== 2. 离线模式，跳过联网（需要补抓请加 --fetch）===")
    else:
        print("\n=== 2. 补抓 /jingxuan/ p6~p20 ===")
        more = fetch_more_jingxuan()
        print(f"  补抓 {len(more)} 条")
        corpus.extend(more)

    # 去重（按标题）
    seen: set[str] = set()
    uniq: list[dict] = []
    for it in corpus:
        t = (it.get("title") or "").strip()
        if t and t not in seen:
            seen.add(t)
            uniq.append(it)

    CORPUS_FILE.write_text(
        json.dumps(uniq, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n去重后语料：{len(uniq)} 条 → {CORPUS_FILE}")

    # ---- token 长度分布 ----
    tok_lens = [len(set(tokens(it["title"]))) for it in uniq]
    print("\n=== token 数量分布（去重后）===")
    hist = Counter(tok_lens)
    for k in sorted(hist):
        bar = "█" * min(hist[k] // 3, 50)
        print(f"  {k:>3} 个 token: {hist[k]:>4}  {bar}")

    # ---- 两两 Dice，取最高的 N 对 ----
    print("\n=== 计算两两相似度 ===")
    prepared = [(it, set(tokens(it["title"]))) for it in uniq]
    pairs: list[tuple[float, dict, dict]] = []
    for (a, ta), (b, tb) in combinations(prepared, 2):
        if not ta or not tb:
            continue
        d = dice(ta, tb)
        if d >= 0.40:  # 只看有讨论价值的区间
            pairs.append((d, a, b))
    pairs.sort(key=lambda x: -x[0])
    print(f"  ≥0.40 的对数：{len(pairs)}")

    print("\n=== 相似度最高的 40 对（人工标注用）===")
    for d, a, b in pairs[:40]:
        print(f"\n  Dice={d:.3f}")
        print(f"    A [{a.get('category') or '-'}] {a['title'][:60]}")
        print(f"    B [{b.get('category') or '-'}] {b['title'][:60]}")

    print("\n=== 相似度分档统计 ===")
    bands = [(0.90, 1.01), (0.80, 0.90), (0.70, 0.80), (0.60, 0.70), (0.50, 0.60), (0.40, 0.50)]
    for lo, hi in bands:
        n = sum(1 for d, _, _ in pairs if lo <= d < hi)
        print(f"  [{lo:.2f}, {hi:.2f}): {n}")


if __name__ == "__main__":
    main()
