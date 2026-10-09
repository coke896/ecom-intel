"""数据指纹测试 —— 守住"什么时候命中缓存"这个契约。

测的是 `build_fingerprint()` 对**哪些变化敏感、对哪些变化不敏感**：
该变的不变（报告在说已经过期的数字）和不该变的变了（缓存永远命中不了）
都不报错，只能靠测试守。

每个用例都必须**只改目标字段、其余逐字节相同**——指纹是哈希，
"改了 A，哈希变了"几乎永远会通过，不这么做测试是绿的却什么也没证明。
"""

from __future__ import annotations

import copy
import re

import pytest

from app.analysis import fingerprint as fp
from app.analysis.fingerprint import FingerprintError, build_fingerprint
from app.analysis.payload import build_payload
from app.source_scope import SourceFilter

MODEL = "glm-4-flash"

# 十六进制、恰好 64 位——指纹要直接存进 String(64) 的列，长度不对换库时才炸。
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
    """一行商品数据，字段与 `payload.build_payload()` 造出来的完全一致。"""
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
    """一个能过校验的最小 payload——结构照着真实的 payload 长，因为指纹覆盖的是**整个 payload**。"""
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


# 一、基本性质


def test_hash_is_64_hex_characters() -> None:
    assert _HEX64.match(_fp(_payload())), "指纹必须是 64 位十六进制（要存进 String(64)）"


def test_same_input_same_hash() -> None:
    """同输入同 hash —— 缓存能工作的前提；传两份**分别构造**的 payload（真实场景是两次请求各组装一份）。"""
    assert _fp(_payload()) == _fp(_payload())


def test_a_completely_different_dataset_changes_the_hash() -> None:
    """先验一下"改了东西哈希确实会变"，否则下面的用例可能全都是假通过。"""
    other = _payload()
    other["top_products"] = [_row(key="smzdm:999", title="完全无关的商品")]
    assert _fp(other) != _fp(_payload())


# 二、★ 顺序无关（不排的话，缓存会随查询计划的变化而失效）


def test_shuffling_products_does_not_change_the_hash() -> None:
    """★★ 商品顺序打乱，指纹必须不变——加索引、ORDER BY、分页都会让顺序变，
    不排序则"数据一字未改"缓存却全失效，排查时不会想到是查询顺序。"""
    payload = _payload()
    shuffled = _payload()
    shuffled["top_products"] = list(reversed(payload["top_products"]))

    # 先确认这两份数据确实不是同一个顺序，否则这个测试是空转的
    assert [r["key"] for r in payload["top_products"]] != [
        r["key"] for r in shuffled["top_products"]
    ]
    assert _fp(shuffled) == _fp(payload)


def test_shuffling_does_not_swallow_differences() -> None:
    """排序是按 `key` 排的，不是把行当成一个集合——交换两行的**内容**（key 不动）必须被发现。"""
    a = _payload()
    b = _payload()
    rows = b["top_products"]
    # 交换两行的价格与标题，key 保持原位
    rows[0]["price"], rows[1]["price"] = rows[1]["price"], rows[0]["price"]
    rows[0]["title"], rows[1]["title"] = rows[1]["title"], rows[0]["title"]

    assert _fp(a) != _fp(b)


# 三、★ 归一化：同一个价格的四种写法必须同值


@pytest.mark.parametrize(
    "variant",
    [199, 199.0, "199.00", "199", 198.99999999999997, " 199.0 "],
)
def test_price_representations_all_collapse_to_one_hash(variant: object) -> None:
    """★ 199 / 199.0 / "199.00" / 198.99999999999997 必须算出同一个指纹——
    它们在 `==` 意义上相等但 `json.dumps` 出来字节不同，不归一化缓存命中率接近零。"""
    payload = _payload()
    payload["top_products"][0]["price"] = variant
    baseline = _payload()
    baseline["top_products"][0]["price"] = 199.0

    assert _fp(payload) == _fp(baseline), f"{variant!r} 没有归一化到 199.0"


def test_sales_int_and_float_are_the_same() -> None:
    """销量 `1000` 和 `1000.0` 必须同值（同一个理由：字节不同；Mock 里是 int，JSON/pandas 读进来是 float）。"""
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
    """脏价格不抛异常，且**同样的脏值得到同样的指纹**（脏值全部归一成 None）。
    一条脏数据不该让整份报告生成不了；"要稳定"比"要正确"更重要，否则缓存彻底失效。"""
    for dirty in ["暂无报价", "面议", "1..2", "0x1F", "🙂", [], {}]:
        payload = _payload()
        payload["top_products"][0]["price"] = dirty
        first = _fp(payload)
        assert _HEX64.match(first), f"脏价格 {dirty!r} 没有算出合法指纹"

        again = _payload()
        again["top_products"][0]["price"] = dirty
        assert _fp(again) == first, f"脏价格 {dirty!r} 两次算出的指纹不同"

    # 脏值归一成 None，所以"价格脏掉"和"价格本来就是 None"是同一个指纹——
    # 两者对模型来说都是"没有价格"，报告不该因此重算。
    dirty = _payload()
    dirty["top_products"][0]["price"] = "暂无报价"
    none = _payload()
    none["top_products"][0]["price"] = None
    assert _fp(dirty) == _fp(none)


