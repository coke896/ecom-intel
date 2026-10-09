"""AI 分析接口 —— 触发报告生成、取回已有报告。

    POST /api/analysis/run      生成（或从缓存取回）报告；`?force=1` 强制重算
    GET  /api/analysis/latest   取最近生成的一份报告

本层只做三件事：把参数交给 `reporter.generate_report()`、把 `AnalysisError`
翻译成状态码、把结果摊平成响应模型；**缓存、指纹、调模型、落库全在
`app/analysis/` 里**——它们跟 HTTP 无关，混写就必须起服务才能测。

★ POST 而不是 GET：`run` 有**要花钱**的副作用——写报告、消耗 token；浏览器预取、
  代理缓存、爬虫、地址栏回车都可能触发付费调用，而 GET 按约定必须"安全、可重复"。
★ 没配 key 返回 503（不是 500/400）：请求合法、服务活着，只是 GLM 用不了。**不做
  "进端点先检查 key"的前置拦截**——报告已在缓存里时根本不需要 key。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.analysis.glm_client import AnalysisError
from app.analysis.reporter import ReportOutcome, generate_report, latest_report
from app.api.deps import DbDep, SourceDep

router = APIRouter(prefix="/api/analysis", tags=["analysis"])


# ---- 响应模型 ----


class ReportSectionOut(BaseModel):
    """报告里的一段。"""

    title: str = ""
    finding: str = ""
    # ★ 默认空串而不是必填：字段缺失时前端不该整个渲染不出来；存进库的报告一定
    #   通过校验，默认值只为极端情况（有人手工改过库）兜底。
    evidence: str = ""


class ReportOut(BaseModel):
    """报告的正文。

    ★ 全部字段给默认值，`content={}` 才能序列化成"空报告"——空态（还没生成过/
      数据源为空）返回的就是它；若 headline 必填，两种空态都会 500。
    """

    headline: str = ""
    sections: list[ReportSectionOut] = Field(default_factory=list)
    opportunities: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    data_limits: list[str] = Field(default_factory=list)


class AnalysisOut(BaseModel):
    """一份报告 + 它的元信息。

    前端的判断逻辑只有一条：**`notice` 有值就显示它，否则渲染正文**——这样
    "还没生成过""数据源是空的""生成成功"三种情况走同一个分支。
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

    ★ 用 `ReportOut.model_validate(...)` 而不是 `ReportOut(**...)`：后者遇到模型
      返回的额外字段（`"conclusion"` 这类）会因参数名对不上直接 `TypeError`。
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

    ★ "调用 GLM 失败：{exc}"那一条会**原样带上 SDK 的报错**，而 SDK 报错不由我们
      控制（某些 HTTP 客户端会把请求头拼进消息）；这段文字又最容易被分享出去
      （页面、issue、日志都见得到），必须在这里过一道。
    ★ 只在 key 非空时替换——空字符串会让 `str.replace` 变成"在每个字符之间插 ***"。
    """
    from app.config import settings

    key = (settings.glm_api_key or "").strip()
    if key and key in message:
        return message.replace(key, "***")
    return message


# ---- 端点 ----


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

    ★ `?force=1` 出口是必需的：否则演示现场"我再生成一次"会命中缓存秒回旧报告，
      看起来就像坏了。
    """
    try:
        outcome = generate_report(db, source, force=force)
    except AnalysisError as exc:
        # 503 理由见模块注释；★ 过 _safe_detail：这条消息会被显示、被复制、被贴进 issue。
        raise HTTPException(status_code=503, detail=_safe_detail(str(exc))) from exc

    return _to_out(outcome)


@router.get("/latest", response_model=AnalysisOut)
def get_latest_report(db: DbDep) -> AnalysisOut:
    """取最近生成的一份报告（没有报告时返回 200 + `notice`）。

    ★ **不接受 `?source=` 不是遗漏**：报告表没有 source 列；同一份数据可能被
      all 和 mock 各生成一份（指纹不同、两个独立缓存项）；按数据源挑报告就要再
      造一个"这份报告给哪个 source"的字段——同一个东西造两种身份，迟早对不上。
    """
    return _to_out(latest_report(db))
