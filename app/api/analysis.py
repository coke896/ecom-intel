"""AI 分析接口 —— 触发报告生成、取回已有报告。

    POST /api/analysis/run      生成（或从缓存取回）报告；`?force=1` 强制重算
    GET  /api/analysis/latest   取最近生成的一份报告

================================================================================
本层只做四件事，别的都不做
================================================================================

    1. 把 `?source=` 交给 `reporter.generate_report()`
    2. 把 `?force=` 传给同一个函数
    3. 把 `AnalysisError` 翻译成 HTTP 状态码
    4. 把 `ReportOutcome` 摊平成响应模型

**缓存、指纹、调模型、落库全部在 `app/analysis/` 里**，这个文件里一行都没有。
原因是那些逻辑的正确性条件跟 HTTP 毫无关系（是不是纯函数、指纹覆盖了什么），
混在一起写之后就必须先起一个服务才能测它们——
而 `test_reporter_cache.py` 现在是不起服务的。

================================================================================
为什么是 POST 而不是 GET
================================================================================

`run` 有副作用，而且是**要花钱**的副作用：写一行报告、消耗 token。

用 GET 的话，浏览器预取、代理缓存、爬虫、甚至"在地址栏里敲一下回车"
都可能触发一次付费调用——而这些都发生在用户没有点任何按钮的时候。
HTTP 语义在这里不是形式主义：GET 的约定就是"安全的、可重复的"，
而这个操作两个都不满足。

（`latest` 是 GET，因为它只读。）

================================================================================
★ 没配 key 时返回 503，不是 500 也不是 400
================================================================================

三种状态码对应三种责任人：

    400/422 —— **调用方**的请求写错了        （本项目里是语法错误，FastAPI 自动拦）
    503     —— **服务端**的问题，但它自己没坏，是它依赖的东西不可用
    500     —— 服务端出 bug 了

"没配 API key"是第二种：请求本身完全合法，服务也活着，
只是它依赖的 GLM 用不了。用 500 会让人去翻栈回溯找 bug，
用 400 会让人去检查自己发的内容——两个方向都是错的。

★ **不做"进端点先检查 key"这种前置拦截。** 看起来更早失败更友好，
  但它会顺手砍掉一条合法的路径：报告**已经在缓存里**时，
  生成报告根本不需要 key。
  已经生成过报告、随后 key 过期或被清掉——这时点"生成报告"
  命中缓存、正常返回，是对的行为；前置拦截会把它变成 503。
  所以判定放在真正要调模型的那一刻（`GlmClient._get_client`），
  这里只负责把异常翻译成状态码。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.analysis.glm_client import AnalysisError
from app.analysis.reporter import ReportOutcome, generate_report, latest_report
from app.api.deps import DbDep, SourceDep

router = APIRouter(prefix="/api/analysis", tags=["analysis"])


# ===========================================================================
# 响应模型
# ===========================================================================


class ReportSectionOut(BaseModel):
    """报告里的一段。"""

    title: str = ""
    finding: str = ""
    # ★ 默认空串而不是必填：字段缺失时前端不该整个渲染不出来。
    #   后端的 schema 校验（`prompts.ReportContent`）已经保证了
    #   "存进库的报告一定通过校验"，这里给默认值只是让响应模型
    #   在极端情况（有人手工改过库）下仍然能序列化。
    evidence: str = ""


class ReportOut(BaseModel):
    """报告的正文。

    ★ 全部字段给默认值，所以 `content={}` 能序列化成一个"空报告"。
      这一点是必需的：空态（还没生成过 / 数据源是空的）返回的就是
      `content: {}`，如果 headline 是必填，那两种空态会直接抛
      响应校验错误 → 500。**空态反而变成了最严重的错误**，
      而它本该是最普通的一种返回。
    """

    headline: str = ""
    sections: list[ReportSectionOut] = Field(default_factory=list)
    opportunities: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    data_limits: list[str] = Field(default_factory=list)


class AnalysisOut(BaseModel):
    """一份报告 + 它的元信息。

    前端的判断逻辑只有一条：**`notice` 有值就显示它，否则渲染正文**。
    这样"还没生成过""数据源是空的""生成成功"三种情况走同一个分支，
    不需要前端去猜"为什么 content 是空的"。
    """

    content: ReportOut = Field(default_factory=ReportOut)

    fingerprint: str = ""
    """数据指纹（64 位十六进制）。同一份数据 + 同一个模型 ⇒ 同一个指纹。

    ★ 暴露给前端不是为了显示（它没有可读性），而是为了**排查**：
      两次调用返回的指纹不同，就说明数据/模型/prompt 版本至少变了一样，
      这比"报告怎么不一样了"好定位得多。
    """

    model: str = ""
    tokens_used: int | None = None
    """**生成这份报告**所消耗的 token。

    ★ 字段描述的是**报告**，不是**这次请求**——所以命中缓存时它仍然是
      当初那个数字（比如 100），而不是 None。理由：它回答的是
      "这份报告值多少 token"，这个问题和"这次有没有真的去调模型"是两个问题。
      后者由 `from_cache` 单独回答。

      （写成"命中缓存时返回 None"会更直觉，但那样"这份报告的成本"
       就在缓存命中后消失了——而这个数字正是用来判断
       "值不值得为它花 token"的依据，不该随请求方式变化。）

    ★ 只有真的没有报告时才为 None：空数据源那条路径。
      这里也不能填 0——0 会被读成"调用了但没消耗"，而实际是"根本没调"。
    """

    created_at: datetime | None = None
    """这份报告**是什么时候生成的**。

    ★ 从缓存返回时，这是**最初那次**生成的时间，不是现在。
      刷成"现在"会造成"内容和时间对不上"：看起来是刚生成的，
      其实里面的数字是三天前的。宁可让用户看到"三天前"。

    ★ 格式沿用全项目的约定：不带时区的 UTC（见 `app/db/base.py` 的说明），
      前端用 `fmtTime()` 原样显示并标上 "UTC"。
      返回带 `Z` 的格式也可以，但那样项目里就有了两种时间格式——
      而两种格式的代价是"每次都要想一下这个字段是哪种"。
    """

    from_cache: bool = False
    """这次是不是直接返回了缓存。

    ★ 它是给**演示和排查**用的，不是给用户看的。
      界面上顺手显示一句"来自缓存"是好的（点一下秒回时，
      没有这句话会让人怀疑"它到底有没有真的重新分析"），
      但它真正的作用是让"缓存到底有没有生效"变成一眼可见的事实。
    """

    notice: str | None = None
    """非 None 表示这次**没有报告可显示**，这个字段解释了原因。

    沿用全项目的空态契约：200 + 一句能直接显示给用户的话。
    """


def _to_out(outcome: ReportOutcome) -> AnalysisOut:
    """把 `ReportOutcome` 摊平成响应模型。

    ★ 用 `ReportOut.model_validate(outcome.content)` 而不是
      `ReportOut(**outcome.content)`：前者对多出来的字段是忽略，
      后者在遇到模型返回的额外字段（`"conclusion"` 这类）时会
      因为参数名对不上直接 `TypeError`。
      `ReportOutcome.content` 已经过 Pydantic 校验，理论上不会有额外字段，
      但**"理论上不会有"的东西正是不该用脆弱写法去接的**。
    """
    return AnalysisOut(
        content=ReportOut.model_validate(outcome.content or {}),
        fingerprint=outcome.fingerprint,
        model=outcome.model,
        tokens_used=outcome.tokens_used,
        created_at=outcome.created_at,
        from_cache=outcome.from_cache,
        notice=outcome.notice,
    )


def _safe_detail(message: str) -> str:
    """确保要返回给前端的错误信息里**不含 API key**。

    ★ 这一步看起来多余：`AnalysisError` 的文案是我们自己写的，
      里面没有 key。但"调用 GLM 失败：{exc}"这一条会**原样带上
      SDK 的报错**，而 SDK 报错的内容不由我们控制——
      某些 HTTP 客户端在描述连接错误时会把请求头拼进消息里。

    ★ 和健康检查那条规则同一个理由（`test_health_never_echoes_the_api_key`）：
      错误信息会被显示在页面上、被贴进 issue、被写进日志。
      它是最容易被分享出去的那类字符串，所以必须在这里过一道。

    ★ 只在 key 非空时做替换——空字符串会让 `str.replace` 变成
      "在每个字符之间插 ***"，把有用的信息毁掉。
    """
    from app.config import settings

    key = (settings.glm_api_key or "").strip()
    if key and key in message:
        return message.replace(key, "***")
    return message


# ===========================================================================
# 端点
# ===========================================================================


@router.post("/run", response_model=AnalysisOut)
def run_analysis(
    db: DbDep,
    source: SourceDep,
    force: bool = Query(
        False,
        description="true/1 时跳过缓存、强制重新分析，并覆盖同一指纹的旧报告。",
    ),
) -> AnalysisOut:
    """生成（或从缓存取回）当前数据源的分析报告。

    ★ `?force=1` 这个出口是必需的，不是可有可无的开关：
      指纹缓存是对的，但**没有强制刷新的出口会在演示现场变成尴尬**——
      你说"我再生成一次"，它命中缓存秒回旧报告，看起来就像坏了。
    """
    try:
        outcome = generate_report(db, source, force=force)
    except AnalysisError as exc:
        # 503 而不是 500，理由见模块注释。
        # ★ 用 _safe_detail 而不是直接 str(exc)：这条消息会被显示、
        #   被复制、被贴进 issue，必须确保里面没有 key。
        raise HTTPException(status_code=503, detail=_safe_detail(str(exc))) from exc

    return _to_out(outcome)


@router.get("/latest", response_model=AnalysisOut)
def get_latest_report(db: DbDep) -> AnalysisOut:
    """取最近生成的一份报告（没有报告时返回 200 + `notice`）。

    ★ **不接受 `?source=` 参数**，这不是遗漏：

      报告表里没有 source 列，而且不该有。同一份数据可能被
      `?source=all` 和 `?source=mock` 各生成一份，它们的**指纹不同**、
      是两个独立的缓存项。要在这一层按数据源挑报告，就得再存一个
      "这份报告是给哪个 source 生成的"字段——那是给同一个东西
      造第二种身份，而两种身份迟早会对不上（指纹是主键，source 是标签，
      标签可以写错，指纹不会）。

      所以这个接口回答的是"页面上有没有报告可以显示"，
      按生成时间取最新的一份就够了。要区分数据源，看报告内容里写了什么。
    """
    return _to_out(latest_report(db))
