"""数据指纹测试 —— 守住"什么时候命中缓存"这个契约。

================================================================================
这个文件里的断言，全部是在描述**缓存的行为**，而不是哈希函数的行为
================================================================================

"sha256 输出稳定"是标准库保证的事，不需要测。这里要测的是
`build_fingerprint()` 对**哪些变化敏感、对哪些变化不敏感**——
而这正是缓存正确性的全部内容。

两个方向，分别对应两类真实事故：

  · **该变的不变**（漏算了影响结论的字段）
      → 改了数据/prompt/模型，却命中旧缓存 → 报告在说已经过期的数字
      → 不报错、没有日志，只有"我改了代码怎么没生效"的困惑

  · **不该变的变了**（多算了不影响结论的字段）
      → 重新 seed 一次、时间戳刷新，缓存全失效
      → 表现是"缓存好像从来没生效过"，排查方向会被完全带偏

所以下面的用例分成两组，每组都直接对应一类事故。

================================================================================
★ 一条贯穿全文件的规矩：每次只改一个东西
================================================================================

指纹是哈希，改任何输入都会得到不同的哈希值——这意味着
"改了 A，哈希变了"这个断言**几乎永远会通过**，哪怕 A 根本不在指纹的
覆盖范围里（只要构造 payload 的那行代码碰巧也改了别的字段）。

所以每个用例都必须严格做到：**只改目标字段，其余逐字节相同**。
不然测试是绿的，但它什么也没证明。
"""

from __future__ import annotations

import copy
import re

import pytest

from app.analysis import fingerprint as fp
from app.analysis.fingerprint import FingerprintError, build_fingerprint
from app.analysis.payload import build_payload
from app.api.deps import SourceFilter

MODEL = "glm-4-flash"

# 十六进制、恰好 64 位。指纹要直接存进 String(64) 的列，
# 长度不对在 SQLite 上不会报错（它照存），换库时才炸——所以在这里钉住。
_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")


def _row(
    key: str = "mock:1",
    title: str = "安克 充电宝 20000毫安",
    price: float | None = 199.0,
    price_max: float | None = 199.0,
    sales: int | None = 1000,
    brand: str = "安克",
    platform: str | None = "京东",
    category: str | None = "充电器",
    shop: str = "京东自营",
    points: int = 1,
) -> dict:
    """一行商品数据，字段与 `payload.build_payload()` 造出来的完全一致。

    ★ "完全一致"这句话曾经是**假的**：这里只有 8 个键，而真实行有 10 个
      （少了 `platform` / `category`，见 payload._sample_products）。
      假得很有欺骗性——它和指纹的白名单**逐键重合**，
      于是"payload 里有、指纹里没有"的字段在这三处同时不存在。
      补上这两个键之后，这句话才是真的（由下面
      `test_every_field_of_a_real_product_row_reaches_the_fingerprint` 守着）。
    """
    return {
        "key": key,
        "title": title,
        "price": price,
        "price_max": price_max,
        "sales": sales,
        "brand": brand,
        "platform": platform,
        "category": category,
        "shop": shop,
        "points": points,
    }


def _payload(**overrides) -> dict:
    """一个能过校验的最小 payload。

    ★ 结构照着真实的 payload 长，不是随便造个 dict。
      因为指纹覆盖的是**整个 payload**，用一个假结构去测，
      测到的是"指纹函数对这个假结构的行为"，而不是"对真实输入的行为"。
    """
    payload = {
        "source": "mock",
        "summary": {
            "product_count": 2,
            "shop_count": 1,
            "price_point_count": 2,
            "price": {"min": 99.0, "median": 149.0, "max": 199.0, "mean": 149.0},
            "has_sales": True,
        },
        "price_buckets": [{"label": "100-150", "lower": 100.0, "upper": 150.0, "count": 1}],
        "shops": [{"name": "京东自营", "product_count": 2, "avg_price": 149.0}],
        "keywords": [{"name": "充电宝", "count": 2}],
        "sample": {"basis": "sales_desc", "size": 2, "total_products": 2, "note": "样本"},
        "top_products": [
            _row(key="mock:1", title="安克 充电宝 20000毫安", price=199.0, sales=1000),
            _row(
                key="mock:2",
                title="绿联 充电宝 10000毫安",
                price=99.0,
                price_max=99.0,
                sales=2000,
                brand="绿联",
                shop="京东自营",
                points=3,
            ),
        ],
    }
    payload.update(overrides)
    return payload


