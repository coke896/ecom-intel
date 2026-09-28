"""GLM 客户端的解析、校验、重试 —— **用假 SDK 覆盖模型输出不可预测的那一段**。

================================================================================
为什么这个文件必须存在
================================================================================

整条分析链路上，只有这里处理的是**别人的输出**：

    我们写的代码  →  确定的行为   →  普通测试（同样的输入必然同样的输出）
    模型的输出    →  不确定       →  只能靠"喂各种畸形输入"来测

而这段代码在真实运行时的输入是**完全不可控**的。它会遇到：

    · 把 JSON 包在 ```json 代码块里（哪怕 prompt 里说了不要）
    · 前面加一句"好的，以下是分析："
    · 多返回一个 `"conclusion"` 字段
    · 漏掉 `headline`
    · 顶层返回一个数组而不是对象（`[...]` 而不是 `{...}`）
    · 直接返回一段散文
    · 空字符串

上面每一种都**真的会发生**（模型行为随采样波动，`temperature=0.3` 意味着
同一个 prompt 每次结果都不同）。这个文件就是把它们逐条钉住。

★ 还有一个更硬的理由：**当前开发环境没有配 GLM key**（没有 .env），
  所以"真的调一次模型"这条验证做不了。既然唯一能做的验证就是
  "把各种模型输出喂给这段代码看它怎么反应"，那就把它做扎实——
  否则这一层在生产里的行为是完全没有依据的。

================================================================================
三层防线，分别测什么
================================================================================

    ① `_extract_json`  —— 只管"能不能拿到一个 JSON 对象"（格式问题）
    ② `_validate`      —— 只管"该有的字段在不在"（内容问题）
    ③ `analyze` 的重试 —— 只管"这两类失败要不要重来一次"

★ ①和②必须分开测：合在一起的话，"模型返回了 `{}`"会被当成"解析成功"，
  然后一路走到落库，最后在渲染报告时 KeyError——那时候已经离现场很远了。

================================================================================
假 SDK 是怎么接上去的
================================================================================

`GlmClient._get_client()` 会做两件事：检查 key、构造 `OpenAI(...)`。
测试里两件都不想要（不想依赖机器上有没有配 key，更不想联网）。

所以直接给 `client._client` 赋值——`_get_client()` 开头就是
`if self._client is None:`，**塞进去之后它原样返回**，检查全被跳过。

★ 这是"用私有属性"的少数正当场合：要测的是 `analyze()` 的重试循环，
  而它唯一的注入点在 `_get_client()` 之后。真要把它改成公开可注入的话，
  产品代码里就会多出一个"只为测试存在"的参数——
  那和 `reporter.generate_report(client=...)` 的性质不同：
  后者的理由是"缓存正确性**只能**靠替换客户端来验证"，是必要条件。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.analysis.glm_client import AnalysisError, GlmClient, _extract_json, _validate


# ---------------------------------------------------------------------------
# 一个结构合法的报告（各用例在它基础上做手脚）
# ---------------------------------------------------------------------------

VALID_REPORT: dict = {
    "headline": "该品类价格集中在 100~200 元",
    "sections": [
        {"title": "价格结构", "finding": "价格带较窄。", "evidence": "中位数 149 元"},
        {"title": "品牌格局", "finding": "头部集中在三家。", "evidence": "见 top_brands"},
    ],
    "opportunities": ["补一个 200 元以上的档位"],
    "risks": ["样本偏向头部"],
    "data_limits": ["这批数据没有销量，无法判断畅销程度"],
}


def _json_text(report: dict | None = None, **overrides) -> str:
    """把报告序列化成模型会输出的那种字符串。"""
    payload = dict(report or VALID_REPORT)
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 假 SDK
# ---------------------------------------------------------------------------


def _response(text: str, tokens: int = 42):
    """伪造一个 OpenAI 响应对象。

    ★ 只造 `choices[0].message.content` 和 `usage.total_tokens` 两个属性，
      因为 `analyze()` 只读这两个。造一整个 SDK 响应对象会引入一堆
      无关字段，而它们一旦变了就会让测试红——而那时真正重要的东西没变。
      测试替身应该只实现**被用到的那部分接口**。
    """
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(total_tokens=tokens),
    )


class _FakeCompletions:
    """按脚本逐次返回（或抛异常）。最后一次会被重复使用。"""

    def __init__(self, script: list) -> None:
        self.script = script
        self.calls = 0
        self.last_kwargs: dict | None = None

    def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        item = self.script[min(self.calls - 1, len(self.script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


class _FakeSdk:
    def __init__(self, script: list) -> None:
        self.chat = SimpleNamespace(completions=_FakeCompletions(script))


@pytest.fixture()
def make_client(monkeypatch):
    """造一个接上假 SDK 的 `GlmClient`。

    ★ 不碰 settings、不读机器上的 .env：`client._client` 一塞进去，
      `_get_client()` 就直接返回它，key 检查和联网都不会发生。
      这样这个文件的测试结果**与运行它的机器无关**。
    """

    def _make(script: list, **kwargs) -> tuple[GlmClient, _FakeSdk]:
        client = GlmClient(api_key="fake-key-not-real-000000", **kwargs)
        sdk = _FakeSdk(script)
        client._client = sdk
        return client, sdk

    return _make


# ===========================================================================
# 一、`_extract_json`：只管格式
# ===========================================================================


def test_plain_json_is_parsed() -> None:
    """模型听话时的正常路径。"""
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_fenced_json_is_parsed() -> None:
    """★ 最常见的不听话形式：包在 ```` ```json ```` 里。

    哪怕 prompt 里明确写了"只输出 JSON，不要用 markdown 代码块包裹"，
    它还是经常这么干——这是模型的训练习惯，不是没读懂要求。
    挡在这里的成本是三行代码，失败一次的代价是重新调一次 API。
    """
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_fenced_json_without_a_language_tag_is_parsed() -> None:
    """```` ``` ```` 后面不带 `json` 也要认。"""
    assert _extract_json('```\n{"a": 1}\n```') == {"a": 1}


def test_json_with_a_preamble_is_parsed() -> None:
    """★ 前面带一句客套话："好的，以下是分析："。

    ★ 这一级降级（截取第一个 `{` 到最后一个 `}`）是三级里最"脏"的一条，
      但它救回来的次数最多——模型在被要求"只输出 JSON"之后，
      仍然有相当比例会加一句开场白。
    """
    assert _extract_json('好的，以下是分析：\n{"a": 1}\n希望有帮助。') == {"a": 1}


def test_json_with_trailing_commentary_is_parsed() -> None:
    """后面带解释也要认（同一级降级的另一半）。"""
    assert _extract_json('{"a": 1}\n\n以上就是全部结论。') == {"a": 1}


def test_an_empty_string_raises() -> None:
    """★ 空内容要报明确的错，不能走到 `json.loads("")` 那儿报一个
    与真实原因无关的 `JSONDecodeError`。"""
    with pytest.raises(AnalysisError, match="空内容"):
        _extract_json("")
    with pytest.raises(AnalysisError, match="空内容"):
        _extract_json("   \n\t ")


def test_a_top_level_array_raises() -> None:
    """★ 顶层是数组而不是对象。

    ★ 它**能**被 JSON 解析器成功解析——所以这一条守的是
      "解析成功 ≠ 拿到了想要的东西"：
      报告要的是一个对象（有 headline、sections 这些键），
      拿到 `[...]` 之后按 dict 用会抛 `TypeError: list indices must be integers`，
      而那个报错和"模型返回了数组"这个真实原因毫无关系。
    """
    with pytest.raises(AnalysisError, match="顶层是 list"):
        _extract_json('["a", "b"]')


def test_a_bare_number_raises() -> None:
    with pytest.raises(AnalysisError):
        _extract_json("42")


def test_prose_only_raises_and_includes_the_raw_text() -> None:
    """★★ 完全不是 JSON 时，报错里必须带上**原始输出**。

    ★ 这一条的价值全在"带上原文"：
      报告内容不对时，第一个要回答的问题是"是模型写歪了，还是我们解析歪了"。
      没有原文就只能靠猜——而重新调一次 API 得到的是**另一次采样**，
      根本不能用来复现问题。

    ★ 这就是 `GlmResult.raw_text` 存在的理由，也是这里截取 300 字的原因：
      完整输出可能几千字，全塞进日志会把日志淹掉；
      但开头 300 字足够看出"它到底写了什么"。
    """
    with pytest.raises(AnalysisError) as excinfo:
        _extract_json("我无法完成这个请求，因为数据不足。")

    message = str(excinfo.value)
    assert "我无法完成这个请求" in message, "报错里没有模型的原始输出"
    assert "三种方式" in message, "没说清楚已经尝试过哪些解析方式"


# ===========================================================================
# 二、`_validate`：只管内容
# ===========================================================================


def test_a_valid_report_passes_and_keeps_its_fields() -> None:
    out = _validate(VALID_REPORT)

    assert out["headline"] == VALID_REPORT["headline"]
    assert len(out["sections"]) == 2
    assert out["sections"][0]["evidence"] == "中位数 149 元"
    assert out["data_limits"] == VALID_REPORT["data_limits"]


def test_an_empty_dict_raises() -> None:
    """★ `{}` 是**能**被 JSON 解析的——所以它必须在这里被拦住。

    这正是"解析和校验必须分开"的理由：`{}` 走到落库那一层之后，
    报告页渲染时才会发现没有 headline，而那时已经离现场很远了。
    """
    with pytest.raises(AnalysisError, match="不符合报告结构"):
        _validate({})


@pytest.mark.parametrize("missing", ["headline", "sections"])
def test_missing_required_fields_raise(missing: str) -> None:
    """`headline` 和 `sections` 是报告的骨架，缺了就没法渲染。"""
    bad = {k: v for k, v in VALID_REPORT.items() if k != missing}
    with pytest.raises(AnalysisError, match="不符合报告结构"):
        _validate(bad)


def test_an_empty_section_list_raises() -> None:
    """★ `sections` 有下限 1。

    ★ 但**不能**设成下限 3（prompt 里要求的是 3~5 段）：
      真实数据稀少时（比如只有一个商品），模型只写得出 1 段，
      卡在 3 会让整次分析失败——而"数据少所以只有一段"是完全合理的结果。
      下限交给 prompt 去引导，schema 只管"别是空的"。
    """
    with pytest.raises(AnalysisError):
        _validate({**VALID_REPORT, "sections": []})


def test_a_section_missing_evidence_is_accepted() -> None:
    """★ `evidence` 是**必填但可为空**的，这个区别是有意的。

    把它设成"必填且非空"的话，模型在确实没有数字可引时
    会**编一个数字**来填满这个字段——那是比留空糟糕得多的结果。
    所以：字段必须在（结构上提醒它这是要求），内容可以为空（给它退路）。
    """
    out = _validate({**VALID_REPORT, "sections": [{"title": "价格结构", "finding": "较窄。"}]})

    assert out["sections"][0]["evidence"] == ""


def test_a_section_missing_finding_raises() -> None:
    """`finding` 是段落的主体，缺了就只剩一个标题。"""
    with pytest.raises(AnalysisError):
        _validate({**VALID_REPORT, "sections": [{"title": "价格结构"}]})


@pytest.mark.parametrize("extra", ["opportunities", "risks", "data_limits"])
def test_missing_optional_lists_default_to_empty(extra: str) -> None:
    """★ 三组列表可以缺——强迫模型在"确实没有风险"时也要硬凑三条，
    只会得到三条废话。缺了就是空列表。"""
    bad = {k: v for k, v in VALID_REPORT.items() if k != extra}
    assert _validate(bad)[extra] == []


def test_unknown_fields_are_ignored() -> None:
    """★ 模型多返回一个字段**不该让整份报告失败**。

    ★ 它会自己加 `"conclusion"` / `"summary"` 这类字段，这是模型的常见行为，
      不是错误。`extra="ignore"` 就是为了这个——丢掉就好。
      用 `extra="forbid"` 的话，报告会因为一个我们根本用不上的字段
      而整份作废，然后触发一次重试，然后大概率**再失败一次**
      （因为模型还是会加那个字段）。
    """
    out = _validate({**VALID_REPORT, "conclusion": "随便写的", "confidence": 0.9})

    assert "conclusion" not in out
    assert out["headline"] == VALID_REPORT["headline"]


# ===========================================================================
# 三、端到端：`analyze()` 的两次尝试
# ===========================================================================


def test_a_good_response_returns_the_report_and_the_token_count(make_client) -> None:
    client, sdk = make_client([_response(_json_text(), tokens=1234)])

    result = client.analyze({"summary": {"product_count": 1}})

    assert sdk.chat.completions.calls == 1
    assert result.content["headline"] == VALID_REPORT["headline"]
    assert result.tokens_used == 1234
    assert result.raw_text == _json_text()


def test_the_raw_text_is_preserved_verbatim(make_client) -> None:
    """★ `raw_text` 必须是**原样**的模型输出，不能被解析过程清洗过。

    ★ 它的用途是排查"是模型写歪了还是解析歪了"——如果它已经被
      剥掉了代码块、截掉了前后文，那这个问题的答案就被毁掉了。
    """
    messy = '好的：\n```json\n' + _json_text() + "\n```\n以上。"
    client, _ = make_client([_response(messy)])

    assert client.analyze({}).raw_text == messy


def test_a_bad_first_response_is_retried_once_and_can_succeed(make_client) -> None:
    """★ 第一次返回散文，第二次返回合法 JSON → 成功，且**恰好调用两次**。

    ★ 为什么值得重试：`temperature=0.3` 意味着同一个 prompt 每次采样结果不同，
      JSON 格式错在温度不为 0 时**确实可能是偶发的**，
      重新采样一次有很大概率就好了。

    ★ 断言 `calls == 2` 而不是 `>= 2`：这个循环的上界必须是确定的。
      写成循环直到成功的话，模型持续不听话时会一直重试下去，
      每一次都是一次真实的付费调用。
    """
    client, sdk = make_client([_response("我无法完成这个请求。"), _response(_json_text())])

    result = client.analyze({})

    assert sdk.chat.completions.calls == 2
    assert result.content["headline"] == VALID_REPORT["headline"]


def test_a_bad_first_response_that_is_bad_for_content_reasons_is_also_retried(
    make_client,
) -> None:
    """★ 缺字段（内容问题）也会触发重试，不只是 JSON 格式错。

    ★ 这两类失败由不同的代码抛出（`_extract_json` vs `_validate`），
      但重试逻辑**必须**把它们一视同仁——否则"模型返回了合法 JSON
      但缺少 sections"这种情况就成了不可恢复的失败，
      而它同样可能只是这一次采样跑偏了。
    """
    client, sdk = make_client([_response(_json_text({"headline": "只有标题"})), _response(_json_text())])

    result = client.analyze({})

    assert sdk.chat.completions.calls == 2
    assert len(result.content["sections"]) == 2


def test_two_bad_responses_raise_and_stop_at_two_calls(make_client) -> None:
    """★★ 连续两次都不行 → 抛异常，**且只调用两次**。

    ★ 为什么**只**重试一次：连续两次都拿不到合法 JSON，
      说明问题在 prompt 或 schema 上，第三次采样大概率还是错的。
      这时候正确的动作是去**改 prompt**，而不是继续等——
      把所有失败都变成"多试几次"，只会让真正的 prompt 缺陷藏得更久，
      而且每一次尝试都在花钱。

    ★ `calls == 2` 是这条测试的核心断言：它同时钉住了
      "重试了"（不是 1）和"没有无限重试"（不是 3、4……）。
    """
    client, sdk = make_client([_response("第一次也是散文"), _response("第二次还是散文")])

    with pytest.raises(AnalysisError) as excinfo:
        client.analyze({})

    assert sdk.chat.completions.calls == 2, "重试次数不是恰好一次"
    # 报错要带上原始输出，方便判断是不是 prompt 的问题
    assert "第二次还是散文" in str(excinfo.value)


def test_an_sdk_failure_is_translated_and_not_retried(make_client) -> None:
    """★ SDK 层的异常要翻译成 `AnalysisError`，而且**不走重试循环**。

    ★ 为什么网络错误不该在这里重试：那件事由 SDK 自己的 `max_retries`
      负责（它做指数退避，比我们手写的好）。两层都重试的话，
      实际尝试次数是**乘起来**的（3 × 2 = 6 次），
      而"最多等多久"这个数字就从 120 秒变成了没法预测的东西。

    ★ 翻译成 `AnalysisError` 的理由在 API 层：只有它能被翻译成
      "去配 key"这样一句能照着做的话，而 openai 的原始报错
      （`AuthenticationError: Error code: 401`）会被读成"这程序坏了"。
    """
    boom = RuntimeError("connection reset by peer")
    client, sdk = make_client([boom, _response(_json_text())])

    with pytest.raises(AnalysisError) as excinfo:
        client.analyze({})

    assert sdk.chat.completions.calls == 1, "网络层错误不该走本模块的重试循环"
    message = str(excinfo.value)
    assert "调用 GLM 失败" in message
    assert "RuntimeError" in message, "没有带上原始异常类型，排查时无法判断是哪一类失败"


def test_the_request_asks_for_json_output(make_client) -> None:
    """★ `response_format` 要显式要 JSON。

    ★ 但它只是**提示**不是保证：不同兼容端点的支持程度不一样，
      所以 `_extract_json` 的三级降级仍然必须有。
      把 `response_format` 当保险丝，不是当开关。
    """
    client, sdk = make_client([_response(_json_text())])
    client.analyze({})

    assert sdk.chat.completions.last_kwargs["response_format"] == {"type": "json_object"}


def test_the_request_carries_both_messages(make_client) -> None:
    """★ system 和 user 两条消息都要发出去。

    ★ 少发 system 的话，prompt 里那六条硬规则（"只用数据里出现过的数字"
      "区分整体和样本""宁可说看不出来"）全部失效，
      而**报文本身看起来完全正常**——模型会照常写出一份通顺但开始编数字的报告。
    """
    client, sdk = make_client([_response(_json_text())])
    client.analyze({"summary": {"product_count": 1}})

    messages = sdk.chat.completions.last_kwargs["messages"]
    roles = [m["role"] for m in messages]

    assert roles == ["system", "user"]
    assert "只使用数据里出现过的数字" in messages[0]["content"]
    # user 消息里带着 payload（紧凑 JSON）
    assert '"product_count":1' in messages[1]["content"], "payload 没有被序列化进 user 消息"