# 四、★ 该变的必须变（防"改了代码却命中旧缓存"）


def test_prompt_version_change_invalidates_everything(monkeypatch) -> None:
    """★★ 改 `PROMPT_VERSION`，指纹必变——这是唯一需要人肉维护的联动（改 prompts.py 就要改版本号）。"""
    before = _fp(_payload())
    # ★ 变异值必须**从当前值推导**，不能写死成 "v2"——
    #   写死的话，等真升到 v2 时这行 monkeypatch 成了空操作，测试变红而代码没错。
    monkeypatch.setattr(fp, "PROMPT_VERSION", fp.PROMPT_VERSION + "-mutated")
    assert _fp(_payload()) != before


def test_model_name_is_part_of_the_hash() -> None:
    """换模型必须失效缓存：同样的数据在 glm-4-flash 和 glm-4-plus 上
    会得到不同的结论，缓存不能跨模型复用。"""
    assert _fp(_payload(), model="glm-4-flash") != _fp(_payload(), model="glm-4-plus")


@pytest.mark.parametrize(
    "field, value",
    [
        ("title", "安克 充电宝 30000毫安"),
        ("price", 299.0),
        ("price_max", 259.0),
        ("sales", 9999),
        ("brand", "倍思"),
        ("platform", "天猫"),                  # ★ 销售平台（漏算过）
        ("category", "移动电源"),              # ★ 类目（漏算过）
        ("shop", "天猫超市"),
        ("points", 7),
    ],
)
def test_every_field_in_the_prompt_changes_the_hash(field: str, value: object) -> None:
    """★ 判据只有一条：**出现在 prompt 里的字段都必须进指纹**（`points` 也在 prompt 里）。

    这份手写清单覆盖不了"上游将来加的字段"——真正兜底的是下面那条
    从真实 payload 推导的用例。
    """
    changed = _payload()
    changed["top_products"][0][field] = value
    assert _fp(changed) != _fp(_payload()), f"改了 {field} 指纹却没变"


@pytest.mark.parametrize(
    "section",
    ["summary", "price_buckets", "shops", "keywords"],
)
def test_every_aggregate_section_changes_the_hash(section: str) -> None:
    """汇总区也必须进指纹——prompt 里的"整体均价"直接来自 summary，变了而不重算就是拿旧均价说新数据。"""
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


# 五、★ 不该变的绝不能变（防"缓存永远命中不了"）


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

    自动忽略等于默认这些字段可以随便加，会让缓存静默全失效；
    抛异常让加字段的人当场看见。三层都要查（时间戳最常出现在最内层的商品行里）。
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
    """★ 自增 `id` 是最典型的"会变但不影响结论"的字段——重新 seed 一次 id 全变，缓存全失效而数据一字未改。"""
    payload = _payload()
    payload["top_products"][0]["id"] = 42
    with pytest.raises(FingerprintError):
        _fp(payload)


def test_deeply_nested_timestamps_are_found() -> None:
    """★ 递归必须钻到底：`summary.price.updated_at` 这种三层嵌套也要抓到。"""
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
    """★★ 商品的**身份**以 `key`（`{source}:{source_id}`）进指纹——
    payload 里根本没有 `source_id` 字段；白名单会把多余的身份表示静默丢掉。"""
    baseline = _payload()
    other = _payload()
    other["top_products"][0]["key"] = "mock:999"

    assert _fp(other) != _fp(baseline), "key 变了指纹却不变——身份没进指纹"


def test_business_key_names_are_not_mistaken_for_the_id_blacklist() -> None:
    """★ `source_id` / `shop_id` 这类含 "id" 的合法字段名不能被误伤——
    匹配必须是**完全相等**而不是包含子串，否则分析接口直接 500。"""
    payload = _payload()
    payload["top_products"][0]["source_id"] = "183089533"
    payload["top_products"][0]["shop_id"] = 7
    payload["shops"][0]["shop_id"] = 7
    payload["summary"]["brand_missing_count"] = 0

    assert _HEX64.match(_fp(payload))


@pytest.mark.parametrize("variant", ["CreatedAt", "createdAt", "CREATED_AT", "created-at"])
def test_timestamp_name_variants_are_all_caught(variant: str) -> None:
    """★ 时间戳字段名的各种写法（CreatedAt / createdAt / CREATED_AT）都要拦住。

    只 lower() 不处理分隔符会漏掉驼峰——它恰恰是从 JS/JSON 抄来的默认形状，
    漏掉是静默的：字段悄悄进指纹，每采集一次缓存就全部失效一次。
    """
    payload = _payload()
    payload["top_products"][0][variant] = "2026-01-01T00:00:00"
    with pytest.raises(FingerprintError):
        _fp(payload)
    # 报错里要出现**原始的**字段名，否则排查时对不上号
    payload2 = _payload()
    payload2["top_products"][0][variant] = "2026-01-01T00:00:00"
    with pytest.raises(FingerprintError, match=variant):
        _fp(payload2)