def _fp(payload: dict, model: str = MODEL) -> str:
    return build_fingerprint(payload, model)


# ===========================================================================
# 一、基本性质
# ===========================================================================


def test_hash_is_64_hex_characters() -> None:
    assert _HEX64.match(_fp(_payload())), "指纹必须是 64 位十六进制（要存进 String(64)）"


def test_same_input_same_hash() -> None:
    """同输入同 hash —— 缓存能工作的前提。

    ★ 传两个**分别构造**的 payload，而不是同一个对象调两次。
      同一个对象调两次只能证明"函数是纯的"，
      而真实场景是两次请求各自查库、各组装一份 payload——
      要证明的是这两份**内容相同**的数据能算出同一个指纹。
    """
    assert _fp(_payload()) == _fp(_payload())


def test_a_completely_different_dataset_changes_the_hash() -> None:
    """先验一下"改了东西哈希确实会变"，否则下面的用例可能全都是假通过。"""
    other = _payload()
    other["top_products"] = [_row(key="smzdm:999", title="完全无关的商品")]
    assert _fp(other) != _fp(_payload())


# ===========================================================================
# 二、★ 顺序无关（不排的话，缓存会随查询计划的变化而失效）
# ===========================================================================


def test_shuffling_products_does_not_change_the_hash() -> None:
    """★★ 商品顺序打乱，指纹必须不变。

    这是指纹能不能用的一块试金石。真实场景里顺序**确实会变**：
      · 加了一个索引，SQLite 换了扫描方式
      · 后来给查询加了 `ORDER BY price`（对展示有用，但不该影响缓存键）
      · 分页/并发导致返回顺序不同

    不排序的话，上面任何一种都会让"数据一个字没改"但缓存全部失效——
    而排查时你会去怀疑缓存没写进库、怀疑进程重启，
    不会想到是查询顺序。

    ★ 反向也要测（见下一个用例）：排序**不能**多排到把不同的数据排成一样。
    """
    payload = _payload()
    shuffled = _payload()
    shuffled["top_products"] = list(reversed(payload["top_products"]))

    # 先确认这两份数据确实不是同一个顺序，否则这个测试是空转的
    assert [r["key"] for r in payload["top_products"]] != [
        r["key"] for r in shuffled["top_products"]
    ]
    assert _fp(shuffled) == _fp(payload)


def test_shuffling_does_not_swallow_differences() -> None:
    """排序是按 `key` 排的，不是把行当成一个集合——
    交换两行的**内容**（key 不动）必须被发现。

    ★ 这个用例防的是"排序写成了排序 + 去重"这种错误实现：
      那种实现下，两行内容一样时看不出问题，但内容不同时也会被吃掉一份。
    """
    a = _payload()
    b = _payload()
    rows = b["top_products"]
    # 交换两行的价格与标题，key 保持原位
    rows[0]["price"], rows[1]["price"] = rows[1]["price"], rows[0]["price"]
    rows[0]["title"], rows[1]["title"] = rows[1]["title"], rows[0]["title"]

    assert _fp(a) != _fp(b)


# ===========================================================================
# 三、★ 归一化：同一个价格的四种写法必须同值
# ===========================================================================


@pytest.mark.parametrize(
    "variant",
    [199, 199.0, "199.00", "199", 198.99999999999997, " 199.0 "],
)
def test_price_representations_all_collapse_to_one_hash(variant: object) -> None:
    """★ 199 / 199.0 / "199.00" / 198.99999999999997 必须算出同一个指纹。

    这个场景一点都不假：价格从 JSON 接口读进来是 float，
    从 CSV 读进来是字符串，从数据库读出来是 Decimal 或 float，
    而 `198.99999999999997` 是任何一次浮点加减的产物——
    **它们在 `==` 意义上相等，但 `json.dumps` 出来是不同的字节。**

    没有这一步归一化，症状是同一批数据每次采集都产生一个新指纹，
    缓存命中率接近零，而"数据明明没变"。

    ★ 注意 `" 199.0 "` 带空格：这是从 HTML 里抠价格时最常见的形态。
    """
    payload = _payload()
    payload["top_products"][0]["price"] = variant
    baseline = _payload()
    baseline["top_products"][0]["price"] = 199.0

    assert _fp(payload) == _fp(baseline), f"{variant!r} 没有归一化到 199.0"


