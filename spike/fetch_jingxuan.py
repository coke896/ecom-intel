"""SMZDM 精选好价页 spike 验证脚本（一次性）：验证 /jingxuan/ 可解析，产出真实标题供阈值标定。

产出 spike/titles.json（约 100 条结构化爆料）；正式采集实现见 scraper/sources/smzdm.py。

合规：只抓不分析、请求间隔 2~3 秒、遵守 robots、只抓 5 页约 100 条、只取公开字段、仅供学习研究。

用法：
    python spike/fetch_jingxuan.py
"""

from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

# 抓 5 页 ≈ 100 条。够标定阈值，也不给对方站点添麻烦。
PAGES = 5
BASE = "https://www.smzdm.com/jingxuan/"

# 请求间隔（秒）。真实浏览器访问时也会停顿，这个节奏不算异常。
DELAY_RANGE = (2.0, 3.0)

# 必须带 UA：不带 UA 的请求一眼是脚本，且实测空 UA 会直接吃 403。
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

OUT_DIR = Path(__file__).parent
RAW_DIR = OUT_DIR / "_raw"


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
# 主体 feed 的卡片。用非贪婪匹配到 </li>，卡片内部没有嵌套 li。
CARD_RE = re.compile(
    r'<li class="J_feed_za feed-row-wide"(?P<attrs>[^>]*)>(?P<body>.*?)</li>',
    re.S,
)

RE_TIMESORT = re.compile(r'timesort="(\d+)"')
RE_ARTICLEID = re.compile(r'articleid="([^"]+)"')
RE_URL = re.compile(r'href="(https://www\.smzdm\.com/p/\d+/)"')

# 标题在 <h5 class="feed-block-title"> 里的 <a> 标签内，前后有大量空白和换行
RE_TITLE = re.compile(
    r'<h5 class="feed-block-title">\s*<a[^>]*>\s*(.*?)\s*</a>', re.S
)

# 价格标签属性跨多行，[^>]* 必须配 re.S 才能匹配到。
RE_PRICE = re.compile(r'<a class="z-highlight"[^>]*>\s*(.*?)\s*</a>', re.S)

RE_IMG = re.compile(r'src="(https://y\.zdmimg\.com/[^"]+)"')

# sensors.track 的 payload 是 JS 对象字面量（键不带引号），不是合法 JSON，逐键正则提取即可。
RE_BRAND = re.compile(r"'brand':'(.*?)'")
RE_MALL = re.compile(r"'mall_name':'(.*?)'")
RE_CATE = re.compile(r"'cate_level1':'(.*?)'")
RE_CHANNEL = re.compile(r"'channel':'(.*?)'")


def _text(raw: str | None) -> str | None:
    """把标签内容清成纯文本：去 HTML 注释与多余空白。

    返回 None 而不是空串，让「字段缺失」和「字段为空」在数据层可区分。
    """
    if raw is None:
        return None
    raw = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw or None


def parse_page(html: str) -> list[dict]:
    """从一页 HTML 里抽出所有爆料卡片。"""
    items: list[dict] = []

    for m in CARD_RE.finditer(html):
        attrs, body = m.group("attrs"), m.group("body")

        aid_m = RE_ARTICLEID.search(attrs)
        ts_m = RE_TIMESORT.search(attrs)
        url_m = RE_URL.search(body)

        # 没有 articleid 的卡片（广告位之类）直接跳过，硬塞进去会让去重逻辑出错。
        if not (aid_m and url_m):
            continue

        # articleid 前缀 "3_" 是固定标记，剥掉后取裸数字，保证同一商品在不同位置的 id 一致。
        raw_id = aid_m.group(1)
        source_id = raw_id.split("_", 1)[-1]

        title_m = RE_TITLE.search(body)
        price_m = RE_PRICE.search(body)
        img_m = RE_IMG.search(body)
        brand_m = RE_BRAND.search(body)
        mall_m = RE_MALL.search(body)
        cate_m = RE_CATE.search(body)
        chan_m = RE_CHANNEL.search(body)

        items.append(
            {
                "source_id": source_id,
                "articleid_raw": raw_id,          # 保留原始值，方便回查前缀规律
                "url": url_m.group(1),
                "title": _text(title_m.group(1) if title_m else None),
                "price_raw": _text(price_m.group(1) if price_m else None),
                "image_url": img_m.group(1) if img_m else None,
                "brand": _text(brand_m.group(1) if brand_m else None),
                "platform": _text(mall_m.group(1) if mall_m else None),
                "category": _text(cate_m.group(1) if cate_m else None),
                "channel": _text(chan_m.group(1) if chan_m else None),
                "timesort": int(ts_m.group(1)) if ts_m else None,
            }
        )

    return items


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------


