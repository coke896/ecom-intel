"""分析接口的 HTTP 层测试：缓存逻辑之上的**翻译层**（缓存逻辑本身在
test_reporter_cache.py）。要翻译的三件事，每一件都能单独错：
`?force=1` 字符串 → True（译错 = "点重新生成没反应"，而缓存逻辑全绿）；
ReportOutcome → JSON（译错 = 空态 500 或字段名对不上前端）；
AnalysisError → HTTP 状态码（译错 = 503 变 500）。第 1 和第 3 条只有在
这一层能测——`reporter` 收到的永远是真正的 bool 和真正的异常。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.analysis import reporter
from app.analysis.glm_client import AnalysisError, GlmResult
from app.api import analysis as analysis_api
from app.db.base import get_db
from app.main import app


@pytest.fixture()
def client(db):
    """把接口的数据库依赖换成测试会话（和 test_api_smoke.py 里同一个做法）。"""
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        # ★ dependency_overrides 挂在**全局 app 对象**上，必须清理——否则下
        #   一个测试文件会拿到这个已经关闭的会话。
        app.dependency_overrides.clear()


class FakeClient:
    """数调用次数的假客户端，内容每次都不同（理由见 test_reporter_cache.py）。"""

    model = "fake-model"

    def __init__(self) -> None:
        self.calls = 0

    def analyze(self, payload: dict) -> GlmResult:
        self.calls += 1
        return GlmResult(
            content={
                "headline": f"第 {self.calls} 次生成的报告",
                "sections": [
                    {"title": "价格结构", "finding": "价格带较窄。", "evidence": "中位数 149"}
                ],
                "opportunities": ["补一个高价位段"],
                "risks": [],
                "data_limits": ["没有销量数据"],
            },
            tokens_used=100 * self.calls,
            raw_text="{}",
        )


@pytest.fixture()
def fake(monkeypatch) -> FakeClient:
    """把接口用的 `generate_report` 换成注入了假客户端的版本。"""
    fake_client = FakeClient()

    def patched(db, source, *, force=False):
        return reporter.generate_report(db, source, force=force, client=fake_client)

    monkeypatch.setattr(analysis_api, "generate_report", patched)
    return fake_client


def _seed(make_product) -> None:
    make_product(sid="m-1", title="安克 充电宝 20000毫安", price=199.0, sales=1000, points=3)
    make_product(sid="m-2", title="绿联 充电宝 10000毫安", price=99.0, sales=2000, brand="绿联")


# 一、空态：200 + notice（不是 404、不是 500）


def test_latest_on_a_fresh_database_returns_200_with_a_notice(client) -> None:
    """★ 还没有报告时返回 200，不是 404——全项目的空态契约："这里现在没有东西"
    是**正常的答案**；改成 404 的话前端要为正常状态写错误处理，而且那个分支容易
    被 `apiGet` 当错误抛出去弹 alert。"""
    r = client.get("/api/analysis/latest")

    assert r.status_code == 200
    body = r.json()
    assert body["notice"]
    assert body["content"]["sections"] == []
    assert body["from_cache"] is False


def test_latest_never_returns_404_even_with_no_data_at_all(client) -> None:
    """空库 + 空报告表：仍然 200。钉住"不许顺手改成 404"——哪天有人觉得
    "没数据就该 404"，这里会红。"""
    assert client.get("/api/analysis/latest").status_code == 200


def test_run_on_an_empty_source_returns_a_notice_and_does_not_call_the_model(
    client, fake
) -> None:
    """★ 空数据源：200 + 解释 + **调用次数为 0**。
    ★ `tokens_used` 必须是 None 而不是 0：0 会被读成"调用了但没消耗
      token"，而事实是**根本没调**。
    """
    r = client.post("/api/analysis/run")

    assert r.status_code == 200
    body = r.json()
    assert body["notice"] == reporter.NO_DATA_NOTICE
    assert body["tokens_used"] is None
    assert body["content"]["headline"] == ""
    assert fake.calls == 0


def test_an_empty_source_is_not_an_error_status(client, fake) -> None:
    """空数据源不能返回 4xx/5xx。**前端会把它当成"系统坏了"。**"""
    assert client.post("/api/analysis/run").status_code == 200


# 二、成功路径：响应的形状


def test_run_returns_the_report_shape_the_frontend_renders(client, make_product, fake) -> None:
    """★ 成功响应的字段必须**恰好**是前端要渲染的那几个：少一个键的话前端渲染到
    那一段会拿到 undefined，而它在模板字符串里会**静默渲染成字符串 "undefined"**
    ——页面上出现一行字面量，不报错。"""
    _seed(make_product)
    body = client.post("/api/analysis/run").json()

    assert sorted(body["content"]) == [
        "data_limits",
        "headline",
        "opportunities",
        "risks",
        "sections",
    ]
    assert body["content"]["headline"] == "第 1 次生成的报告"
    assert body["content"]["sections"][0]["title"] == "价格结构"
    # 元信息
    assert body["from_cache"] is False
    assert body["notice"] is None
    assert body["model"] == "fake-model"
    assert len(body["fingerprint"]) == 64
    assert body["created_at"]


def test_the_second_run_reports_that_it_came_from_cache(client, make_product, fake) -> None:
    """★ 第二次调用要能**从响应上看出来**它是缓存来的——这就是 `from_cache` 存在
    的理由：没有它，"缓存命中"和"重新分析"返回的东西一模一样，你说"你看它秒回"
    ——**但证明不了**。"""
    _seed(make_product)
    first = client.post("/api/analysis/run").json()
    second = client.post("/api/analysis/run").json()

    assert fake.calls == 1
    assert second["from_cache"] is True
    assert second["content"] == first["content"]
    assert second["fingerprint"] == first["fingerprint"]


def test_a_cache_hit_still_reports_the_token_cost(client, make_product, fake) -> None:
    """★ 命中缓存时 `tokens_used` 仍然是**当初那个数字**，不是 None：它描述"这份
    报告花了多少 token"，不是"这次请求花了多少"（后者由 `from_cache` 回答）。抹成
    None 的话"这份报告的成本"就消失了，而它正是判断"值不值得花 token"的依据。"""
    _seed(make_product)
    client.post("/api/analysis/run")
    second = client.post("/api/analysis/run").json()

    assert second["from_cache"] is True
    assert second["tokens_used"] == 100


def test_force_as_a_query_string_is_actually_parsed_as_true(client, make_product, fake) -> None:
    """★ `?force=1` 这个**字符串**要真的被解析成 True——只有这一层能测：`reporter`
    收到的永远是真正的 bool。译错的表现特别隐蔽：点「重新生成」→ 后端当成 False →
    命中缓存 → 秒回同一份报告 → **看起来像按钮坏了**；而 reporter_cache 的 force
    用例全是绿的（直接传 `force=True`，绕过了解析）。"""
    _seed(make_product)
    first = client.post("/api/analysis/run").json()
    forced = client.post("/api/analysis/run?force=1").json()

    assert fake.calls == 2, "?force=1 没有被解析成 True"
    assert forced["from_cache"] is False
    assert forced["content"]["headline"] != first["content"]["headline"]


@pytest.mark.parametrize("raw", ["1", "true", "True", "yes", "on"])
def test_the_usual_truthy_spellings_all_work(client, make_product, fake, raw: str) -> None:
    """★ 常见的"真值写法"都要能用：前端写 `force=1`，人手 curl 写 `force=true`，
    记事本里记 `force=yes`；解析器只认一种的话另外两种会**静默当成 False**——
    FastAPI/pydantic 本来都认，这里钉住防止将来自己解析时悄悄变窄。"""
    _seed(make_product)
    client.post("/api/analysis/run")

    assert client.post(f"/api/analysis/run?force={raw}").json()["from_cache"] is False
    assert fake.calls == 2


def test_force_false_uses_the_cache(client, make_product, fake) -> None:
    """反向：`force=0` 要走缓存——一个"永远重新生成"的实现也能让上面
    几条全绿。"""
    _seed(make_product)
    client.post("/api/analysis/run")

    assert client.post("/api/analysis/run?force=0").json()["from_cache"] is True
    assert fake.calls == 1


def test_latest_returns_the_newest_report(client, make_product, fake) -> None:
    """`latest` 是页面加载时调的第一个接口。"""
    _seed(make_product)
    client.post("/api/analysis/run")
    forced = client.post("/api/analysis/run?force=1").json()

    body = client.get("/api/analysis/latest").json()

    assert body["content"]["headline"] == forced["content"]["headline"]
    assert body["from_cache"] is True


def test_latest_does_not_generate_anything(client, make_product, fake) -> None:
    """★ `latest` 绝不能顺手生成报告：页面每次打开都会调它；顺手生成的话，打开
    分析页 = 花钱调模型，而**这个错误在开发时不会出现**（页面上总是已有报告）。"""
    client.get("/api/analysis/latest")
    client.get("/api/analysis/latest")

    assert fake.calls == 0


# 三、★ 没配 key：503 + 能照着做的话


def test_missing_key_returns_503_not_500(client, make_product, monkeypatch) -> None:
    """★★ 没配 key 时返回 **503**，消息要能照着做。不前置检查 key 是为了保住一条
    合法路径：**报告已在缓存里时不需要 key**——已生成过报告、随后 key 被清掉，
    点「生成报告」应命中缓存正常返回而不是 503，所以这里**不注入假客户端**。
    ★ 503 的含义是"服务端没坏但它依赖的东西不可用"——用 500 会让人翻栈回溯，
      用 400 会让人检查自己发的内容，两个方向都是错的。"""
    from app.config import settings

    _seed(make_product)
    monkeypatch.setattr(settings, "glm_api_key", "")

    r = client.post("/api/analysis/run")

    assert r.status_code == 503
    detail = r.json()["detail"]
    assert "GLM_API_KEY" in detail
    assert ".env" in detail


def test_a_cache_hit_works_without_a_key(client, make_product, fake, monkeypatch) -> None:
    """★ 缓存命中不需要 key —— 这条是"不前置检查 key"这个决定的**全部理由**：演示前
    配好 key 生成了一份报告；演示当天 key 被清掉或过期，点「生成报告」——报告本来
    就在缓存里，前置检查会把它变成 503。"""
    from app.config import settings

    _seed(make_product)
    first = client.post("/api/analysis/run").json()

    # 现在把 key 拿掉，但缓存里已经有报告了
    monkeypatch.setattr(settings, "glm_api_key", "")

    r = client.post("/api/analysis/run")

    assert r.status_code == 200, "缓存命中时不该因为没配 key 就 503"
    assert r.json()["from_cache"] is True
    assert r.json()["content"] == first["content"]
    assert client.get("/api/analysis/latest").status_code == 200


def test_the_503_detail_never_contains_the_api_key(client, make_product, monkeypatch) -> None:
    """★ 错误信息里绝不能带 API key：它是最容易被**分享出去**的字符串——显示在页面
    上、复制进聊天记录、贴进 issue；泄露就发生在"为了排查而分享"的过程中，而当事人
    完全没有感觉。
    ★ 这里走 `_safe_detail` 那道关卡构造"错误信息里混进 key"；用假 key——真 key
    绝不能出现在测试文件里（测试文件会进 git）。"""
    from app.config import settings

    fake_key = "fake-secret-key-for-testing-0123456789"
    _seed(make_product)
    monkeypatch.setattr(settings, "glm_api_key", fake_key)

    def boom(db, source, *, force=False):
        # 模拟 SDK 把请求信息（含 key）拼进了报错里
        raise AnalysisError(f"调用 GLM 失败：连接被拒绝（api_key={fake_key}）")

    monkeypatch.setattr(analysis_api, "generate_report", boom)

    r = client.post("/api/analysis/run")

    assert r.status_code == 503
    assert fake_key not in r.text, "错误响应把 API key 回显出来了"
    assert "***" in r.json()["detail"], "key 应该被替换成 ***，而不是整条消息被吞掉"
    # 有用的信息要留着——不能为了脱敏把报错变成一句无用的"出错了"
    assert "调用 GLM 失败" in r.json()["detail"]


# 四、参数校验：交给 FastAPI


def test_an_invalid_source_is_rejected_with_422(client) -> None:
    """非法数据源要在业务代码之前被拦住，而不是当成"没有数据"。"""
    assert client.post("/api/analysis/run?source=jd").status_code == 422


def test_the_same_rule_governs_health_and_the_analysis_endpoint(
    client, make_product, monkeypatch
) -> None:
    """★★ `/api/health` 的 `llm_configured` 必须和**分析接口的实际行为**一致。
    ★ 守一个**已经真实发生过**的不一致：`app/main.py` 里曾有自己的 key 校验实现，
      和 `glm_client.key_configured()` 分叉——对 replace_me_please / example-key-123
      / xxxxxx / TODO_KEY 这些值，health 判"已配置"、glm_client 判"未配置"；后果是
      health 说"AI 分析可用"→ 前端点亮按钮 → 点下去 503 → "按钮亮着，一点就报错"。
      修法不是把两份实现改成一样，而是**只留一份**。
    ★ 这条测的是**两个接口之间的关系**——每个模块自己的测试都是绿的，这类测试通常
      没人写。"""
    from app.config import settings

    _seed(make_product)
    probes = [
        "",
        "your_key_here",
        "replace_me_please",
        "example-key-123",
        "xxxxxx",
        "TODO_KEY",
        "sk-xxx",
        "在此填入你的key",
    ]

    for probe in probes:
        monkeypatch.setattr(settings, "glm_api_key", probe)
        healthy = client.get("/api/health").json()["llm_configured"]
        # 健康检查说"可用"，分析接口就必须真的不会报"未配置"
        status = client.post("/api/analysis/run?force=1").status_code
        assert healthy == (status != 503), (
            f"key={probe!r} 时两个接口不一致："
            f"health 说 {'可用' if healthy else '不可用'}，"
            f"分析接口返回 {status}"
        )