def test_sales_int_and_float_are_the_same() -> None:
    """销量 `1000` 和 `1000.0` 必须同值。同一个理由：字节不同。

    ★ 这个在真实链路上一定会遇到：Mock 数据里销量是 int，
      而从 JSON 接口 / pandas 读进来时会变成 float。
    """
    a = _payload()
    a["top_products"][0]["sales"] = 1000
    b = _payload()
    b["top_products"][0]["sales"] = 1000.0

    assert _fp(a) == _fp(b)


def test_a_real_price_difference_is_still_detected() -> None:
    """归一化不能把真的差异也抹平了（199.0 vs 199.5 必须不同）。"""
    a = _payload()
    a["top_products"][0]["price"] = 199.0
    b = _payload()
    b["top_products"][0]["price"] = 199.5

    assert _fp(a) != _fp(b)


def test_dirty_price_values_do_not_raise_and_are_stable() -> None:
    """脏价格不抛异常，且**同样的脏值得到同样的指纹**。

    ★ 为什么必须吞掉异常：指纹算不出来会让整个分析接口 500，
      而"1000 个商品里有 1 个价格是脏的"不该让整份报告生成不了。
      这和 cleaner 的承诺是同一条：管道里的一条脏数据不能掀桌子。

    ★ 为什么"要稳定"比"要正确"更重要：脏值全部归一成 None，
      至少保证**同一批脏数据**每次算出同一个指纹。
      如果归一成一个随机值或异常，缓存就彻底失效了。
    """
    for dirty in ["暂无报价", "面议", "1..2", "0x1F", "🙂", [], {}]:
        payload = _payload()
        payload["top_products"][0]["price"] = dirty
        first = _fp(payload)
        assert _HEX64.match(first), f"脏价格 {dirty!r} 没有算出合法指纹"

        again = _payload()
        again["top_products"][0]["price"] = dirty
        assert _fp(again) == first, f"脏价格 {dirty!r} 两次算出的指纹不同"

    # 脏值归一成 None，所以"价格脏掉"和"价格本来就是 None"应该是同一个指纹。
    # 这不是缺陷——两者对模型来说都是"没有价格"，报告不该因此重算。
    dirty = _payload()
    dirty["top_products"][0]["price"] = "暂无报价"
    none = _payload()
    none["top_products"][0]["price"] = None
    assert _fp(dirty) == _fp(none)


# ===========================================================================
# 四、★ 该变的必须变（防"改了代码却命中旧缓存"）
# ===========================================================================


def test_prompt_version_change_invalidates_everything(monkeypatch) -> None:
    """★★ 改 `PROMPT_VERSION`，指纹必变。

    这是**唯一需要人肉维护**的一条联动（改 prompts.py 就要改版本号），
    所以它是整个缓存机制里最容易漏的地方，必须有测试守着。

    ★ 注意这里测的是"改了版本号指纹会变"——
      它**证明不了**"你改了 prompt 之后记得改版本号"。
      那一条只能靠 prompts.py 顶部那句警告。测试能做的到这里为止，
      而这一点点也值得测：万一有人把 PROMPT_VERSION 从指纹输入里删了，
      这条会立刻红。
    """
    before = _fp(_payload())
    monkeypatch.setattr(fp, "PROMPT_VERSION", "v2")
    assert _fp(_payload()) != before


def test_model_name_is_part_of_the_hash() -> None:
    """换模型必须失效缓存：同样的数据在 glm-4-flash 和 glm-4-plus 上
    会得到不同的结论，缓存不能跨模型复用。"""
    assert _fp(_payload(), model="glm-4-flash") != _fp(_payload(), model="glm-4-plus")


