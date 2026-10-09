"""报告生成 + 按数据指纹缓存。

只做三件事：装配 payload（委托 payload.build_payload）、按指纹查缓存或调模型
（委托 AnalysisClient）、落库（自己的事）。判断"该不该重新分析"的全部依据是
**指纹变了没有**，不掺时间、不掺调用次数（理由见 fingerprint.py 的模块注释）。

`force=1` 必须有：没有强制刷新的出口，说"再生成一次"却秒回旧报告，看起来像坏了。
★ 落库必须 `ON CONFLICT (fingerprint) DO UPDATE` 而不是 INSERT——指纹没变时
那一行已经在表里，INSERT 会撞唯一索引。UPSERT 还顺带把"缓存未命中时的写入"变成
原子的：两个请求同时未命中时后落库的 UPDATE，而不是 IntegrityError（"先 SELECT
再 INSERT"就有这个竞态，且只在并发时偶发、非常难查）。

空数据源不调模型：一个商品都没有时调 GLM 是纯粹的浪费（花 token、等十几秒换来
一段"数据不足"的废话），`generate_report` 直接返回、不碰客户端。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.analysis.fingerprint import build_fingerprint
from app.analysis.glm_client import AnalysisClient, GlmClient
from app.analysis.payload import build_payload
from app.db.base import utcnow
from app.db.models import AnalysisReport
from app.source_scope import SourceFilter

logger = logging.getLogger(__name__)

NO_REPORT_NOTICE = "还没有生成过分析报告。点「生成分析报告」即可调用 GLM 分析当前数据源。"
NO_DATA_NOTICE = "该数据源暂无数据（尚未采集），无法分析。请先灌入 Mock 数据或运行采集。"


@dataclass(frozen=True)
class ReportOutcome:
    """一次 `generate_report` 的结果。"""

    fingerprint: str
    model: str
    content: dict[str, Any]
    tokens_used: int | None
    created_at: datetime
    from_cache: bool
    """★ 给**测试和日志**用的，不是给用户看的。

    验证缓存真的生效，唯一的办法就是看第二次调用有没有真的去碰网络；
    前端"来自缓存"只是顺手显示。
    """

    notice: str | None = None
    """非 None 表示这次**没有生成报告**，这个字段解释了原因。

    沿用全项目的空态契约：不抛异常、不返回 None 让调用方猜，
    而是给一个能直接显示给用户的句子。
    """


def _upsert_report(
    db: Session,
    fingerprint: str,
    model: str,
    content: dict[str, Any],
    tokens_used: int | None,
) -> datetime:
    """写入或覆盖报告行，返回写入后的 created_at。

    ★ 用 Core 的 `insert()` 而不是 ORM 的 `db.merge()`：merge 会先 SELECT 一次
      再决定 INSERT 还是 UPDATE，也就是上面说的那个竞态；UPSERT 是一条语句、
      由数据库保证原子性。
    """
    now = utcnow()
    row = {
        "fingerprint": fingerprint,
        "model": model,
        "content_json": json.dumps(content, ensure_ascii=False),
        "tokens_used": tokens_used,
        "created_at": now,
    }

    stmt = sqlite_insert(AnalysisReport).values(**row)
    db.execute(
        stmt.on_conflict_do_update(
            index_elements=["fingerprint"],
            set_={
                "model": row["model"],
                "content_json": row["content_json"],
                "tokens_used": row["tokens_used"],
                # ★ created_at 必须一起刷新。不刷的话，force=1 重新生成之后，
                #   界面上的"生成时间"还显示着三天前，而内容是刚刚的——
                #   这比"显示旧报告"更让人困惑：内容和时间对不上。
                "created_at": row["created_at"],
            },
        )
    )
    db.commit()
    return now


def _load_cached(db: Session, fingerprint: str) -> AnalysisReport | None:
    """按指纹取缓存行。"""
    return db.execute(
        select(AnalysisReport).where(AnalysisReport.fingerprint == fingerprint)
    ).scalar_one_or_none()


def _outcome_from_row(row: AnalysisReport, from_cache: bool) -> ReportOutcome:
    """把数据库行还原成返回值。

    ★ `json.loads` 失败时不吞异常：content_json 是本程序自己写进去的，它坏掉
      说明有更严重的问题，静默返回空报告只会掩盖它。（对比 `_round_price` 选择
      吞掉异常——那是**外部数据**，脏是正常的；这里是**自己写的数据**，脏是 bug。）
    """
    return ReportOutcome(
        fingerprint=row.fingerprint,
        model=row.model,
        content=json.loads(row.content_json),
        tokens_used=row.tokens_used,
        created_at=row.created_at,
        from_cache=from_cache,
        notice=None,
    )


def generate_report(
    db: Session,
    source: SourceFilter = SourceFilter.all,
    *,
    force: bool = False,
    client: AnalysisClient | None = None,
    model: str | None = None,
    sample_size: int | None = None,
) -> ReportOutcome:
    """生成（或从缓存取回）当前数据源的分析报告。

    `force=True` 时跳过缓存查询、直接调模型，然后**覆盖**同一指纹的旧记录。
    `client` 可注入分析客户端——测试全靠它：传一个只数调用次数的假客户端，
    就能在不联网的情况下验证"第二次调用没有碰网络"。
    """
    payload_kwargs = {} if sample_size is None else {"sample_size": sample_size}
    payload = build_payload(db, source, **payload_kwargs)

    # ---- 空数据源：不调模型 ----
    # ★ 判断依据是 payload 里的商品数，不是"报告表里有没有行"——后者会把
    #   "数据被清空了但报告还在"当成有数据，然后生成一份描述已经不存在的报告。
    if payload["summary"]["product_count"] == 0:
        return ReportOutcome(
            fingerprint="",
            model=model or _client_model(client),
            content={},
            tokens_used=None,
            created_at=utcnow(),
            from_cache=False,
            notice=NO_DATA_NOTICE,
        )

    client = client or GlmClient()
    # ★ 用客户端自己的模型名，而不是 settings 里的：否则测试注入"模型名是
    #   fake-model"的假客户端时，指纹会按 settings 的模型名算，缓存键和实际
    #   调用的模型对不上，让"改模型指纹就该变"这条测试测不到真东西。
    model_name = model or _client_model(client)

    fingerprint = build_fingerprint(payload, model_name)

    # ---- 查缓存 ----
    if not force:
        cached = _load_cached(db, fingerprint)
        if cached is not None:
            logger.info("报告命中缓存 fingerprint=%s…", fingerprint[:12])
            return _outcome_from_row(cached, from_cache=True)

    # ---- 调模型 ----
    result = client.analyze(payload)
    created_at = _upsert_report(
        db,
        fingerprint=fingerprint,
        model=model_name,
        content=result.content,
        tokens_used=result.tokens_used,
    )

    return ReportOutcome(
        fingerprint=fingerprint,
        model=model_name,
        content=result.content,
        tokens_used=result.tokens_used,
        created_at=created_at,
        from_cache=False,
    )


def latest_report(db: Session) -> ReportOutcome:
    """取最近生成的一份报告（不关心数据源）。

    ★ 报告表没有 source 列、也**不该有**：同一份数据在 `?source=all` 和
      `?source=mock` 下是两个独立的缓存项（指纹不同）。这个接口只是"打开页面
      看看有没有报告可显示"，所以按 created_at 取最新的一条就够了。
    """
    row = db.execute(
        select(AnalysisReport).order_by(AnalysisReport.created_at.desc(), AnalysisReport.id.desc())
    ).first()
    if row is None:
        return ReportOutcome(
            fingerprint="",
            model="",
            content={},
            tokens_used=None,
            created_at=utcnow(),
            from_cache=False,
            notice=NO_REPORT_NOTICE,
        )
    return _outcome_from_row(row[0], from_cache=True)


def _client_model(client: AnalysisClient) -> str:
    """取客户端用的模型名，取不到就回落到配置。

    ★ 真实客户端有 `.model` 属性，假客户端可能没有——用 getattr 而不是给
      Protocol 加一个属性要求：逼所有测试替身实现它只会让测试更难写，
      换来的只是一个本来就有默认值的值。
    """
    return getattr(client, "model", None) or "unknown-model"