def test_the_error_message_says_where_and_what_to_do() -> None:
    """报错信息要能照着做：指出路径和两种可能，而不是只说"payload 非法"（否则使用者只能翻源码）。"""
    payload = _payload()
    payload["top_products"][0]["last_crawled_at"] = "2026-01-01T00:00:00"
    with pytest.raises(FingerprintError) as excinfo:
        _fp(payload)

    message = str(excinfo.value)
    assert "last_crawled_at" in message, "报错里没说是哪个字段"
    assert "source_id" in message, "报错里没给可照做的建议"


def test_empty_product_list_is_allowed() -> None:
    """没有商品时不抛异常——空数据源由 reporter 拦在前面，但指纹函数对空输入也该有清楚的边界。"""
    payload = _payload(top_products=[])
    assert _HEX64.match(_fp(payload))


def test_missing_optional_sections_do_not_raise() -> None:
    """payload 缺了某个区（上游改了装配逻辑）时不该抛异常——键不存在和值为 None 都归一成空。"""
    assert _HEX64.match(_fp({"top_products": []}))


# 六、★ 端到端：真实的 payload 必须能算出指纹


def test_the_real_payload_is_fingerprint_safe(db, make_product) -> None:
    """★★ 用真实数据装配出来的 payload 必须能过 `_assert_fingerprint_safe`——
    守着"payload.py 加一个字段就静默让所有缓存失效"这件事的拦截点。"""
    make_product(sid="m-1", title="安克 充电宝 20000毫安", price=199.0, sales=1000, points=2)
    make_product(
        sid="m-2", title="绿联 充电宝 10000毫安", price=99.0, sales=2000, brand="绿联"
    )

    payload = build_payload(db, SourceFilter.all)
    assert _HEX64.match(build_fingerprint(payload, MODEL))


def test_the_real_payload_hash_is_stable_across_rebuilds(db, make_product) -> None:
    """★ 同样的数据装配两次得到同一个指纹——测的是**装配过程**有没有混进
    随时间/状态变化的东西（按 created_at 排序、utcnow() 进 summary 等），
    那正是"缓存莫名其妙不命中"的根源。两次之间 commit 一次，模拟两次真实请求。"""
    make_product(sid="m-1", title="安克 充电宝 20000毫安", price=199.0, sales=1000, points=3)
    make_product(sid="m-2", title="绿联 充电宝 10000毫安", price=99.0, sales=2000, brand="绿联")
    make_product(sid="m-3", title="倍思 氮化镓充电器 65W", price=149.0, sales=500, brand="倍思")

    first = build_fingerprint(build_payload(db, SourceFilter.all), MODEL)
    db.commit()
    second = build_fingerprint(build_payload(db, SourceFilter.all), MODEL)

    assert first == second


def _mutate(value: object) -> object:
    """给一个字段造一个**与它不同**的值。

    `None` 这一支返回的是**数字 1，不是字符串**：换成字符串的话，数值字段
    会被 `_round_price` 兜成 None，"价格没进指纹"和"脏值被归一"两种实现都会通过。
    """
    if value is None:
        return 1
    if isinstance(value, str):
        return f"{value}·改"
    return value + 1


def test_every_field_of_a_real_product_row_reaches_the_fingerprint(db, make_product) -> None:
    """★★ 真实商品行里的**每一个**键，改了都必须让指纹变。

    这条从上游推导而不是手写清单（手写清单和 `_row()`、白名单互为镜像，
    漏掉的字段三处同时不存在）。依据：`build_user_prompt()` 把整个 payload
    原样发出去，所以"行里有这个键"就等于"模型看得见这个键"。
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
    # ★ 收集完再断言，而不是撞到第一个就停：漏字段往往是一漏一批，
    #   逐个失败会让人修一个跑一次。
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
    """★ Mock 与真实数据必须算出不同的指纹，否则一个数据源的分析结果会被另一个复用，
    报告里的数字和屏幕上的过滤条件对不上。"""
    make_product(sid="m-1", title="安克 充电宝", price=199.0, sales=1000)
    make_product(
        sid="183089533", title="今日必买：京觅 猕猴桃", source="smzdm", price=17.5, sales=None
    )

    mock = build_fingerprint(build_payload(db, SourceFilter.mock), MODEL)
    smzdm = build_fingerprint(build_payload(db, SourceFilter.smzdm), MODEL)
    everything = build_fingerprint(build_payload(db, SourceFilter.all), MODEL)

    assert len({mock, smzdm, everything}) == 3, "三个数据源的指纹出现了重复"