@pytest.mark.parametrize(
    "field, value",
    [
        ("title", "安克 充电宝 30000毫安"),   # 标题改了
        ("price", 299.0),                      # 价格变了
        ("price_max", 259.0),                  # 区间上界变了
        ("sales", 9999),                       # 销量变了
        ("brand", "倍思"),                     # 品牌变了
        ("platform", "天猫"),                  # ★ 销售平台变了（漏算过）
        ("category", "移动电源"),              # ★ 类目变了（漏算过）
        ("shop", "天猫超市"),                  # 店铺变了
        ("points", 7),                         # 观测次数变了
    ],
)
def test_every_field_in_the_prompt_changes_the_hash(field: str, value: object) -> None:
    """★ 判据只有一条：**出现在 prompt 里的字段都必须进指纹**。

    `points` 是最容易被漏掉的那个——它不像是"商品数据"，
    更像是个统计量。但它确实在 prompt 里（"这个商品被爆料过几次"），
    所以它变了报告就可能过时。

    ★★ 但**别把这份清单当成保障**。它曾经同时漏掉 `platform` 和 `category`：
      清单是照着 `_row()` 写的，`_row()` 是照着指纹白名单写的，
      三者互为镜像——漏掉的字段在三处同时不存在，所以清单全绿。

      清单能覆盖"我知道的字段"，覆盖不了"上游将来加的字段"。
      真正兜底的是下面那条**从真实 payload 推导**的用例
      （`test_every_field_of_a_real_product_row_reaches_the_fingerprint`），
      这条清单的价值只剩"断言得更直白、失败信息更好读"。
    """
    changed = _payload()
    changed["top_products"][0][field] = value
    assert _fp(changed) != _fp(_payload()), f"改了 {field} 指纹却没变"


@pytest.mark.parametrize(
    "section",
    ["summary", "price_buckets", "shops", "keywords"],
)
def test_every_aggregate_section_changes_the_hash(section: str) -> None:
    """汇总区也必须进指纹——它们是**派生量**，但正因为如此才更要算。

    prompt 里"整体均价 149 元"这句话直接来自 summary，
    summary 变了而指纹没变，模型就会拿着旧均价说新数据。
    """
    changed = _payload()
    if section == "summary":
        changed["summary"]["price"]["mean"] = 999.0
    elif section == "price_buckets":
        changed["price_buckets"][0]["count"] = 999
    elif section == "shops":
        changed["shops"][0]["avg_price"] = 999.0
    else:
        changed["keywords"][0]["count"] = 999

    assert _fp(changed) != _fp(_payload()), f"改了 {section} 指纹却没变"


def test_source_is_part_of_the_hash() -> None:
    """`?source=mock` 和 `?source=smzdm` 是两份不同的数据，不能共用缓存。"""
    changed = _payload()
    changed["source"] = "smzdm"
    assert _fp(changed) != _fp(_payload())


# ===========================================================================
# 五、★ 不该变的绝不能变（防"缓存永远命中不了"）
# ===========================================================================


@pytest.mark.parametrize(
    "noisy",
    [
        "last_crawled_at",
        "updated_at",
        "created_at",
    ],
)
def test_timestamps_in_the_payload_are_rejected_outright(noisy: str) -> None:
    """★ payload 里出现时间戳字段 → 直接抛 `FingerprintError`，不静默算。

    ★ 为什么不"自动忽略掉"而是抛异常：
      自动忽略看起来更友好，但那样等于**默认**这些字段是可以随便加的——
      而真正危险的是那个叫 `snapshot_time` 的新字段（黑名单拦不住它），
      它会静默进指纹、静默让缓存全失效。
      抛异常至少给了两次机会：加字段的人当场看见，
      和漏过去之后的第一次调用报错（而不是一个月后有人问
      "为什么缓存从来没命中过"）。

    ★ 三层都要查（见 parametrize 里的位置）：
      时间戳最常出现在**最内层**的商品行里，只看第一层等于没查。
    """
    if noisy == "last_crawled_at":
        payload = _payload()
        payload["top_products"][0][noisy] = "2026-01-01T00:00:00"
    elif noisy == "updated_at":
        payload = _payload()
        payload["summary"][noisy] = "2026-01-01T00:00:00"
    else:
        payload = _payload()
        payload["price_buckets"][0][noisy] = "2026-01-01T00:00:00"

    with pytest.raises(FingerprintError):
        _fp(payload)


def test_the_self_increment_id_is_rejected() -> None:
    """★ 自增 `id` 是最典型的"会变但不影响结论"的字段。

    重新 seed 一次，每行的 id 都变了（SQLite 的 rowid 不复用），
    于是指纹全变、缓存全失效——而数据内容一个字没改。
    """
    payload = _payload()
    payload["top_products"][0]["id"] = 42
    with pytest.raises(FingerprintError):
        _fp(payload)


def test_deeply_nested_timestamps_are_found() -> None:
    """★ 递归必须真的钻到底：`summary.price.updated_at` 这种三层嵌套也要抓到。

    只查第一层和查两层看起来差别不大，但 payload 恰好是
    "顶层 → summary → price → 各种统计量"这样的深度。
    """
    payload = _payload()
    payload["summary"]["price"]["updated_at"] = "2026-01-01T00:00:00"
    with pytest.raises(FingerprintError):
        _fp(payload)


