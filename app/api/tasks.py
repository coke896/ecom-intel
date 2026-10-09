"""采集任务接口 —— 入队、查进度、看历史（POST /crawl、GET /{id}、GET 列表）。

★ **这个文件里没有一行采集代码**：`POST /crawl` 只插一行 `pending` 就返回，抓网页
  的是另一个进程（`python -m scraper.worker`）——长任务不能绑在请求上；
  **Playwright 绝不能在 API 进程里跑**（Windows 上 uvicorn 的 worker 事件循环不
  支持子进程，必崩）。代价：**API 和 worker 必须分别启动**，只起 Web 服务的话
  任务会永远停在 `pending`（所以入队响应里提示了启动方式）。
★ 采集接口禁止 `?source=all`："抓全部数据源"不是一个可以执行的动作。
★ **同一个源已有待办任务时不再入队第二个**：SMZDM 有 WAF，实测约 50~100 次请求
  后开始拦截，连点五次"采集"就把额度一次性用光，而且看起来完全像正常操作。挡在
  **后端**而不是只在前端防抖（防抖挡不住刷新页面、curl、第二个标签页）；挡下时
  返回 **200 + 已有任务**（不是 409）——用户的意图已经满足了，409 只会让人去重试。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy import insert, literal, select
from sqlalchemy.orm import Session

from app.api.deps import DbDep, SourceDep
from app.config import settings
from app.db.base import utcnow
from app.db.models import CrawlTask
from app.source_scope import SourceFilter

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

# 还在"进行中"的两个状态。用来判断"同一个源是不是已经有一个在跑了"。
_ACTIVE_STATUSES = ("pending", "running")

# worker 的启动命令。写在响应里而不是只写在文档里——因为"任务一直不动"是这套
# 架构最常见的困惑点，而困惑发生的那一刻正是最该看到答案的那一刻。
_WORKER_HINT = "python -m scraper.worker"

# ★ 认领超时：running 任务超过这个时间没更新，界面就显示成"可能已中断"。这个值
#   **只有一处定义**（settings.stale_task_minutes）；这里曾写死过一份和 worker
#   那份靠注释"保持一致"的 `_STALE_MINUTES = 10`——同一条规则写两遍迟早会分叉。
_STALE_MINUTES = settings.stale_task_minutes


class TaskOut(BaseModel):
    """一个采集任务的状态。前端轮询这个结构。"""

    id: int
    source: str
    status: str
    """pending / running / success / failed。

    ★ 前端**不应该**自己推断状态。比如"progress 不动了是不是挂了"——
      那是 `stale` 字段回答的问题，而它需要知道 worker 的自愈阈值。
      把判断留在后端，前端只按状态渲染。
    """

    progress: int = 0
    total: int | None = None
    message: str | None = None

    percent: int | None = None
    """进度百分比。**`total` 未知时是 None，不是 0。**

    ★ 这个区分很重要：抓取阶段 `total` 还没写（要抓完才知道总数），
      此时 percent 是 None，前端显示"抓取中…"这个不确定态。
      如果返回 0，前端会显示一个有刻度的进度条停在 0%——
      而"0% 卡住"和"还在抓取"在视觉上完全无法区分。
    """

    stale: bool = False
    """true 表示这个任务看起来已经中断（running 但很久没更新）。

    ★ 它不会被自动改状态——worker 会在启动时把这类任务打回 pending。
      所以这里的 true 意思是"现在这一刻它没有在前进"，
      而不是"它失败了"。
    """

    created_at: datetime
    updated_at: datetime


class TaskListOut(BaseModel):
    """任务列表。"""

    items: list[TaskOut] = Field(default_factory=list)
    total: int = 0


def _to_out(task: CrawlTask, *, now: datetime | None = None) -> TaskOut:
    """把 ORM 行摊平成响应模型，顺便算出两个派生字段。"""
    now = now or utcnow()

    percent = None
    if task.total:
        # ★ 夹到 [0, 100]：total 被改小（比如重跑时数据少了）会让进度条宽度溢出。
        #   这是界面问题，但根因是脏数据，在这里夹一次前端就不用再防一遍。
        percent = max(0, min(100, round(task.progress * 100 / task.total)))

    stale = (
        task.status == "running"
        and (now - task.updated_at).total_seconds() > _STALE_MINUTES * 60
    )

    return TaskOut(
        id=task.id,
        source=task.source,
        status=task.status,
        progress=task.progress,
        total=task.total,
        message=task.message,
        percent=percent,
        stale=stale,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _active_task(db: Session, source: str) -> CrawlTask | None:
    """这个源是不是已经有一个在排队或正在跑的任务。"""
    return db.execute(
        select(CrawlTask)
        .where(CrawlTask.source == source, CrawlTask.status.in_(_ACTIVE_STATUSES))
        .order_by(CrawlTask.id)
        .limit(1)
    ).scalar_one_or_none()


# ---- 端点 ----


@router.post("/crawl", response_model=TaskOut)
def enqueue_crawl(db: DbDep, source: SourceDep) -> TaskOut:
    """入队一个采集任务。**立即返回，不在这个请求里采集。**

    ★ 必须指定具体的源（`?source=mock` / `?source=smzdm`）；`?source=all` 返回
      422——"抓全部数据源"不是一个能执行的动作。
    """
    if source is SourceFilter.all:
        raise HTTPException(
            status_code=422,
            detail=(
                "采集必须指定具体的源：?source=mock 或 ?source=smzdm。"
                "all 只对统计查询有意义——它不是一个可以抓的东西。"
            ),
        )

    # ★ 一条 SQL 完成"检查 + 插入"，**不能写成先查后插**：并发下两个请求会各自
    #   查到"没有待办"然后各插一行。把判断塞进 INSERT 的 WHERE 就成立（SQLite 的
    #   写是串行的）；形状和 worker.py 的 claim_next_task 一样，一边守认领一边守入队。
    now = utcnow()
    already_queued = (
        select(literal(1))
        .where(CrawlTask.source == source.value, CrawlTask.status.in_(_ACTIVE_STATUSES))
        .exists()
    )
    new_id = db.execute(
        insert(CrawlTask)
        .from_select(
            # ★ 列要写全（`total` 除外，它本来就该是 NULL）：`default=` 是
            #   SQLAlchemy 在**参数集**上做的客户端默认值，而 INSERT ... SELECT
            #   走的是另一条路，不经过它，漏写的 NOT NULL 列会直接插入失败。
            ["source", "status", "progress", "message", "created_at", "updated_at"],
            select(
                literal(source.value),
                literal("pending"),
                literal(0),
                literal(f"已入队。等待 worker 认领——启动方式：{_WORKER_HINT}"),
                literal(now),
                literal(now),
            ).where(~already_queued),
        )
        .returning(CrawlTask.id)
    ).scalar_one_or_none()
    db.commit()

    if new_id is not None:
        # 这里不需要 expire：上面是 Core 插入，会话里没有对应的 ORM 对象。
        return _to_out(db.get(CrawlTask, new_id))

    # 没插进去 = 有别人先提交了一个待办任务。这不是错误，把那个任务返回回去。
    existing = _active_task(db, source.value)
    if existing is None:
        # ★ 理论上到不了（EXISTS 为真就查得到那一行）；真发生了**绝不能返回
        #   200**——那等于告诉调用方"任务已在队列里"，而实际一个都没有。
        raise HTTPException(
            status_code=503,
            detail=(
                "入队被并发操作挡住了，但没查到是哪一个任务。"
                "这不该发生——请刷新后在任务列表里确认一下，再重试。"
            ),
        )

    out = _to_out(existing)
    out.message = (
        f"{source.value} 已有一个采集任务（#{existing.id}，"
        f"{existing.status}）在队列里，本次不再重复入队。"
        f"★ 重复入队不会更快，只会把 SMZDM 的请求额度更快用光。"
    )
    return out


@router.get("", response_model=TaskListOut)
def list_tasks(
    db: DbDep,
    limit: int = Query(20, ge=1, le=200, description="最多返回几条"),
) -> TaskListOut:
    """最近的任务，最新的在前。

    ★ 前端刷新页面后靠它恢复"刚才那个任务跑到哪了"——状态在数据库里而不是浏览器
      内存里，这正是"任务表而不是 BackgroundTasks"的收益。
    """
    rows = list(
        db.execute(select(CrawlTask).order_by(CrawlTask.id.desc()).limit(limit))
        .scalars()
    )
    now = utcnow()
    return TaskListOut(
        items=[_to_out(t, now=now) for t in rows],
        total=len(rows),
    )


@router.get("/{task_id}", response_model=TaskOut)
def get_task(
    db: DbDep,
    task_id: int = Path(description="任务 id（POST /crawl 的返回值）"),
) -> TaskOut:
    """查一个任务的进度。

    ★ 找不到返回 404 而不是"空任务"：空态契约（200+notice）用于"查询成功但结果
      为空"，"这个 id 不存在"是**请求写错了**；混用会让前端把"任务被删了"显示
      成"任务还没开始"。
    """
    task = db.get(CrawlTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"没有 id={task_id} 的采集任务")
    return _to_out(task)