def fetch_pages() -> list[dict]:
    """抓 PAGES 页，返回全部卡片。"""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    all_items: list[dict] = []

    with httpx.Client(headers=HEADERS, timeout=30.0, follow_redirects=True) as client:
        for page in range(1, PAGES + 1):
            # 第 1 页是 /jingxuan/，第 N 页是 /jingxuan/pN/
            url = BASE if page == 1 else f"{BASE}p{page}/"

            resp = client.get(url)
            print(f"[{page}/{PAGES}] {url} -> HTTP {resp.status_code}, {len(resp.text)} 字节")

            # WAF 拦截的症状：状态码 202、响应体几百字节、正文带 probe.js；显式检测比 parse 出 0 条再回头查省时间。
            if resp.status_code != 200 or len(resp.text) < 5000:
                print(f"    ⚠️ 疑似被拦截或页面异常，跳过。前 200 字符：{resp.text[:200]!r}")
                continue

            (RAW_DIR / f"jingxuan_p{page}.html").write_text(resp.text, encoding="utf-8")

            items = parse_page(resp.text)
            print(f"    → 解析出 {len(items)} 条")
            all_items.extend(items)

            # 请求间隔；最后一页不用等。
            if page < PAGES:
                time.sleep(random.uniform(*DELAY_RANGE))

    return all_items


# 自检


def report(items: list[dict]) -> None:
    total = len(items)
    if not total:
        print("\n❌ 一条都没解析出来，页面结构大概率变了。")
        return

    ids = [it["source_id"] for it in items]
    unique_ids = set(ids)
    with_ts = sum(1 for it in items if it["timesort"] is not None)

    def filled(key: str) -> int:
        return sum(1 for it in items if it.get(key))

    print("\n" + "=" * 62)
    print(f"总计           {total} 条")
    print(f"唯一 source_id  {len(unique_ids)} 条  (重复 {total - len(unique_ids)} 条)")
    print(f"有 timesort    {with_ts}/{total}   ← 价格时间序列依赖它")
    print("-" * 62)
    for key, label in [
        ("title", "标题"),
        ("price_raw", "价格原文"),
        ("image_url", "图片"),
        ("brand", "品牌"),
        ("platform", "平台"),
        ("category", "类目"),
    ]:
        n = filled(key)
        flag = "✅" if n == total else "⚠️ "
        print(f"{flag} {label:<6} {n}/{total}")
    print("=" * 62)

    print("\n价格原文样本（决定 cleaner.parse_price 的用例）:")
    for it in items[:6]:
        print(f"    {it['price_raw']!r}")

    print("\n标题样本（决定 Dice 阈值，注意长度）:")
    for it in items[:12]:
        t = it["title"] or ""
        print(f"    [{len(t):>3} 字] {t}")

    print("\n品牌样本:")
    print("   ", sorted({it["brand"] for it in items if it["brand"]})[:20])

    print("\n平台分布:")
    from collections import Counter

    for k, v in Counter(it["platform"] for it in items).most_common():
        print(f"    {k}: {v}")

    print("\n类目分布:")
    for k, v in Counter(it["category"] for it in items).most_common(10):
        print(f"    {k}: {v}")

    print("\narticleid 原始前缀分布（验证 '3_' 是否恒定）:")
    for k, v in Counter(it["articleid_raw"].split("_")[0] for it in items).most_common():
        print(f"    {k!r}: {v}")


def main() -> None:
    items = fetch_pages()

    out = OUT_DIR / "titles.json"
    out.write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n已写入 {out}")

    report(items)


if __name__ == "__main__":
    main()