def test_timestamps_inside_lists_are_found() -> None:
    """★ 列表也要下钻：时间戳最可能出现的地方就是 `top_products` 这个列表里。"""
    payload = _payload()
    payload["top_products"][1]["recorded_at"] = "2026-01-01T00:00:00"
    with pytest.raises(FingerprintError):
        _fp(payload)


def test_the_business_key_carries_the_identity_and_does_change_the_hash() -> None:
    """★★ 商品的**身份**必须进指纹，而它是以 `key` 的形式进去的。

    `key = f"{source}:{source_id}"`（"mock:1" / "smzdm:183089533"），
    由 `payload.py` 装配时就拼好——**payload 里根本没有 `source_id` 这个字段**。

    ★ 这一点是写这个测试时才想清楚的，值得记下来：
      "业务主键要进指纹"不等于"要往 payload 里加一个 source_id 字段"。
      身份已经在 `key` 里了，再加一个字段是**同一个东西的第二种表示**——
      而两份表示迟早会分叉（改了一处忘了另一处），
      且分叉的表现是"指纹算的是旧身份、报告说的是新身份"。
      `_normalize_product_row` 是白名单，所以多塞一个 `source_id`
      会被它**静默丢掉**——这正是白名单的好处：不是漏了，是不要。

    ★ 那如果 `key` 变了但商品其实没变呢？不会——`key` 由 (source, source_id)
      拼成，而 (source, source_id) 上有唯一约束，它在库里就是身份的定义。
    """
    baseline = _payload()
    other = _payload()
    other["top_products"][0]["key"] = "mock:999"

    assert _fp(other) != _fp(baseline), "key 变了指纹却不变——身份没进指纹"


def test_business_key_names_are_not_mistaken_for_the_id_blacklist() -> None:
    """★ `source_id` / `shop_id` 这类**含 "id" 的合法字段名**不能被误伤。

    这是一个很容易踩的坑：黑名单里有一个 `id`，
    而实现"匹配"这一步时有两条路——
      ① 压平后**完全相等**（`source_id` → `sourceid` ≠ `id`）→ 安全
      ② 包含子串（`"id" in "source_id"` → True）→ 误伤

    ② 的后果是**分析接口直接 500**：payload 里只要有一个 `shop_id`，
    整份报告就生成不了。而报错说的是"payload 里有 id 字段"，
    排查的人会盯着商品的自增 id 看半天，不会想到问题出在 "包含" 这个匹配方式上。

    ★ 所以这里不测"指纹算得对不对"，测的是**匹配的严格程度**。
      它是那种"实现只要稍微简化一点就会变红"的测试——正是该有的样子。
    """
    payload = _payload()
    payload["top_products"][0]["source_id"] = "183089533"
    payload["top_products"][0]["shop_id"] = 7
    payload["shops"][0]["shop_id"] = 7
    payload["summary"]["brand_missing_count"] = 0  # 含 "id" 的反例：missing

    assert _HEX64.match(_fp(payload))


@pytest.mark.parametrize("variant", ["CreatedAt", "createdAt", "CREATED_AT", "created-at"])
def test_timestamp_name_variants_are_all_caught(variant: str) -> None:
    """★ 时间戳字段名的**各种写法**都要拦住。

    ★ 这条是被测试逼出来的一个真实缺口。原本的检查是
      `key.lower() in _FORBIDDEN_KEYS`，看起来已经"大小写不敏感"了，
      但它只处理大小写、**不处理分隔符**：

          "Created_At".lower() = "created_at"   → 命中 ✅
          "CreatedAt".lower()  = "createdat"    → **漏掉** ❌

      而驼峰恰好是从任何 JS/JSON 来源抄过来的字段名的默认形状。
      漏掉它的后果不是报错，是**静默**——这个字段悄悄进指纹，
      于是每采集一次、缓存就全部失效一次，
      而没有人会怀疑"是某个字段名没被拦住"。

    ★ 修法是把字段名压成"只有字母数字"的形状再比对
      （`_key_shape`），而不是继续往黑名单里加 `"createdat"` 这种变体——
      加变体的做法永远追不上变化的写法。
    """
    payload = _payload()
    payload["top_products"][0][variant] = "2026-01-01T00:00:00"
    with pytest.raises(FingerprintError):
        _fp(payload)
    # key 的写法变了，但报错里要出现**原始的**字段名，否则排查时对不上号
    payload2 = _payload()
    payload2["top_products"][0][variant] = "2026-01-01T00:00:00"
    with pytest.raises(FingerprintError, match=variant):
        _fp(payload2)


