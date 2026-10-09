"""GLM 客户端的解析、校验、重试 —— 用假 SDK 覆盖模型输出不可预测的那一段。

输入完全不可控：模型会把 JSON 包代码块、加一句开场白、多返回字段、漏掉
headline、顶层给数组或直接给散文，这里逐条钉住。三层防线分开测：
`_extract_json` 只管格式（能不能拿到 JSON 对象）、`_validate` 只管字段在不在、
`analyze` 的重试管"这两类失败要不要重来"——合起来测的话，`{}` 会被当成解析成功。
假 SDK 的接法：直接给 `client._client` 赋值，`_get_client()` 见到非 None
就原样返回，key 检查和联网全被跳过，测试结果与机器无关。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.analysis.glm_client import AnalysisError, GlmClient, _extract_json, _validate


# 一个结构合法的报告（各用例在它基础上做手脚）

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


# 假 SDK


def _response(text: str, tokens: int = 42):
    """伪造一个 OpenAI 响应对象——只造 `analyze()` 真正读到的两个属性：
    `choices[0].message.content` 和 `usage.total_tokens`。"""
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
    """造一个接上假 SDK 的 `GlmClient`——不碰 settings、不读 .env，key 检查和联网都不会发生。"""

    def _make(script: list, **kwargs) -> tuple[GlmClient, _FakeSdk]:
        client = GlmClient(api_key="fake-key-not-real-000000", **kwargs)
        sdk = _FakeSdk(script)
        client._client = sdk
        return client, sdk

    return _make


# 一、`_extract_json`：只管格式


def test_plain_json_is_parsed() -> None:
    """模型听话时的正常路径。"""
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_fenced_json_is_parsed() -> None:
    """★ 最常见的不听话形式：包在 ```` ```json ```` 里，哪怕 prompt 明确说了不要——这是模型的训练习惯。"""
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_fenced_json_without_a_language_tag_is_parsed() -> None:
    """```` ``` ```` 后面不带 `json` 也要认。"""
    assert _extract_json('```\n{"a": 1}\n```') == {"a": 1}


def test_json_with_a_preamble_is_parsed() -> None:
    """★ 前面带一句客套话："好的，以下是分析："——模型被要求"只输出 JSON"后仍经常加开场白。"""
    assert _extract_json('好的，以下是分析：\n{"a": 1}\n希望有帮助。') == {"a": 1}


def test_json_with_trailing_commentary_is_parsed() -> None:
    """后面带解释也要认（同一级降级的另一半）。"""
    assert _extract_json('{"a": 1}\n\n以上就是全部结论。') == {"a": 1}


def test_an_empty_string_raises() -> None:
    """★ 空内容要报明确的错，不能走到 `json.loads("")` 报一个与真实原因无关的 `JSONDecodeError`。"""
    with pytest.raises(AnalysisError, match="空内容"):
        _extract_json("")
    with pytest.raises(AnalysisError, match="空内容"):
        _extract_json("   \n\t ")


def test_a_top_level_array_raises() -> None:
    """★ 顶层是数组时 JSON 解析**成功**了，但报告要的是对象——守的是"解析成功 ≠ 拿到了想要的东西"。"""
    with pytest.raises(AnalysisError, match="顶层是 list"):
        _extract_json('["a", "b"]')


def test_a_bare_number_raises() -> None:
    with pytest.raises(AnalysisError):
        _extract_json("42")


def test_prose_only_raises_and_includes_the_raw_text() -> None:
    """★★ 完全不是 JSON 时，报错里必须带上**原始输出**（截取开头 300 字）——
    没有原文就只能靠猜，而重新调一次 API 得到的是另一次采样，无法复现问题。"""
    with pytest.raises(AnalysisError) as excinfo:
        _extract_json("我无法完成这个请求，因为数据不足。")

    message = str(excinfo.value)
    assert "我无法完成这个请求" in message, "报错里没有模型的原始输出"
    assert "三种方式" in message, "没说清楚已经尝试过哪些解析方式"


# 二、`_validate`：只管内容


def test_a_valid_report_passes_and_keeps_its_fields() -> None:
    out = _validate(VALID_REPORT)

    assert out["headline"] == VALID_REPORT["headline"]
    assert len(out["sections"]) == 2
    assert out["sections"][0]["evidence"] == "中位数 149 元"
    assert out["data_limits"] == VALID_REPORT["data_limits"]


def test_an_empty_dict_raises() -> None:
    """★ `{}` 能被 JSON 解析，所以必须在校验这里拦住——这正是"解析和校验必须分开"的理由。"""
    with pytest.raises(AnalysisError, match="不符合报告结构"):
        _validate({})


@pytest.mark.parametrize("missing", ["headline", "sections"])
def test_missing_required_fields_raise(missing: str) -> None:
    """`headline` 和 `sections` 是报告的骨架，缺了就没法渲染。"""
    bad = {k: v for k, v in VALID_REPORT.items() if k != missing}
    with pytest.raises(AnalysisError, match="不符合报告结构"):
        _validate(bad)


def test_an_empty_section_list_raises() -> None:
    """★ `sections` 下限是 1 而不是 3（prompt 要求 3~5 段）：数据稀少时模型只写得出 1 段，
    卡在 3 会让整次分析失败；下限交给 prompt 引导，schema 只管"别是空的"。"""
    with pytest.raises(AnalysisError):
        _validate({**VALID_REPORT, "sections": []})


def test_a_section_missing_evidence_is_accepted() -> None:
    """★ `evidence` 必填但可为空：设成"非空"的话，模型没有数字可引时会**编一个数字**来填。"""
    out = _validate({**VALID_REPORT, "sections": [{"title": "价格结构", "finding": "较窄。"}]})

    assert out["sections"][0]["evidence"] == ""


def test_a_section_missing_finding_raises() -> None:
    """`finding` 是段落的主体，缺了就只剩一个标题。"""
    with pytest.raises(AnalysisError):
        _validate({**VALID_REPORT, "sections": [{"title": "价格结构"}]})


@pytest.mark.parametrize("extra", ["opportunities", "risks", "data_limits"])
def test_missing_optional_lists_default_to_empty(extra: str) -> None:
    """★ 三组列表可以缺——强迫模型在"确实没有风险"时硬凑只会得到废话，缺了就是空列表。"""
    bad = {k: v for k, v in VALID_REPORT.items() if k != extra}
    assert _validate(bad)[extra] == []


def test_unknown_fields_are_ignored() -> None:
    """★ 模型多返回字段（`conclusion` / `summary` 这类）不该让整份报告失败——
    用 `extra="forbid"` 会整份作废、重试、然后大概率再失败一次。"""
    out = _validate({**VALID_REPORT, "conclusion": "随便写的", "confidence": 0.9})

    assert "conclusion" not in out
    assert out["headline"] == VALID_REPORT["headline"]


# 三、端到端：`analyze()` 的两次尝试


def test_a_good_response_returns_the_report_and_the_token_count(make_client) -> None:
    client, sdk = make_client([_response(_json_text(), tokens=1234)])

    result = client.analyze({"summary": {"product_count": 1}})

    assert sdk.chat.completions.calls == 1
    assert result.content["headline"] == VALID_REPORT["headline"]
    assert result.tokens_used == 1234
    assert result.raw_text == _json_text()


def test_the_raw_text_is_preserved_verbatim(make_client) -> None:
    """★ `raw_text` 必须是**原样**的模型输出——它的用途是排查"是模型写歪了还是解析歪了"。"""
    messy = '好的：\n```json\n' + _json_text() + "\n```\n以上。"
    client, _ = make_client([_response(messy)])

    assert client.analyze({}).raw_text == messy


def test_a_bad_first_response_is_retried_once_and_can_succeed(make_client) -> None:
    """★ 第一次散文、第二次合法 JSON → 成功，且**恰好调用两次**。
    温度不为 0 时格式错可能是偶发的，重采样一次大概率就好；但上界必须确定，
    每次重试都是一次真实的付费调用。"""
    client, sdk = make_client([_response("我无法完成这个请求。"), _response(_json_text())])

    result = client.analyze({})

    assert sdk.chat.completions.calls == 2
    assert result.content["headline"] == VALID_REPORT["headline"]


def test_a_bad_first_response_that_is_bad_for_content_reasons_is_also_retried(
    make_client,
) -> None:
    """★ 缺字段（内容问题）也会触发重试——两类失败由不同代码抛出，但重试逻辑必须一视同仁。"""
    client, sdk = make_client([_response(_json_text({"headline": "只有标题"})), _response(_json_text())])

    result = client.analyze({})

    assert sdk.chat.completions.calls == 2
    assert len(result.content["sections"]) == 2


def test_two_bad_responses_raise_and_stop_at_two_calls(make_client) -> None:
    """★★ 连续两次都不行 → 抛异常且**只调用两次**：连续失败说明问题在 prompt 或 schema 上，
    第三次采样大概率还是错的；`calls == 2` 同时钉住"重试了"和"没有无限重试"。"""
    client, sdk = make_client([_response("第一次也是散文"), _response("第二次还是散文")])

    with pytest.raises(AnalysisError) as excinfo:
        client.analyze({})

    assert sdk.chat.completions.calls == 2, "重试次数不是恰好一次"
    # 报错要带上原始输出，方便判断是不是 prompt 的问题
    assert "第二次还是散文" in str(excinfo.value)


def test_an_sdk_failure_is_translated_and_not_retried(make_client) -> None:
    """★ SDK 异常翻译成 `AnalysisError` 且**不走重试循环**。
    网络重试归 SDK 自己的 `max_retries`（指数退避），两层都重试会让尝试次数相乘、
    等待时间无法预测；翻译是为了 API 层能给出"去配 key"这样能照做的提示。"""
    boom = RuntimeError("connection reset by peer")
    client, sdk = make_client([boom, _response(_json_text())])

    with pytest.raises(AnalysisError) as excinfo:
        client.analyze({})

    assert sdk.chat.completions.calls == 1, "网络层错误不该走本模块的重试循环"
    message = str(excinfo.value)
    assert "调用 GLM 失败" in message
    assert "RuntimeError" in message, "没有带上原始异常类型，排查时无法判断是哪一类失败"


def test_the_request_asks_for_json_output(make_client) -> None:
    """★ `response_format` 只是**提示**不是保证（不同兼容端点支持程度不一），三级降级仍然必须有。"""
    client, sdk = make_client([_response(_json_text())])
    client.analyze({})

    assert sdk.chat.completions.last_kwargs["response_format"] == {"type": "json_object"}


def test_the_request_carries_both_messages(make_client) -> None:
    """★ system 和 user 两条消息都要发——少发 system 后 prompt 里的硬规则全部失效，
    而报文本身看起来完全正常，模型会写出一份通顺但开始编数字的报告。"""
    client, sdk = make_client([_response(_json_text())])
    client.analyze({"summary": {"product_count": 1}})

    messages = sdk.chat.completions.last_kwargs["messages"]
    roles = [m["role"] for m in messages]

    assert roles == ["system", "user"]
    assert "只使用数据里出现过的数字" in messages[0]["content"]
    # user 消息里带着 payload（紧凑 JSON）
    assert '"product_count":1' in messages[1]["content"], "payload 没有被序列化进 user 消息"