def test_the_error_message_says_where_and_what_to_do() -> None:
    """报错信息要能照着做。

    ★ 这条不是在测文案好看：抛异常的全部价值就在于**让人知道怎么改**。
      只说一句"payload 非法"，使用者只会去翻源码；
      指出路径和两种可能，他就能当场改掉。
      （同类的错误处理风格见 cleaner：脏数据要么给结果、要么给原因，
       绝不给一个需要猜的失败。）
    """
    payload = _payload()
    payload["top_products"][0]["last_crawled_at"] = "2026-01-01T00:00:00"
    with pytest.raises(FingerprintError) as excinfo:
        _fp(payload)

    message = str(excinfo.value)
    assert "last_crawled_at" in message, "报错里没说是哪个字段"
    assert "source_id" in message, "报错里没给可照做的建议"


def test_empty_product_list_is_allowed() -> None:
    """没有商品时不抛异常。

    ★ 空数据源由 reporter 拦在前面（它根本不调指纹），
      但指纹函数自己不该因为"列表是空的"就炸——
      一个函数对空输入的处理方式，是它边界是否清楚的标志。
    """
    payload = _payload(top_products=[])
    assert _HEX64.match(_fp(payload))


def test_missing_optional_sections_do_not_raise() -> None:
    """payload 缺了某个区（上游改了装配逻辑）时不该抛异常。

    ★ 用 `.get(...) or {}` / `or []` 而不是 `payload["x"]`：
      键不存在和值为 None 是同一件事，两者都该归一成空。
    """
    assert _HEX64.match(_fp({"top_products": []}))


# ===========================================================================
# 六、★ 端到端：真实的 payload 必须能算出指纹
# ===========================================================================


def test_the_real_payload_is_fingerprint_safe(db, make_product) -> None:
    """★★ 用真实数据装配出来的 payload，必须能过 `_assert_fingerprint_safe`。

    这是本文件里最有价值的一条，因为它守的是**将来**的改动：

      `payload.py` 里加一个字段是极常见的操作（比如"加个 snapshot_time
      方便排查"），而加了之后所有缓存都会静默失效——
      除非有人拦着。这条测试就是那个拦截点。

    ★ 它测的不是"指纹算得对不对"，而是"这两个模块还能不能合作"。
      这类跨模块的契约测试通常没人写，因为每个模块自己的测试都是绿的。
    """
    make_product(sid="m-1", title="安克 充电宝 20000毫安", price=199.0, sales=1000, points=2)
    make_product(
        sid="m-2", title="绿联 充电宝 10000毫安", price=99.0, sales=2000, brand="绿联"
    )

    payload = build_payload(db, SourceFilter.all)
    assert _HEX64.match(build_fingerprint(payload, MODEL))


def test_the_real_payload_hash_is_stable_across_rebuilds(db, make_product) -> None:
    """★ 同样的数据，装配两次得到同一个指纹。

    这条听起来是废话（同一个函数同样的输入），但它其实在测**装配过程有没有
    引入不稳定的东西**——比如按 `created_at` 排序、把 `utcnow()` 放进 summary、
    或者 SQLite 两次返回顺序不同。

    ★ 两次装配之间**故意不再插入数据**，但会 commit 一次：
      真实场景里"两次请求"之间数据库是提交过的，
      而 commit 之后 expire 的对象重新加载，某些字段的取法可能不同。

    这个用例红了，说明 payload 里混进了随时间/状态变化的东西——
    而那正是"缓存莫名其妙不命中"的根源。
    """
    make_product(sid="m-1", title="安克 充电宝 20000毫安", price=199.0, sales=1000, points=3)
    make_product(sid="m-2", title="绿联 充电宝 10000毫安", price=99.0, sales=2000, brand="绿联")
    make_product(sid="m-3", title="倍思 氮化镓充电器 65W", price=149.0, sales=500, brand="倍思")

    first = build_fingerprint(build_payload(db, SourceFilter.all), MODEL)
    db.commit()
    second = build_fingerprint(build_payload(db, SourceFilter.all), MODEL)

    assert first == second


def _mutate(value: object) -> object:
    """给一个字段造一个**与它不同**的值。

    ★ `None` 这一支返回的是**数字 1，不是字符串**，这一点是刻意的：
      它要同时适用于文本字段和数值字段——
        文本：`_normalize_text(1)` → "1" ≠ ""
        数值：`_round_price(1)`  → 1.0 ≠ None
      换成字符串的话，数值字段会被 `_round_price` 的 except 分支兜成 None，
      于是"价格根本没进指纹"和"价格进了指纹、只是脏值被归一"两种实现
      **都会让这个用例通过**——用例变成空转。
      （本文件开头那条"每次只改一个东西"的规矩，防的就是这类空转。）
    """
    if value is None:
        return 1
    if isinstance(value, str):
        return f"{value}·改"
    return value + 1


def test_every_field_of_a_real_product_row_reaches_the_fingerprint(db, make_product) -> None:
    """★★ 真实商品行里的**每一个**键，改了都必须让指纹变。

    ★ 为什么需要这一条：上面那份参数化清单是**手写的**，
      而且和 `_row()`、和指纹的白名单互为镜像。
      "payload 里有、指纹里没有"的字段，在这种镜像里**三处同时不存在**，
      于是谁也不会发现、清单还是全绿。

      这不是假想：写这条用例之前，`platform` / `category` 正是这样漏掉的
      （真实行 10 个键，白名单只有 8 个，参数化清单 7 个字段）。

    ★ 所以这里不再列字段，改成**从上游推导**：
      拿一份真实装配出来的 payload，把 `top_products[0]` 的每个键逐个改一遍，
      看指纹认不认。payload 将来加了字段，这条自动就覆盖到了——
      而"上游加字段"恰恰是手写清单永远追不上的那件事。

    ★ 判据仍然只有一条（见 fingerprint.py 的模块注释）：
      **出现在 prompt 里的每个字段都必须进指纹**。
      而 `build_user_prompt()` 是把整个 payload 原样序列化发出去的
      （不筛字段、不重命名），所以"行里有这个键"就等于"模型看得见这个键"。
      这也是这条用例敢直接从行里推导覆盖范围的依据。
    """
    make_product(
        sid="m-1",
        title="安克 充电宝 20000毫安",
        price=199.0,
        sales=1000,
        points=2,
        platform="京东",
        category="充电器",
    )
    payload = build_payload(db, SourceFilter.all)

    row = payload["top_products"][0]
    # 先确认样本不是空的：空行会让下面的循环一次都不跑，用例静默地什么也没测
    assert row, "样本是空的，这个用例没有测到任何字段"

    baseline = build_fingerprint(payload, MODEL)
    # ★ 收集完再断言，而不是撞到第一个就停：
    #   漏字段往往是一漏一批（platform / category 就是同一行里一起漏的），
    #   逐个失败会让人修一个跑一次，而每次跑完只被告知下一个。
    uncovered: list[str] = []
    for field in sorted(row):
        changed = copy.deepcopy(payload)
        changed["top_products"][0][field] = _mutate(row[field])
        if build_fingerprint(changed, MODEL) == baseline:
            uncovered.append(field)

    assert not uncovered, (
        f"这些字段在 prompt 里、却不在指纹的覆盖范围内：{uncovered}。"
        f"改它们仍然命中旧缓存——报告会拿着过期的数字说话，而且不报错。"
    )


def test_different_sources_produce_different_fingerprints(db, make_product) -> None:
    """★ Mock 与真实数据必须算出不同的指纹，否则一个数据源的分析结果
    会被另一个数据源复用——报告里说的数字和屏幕上的过滤条件对不上，
    而且完全看不出是哪一步出了问题。"""
    make_product(sid="m-1", title="安克 充电宝", price=199.0, sales=1000)
    make_product(
        sid="183089533", title="今日必买：京觅 猕猴桃", source="smzdm", price=17.5, sales=None
    )

    mock = build_fingerprint(build_payload(db, SourceFilter.mock), MODEL)
    smzdm = build_fingerprint(build_payload(db, SourceFilter.smzdm), MODEL)
    everything = build_fingerprint(build_payload(db, SourceFilter.all), MODEL)

    assert len({mock, smzdm, everything}) == 3, "三个数据源的指纹出现了重复"
