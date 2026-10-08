"""采集任务接口 —— 入队、查进度、看历史。

    POST /api/tasks/crawl     入队一个采集任务（**只写一行，不采集**）
    GET  /api/tasks/{id}      查一个任务的进度
    GET  /api/tasks           最近的任务列表

================================================================================
★ 这个文件里**没有一行采集代码**，这是设计而不是巧合
================================================================================
`POST /api/tasks/crawl` 的全部工作就是往 `crawl_tasks` 表里插一行 `pending`，
然后立刻返回。真正去抓网页的是另一个进程（`python -m scraper.worker`）。

这不是"为了解耦而解耦"，三条理由都是实的：

    1. 长任务不能绑在请求上。采集要几秒到几分钟，请求早返回了。
    2. **Playwright 绝不能在 API 进程里跑**。Windows 上 uvicorn 的
       worker 事件循环不支持子进程，Playwright 启动 Chromium 必崩
       （详见 scraper/worker.py 的模块注释）。
    3. 采集是 CPU + 网络密集且突发，Web 是 IO 密集且要低延迟。
       放一个进程里，一次采集能让页面卡住好几秒。

★ 代价必须说清楚：**API 和 worker 是两件独立的事，要分别启动。**
  只起 Web 服务的话，任务会永远停在 `pending`。
  这不是 bug，是上面那个设计的直接后果——所以本文件在入队响应里
  显式提示了怎么启动 worker（`TaskOut.notice`），
  而不是让人对着一个不动的进度条猜。

================================================================================
为什么禁止 `?source=all`，以及为什么重复提交会被挡下
================================================================================
`?source=` 这个参数在统计接口里的合法值是 `all|mock|smzdm`，
但在**采集**接口里 `all` 没有意义——"抓全部数据源"不是一个可以执行的动作，
能抓的只有具体的源。

★ 更重要的是第二件事：**同一个源已有待办任务时，不再入队第二个。**

  重复提交的后果不是"多跑一遍"这么轻：SMZDM 有 WAF，
  实测约 50~100 次请求后开始拦截。在页面上连点五次"采集"，
  就是 500 次请求——足以把这个额度一次性用光，
  而且看起来完全像正常操作（按钮没有防抖，点五次就是五次）。

  所以挡在**后端**而不是只在前端做防抖：前端的防抖挡不住
  刷新页面、挡不住 curl、挡不住两个标签页。
  后端是唯一能真正守住这个约束的地方。

  挡下时返回 **200 + 已有任务**（而不是 409），并且带一句 notice 说明
  "已经有一个在跑了，这是那一个"。理由：用户的意图（"我要看这个源的最新数据"）
  已经满足了，只是满足它的方式不是"再开一个任务"。
  返回 409 会让人以为是出错了，然后去重试——那正是我们要避免的行为。
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

# worker 的启动命令。写在响应里而不是只写在文档里——
# 因为"任务一直不动"是这套架构最常见的困惑点，
# 而困惑发生的那一刻正是最该看到答案的那一刻。
_WORKER_HINT = "python -m scraper.worker"

# ★ 认领超时：一个 running 任务超过这个时间没有进度更新，
#   前端就把它显示成"可能已中断"。
#
#   ★ 这个值现在**只有一处定义**：`settings.stale_task_minutes`（app/config.py）。
#     这里曾经写死过一份 `_STALE_MINUTES = 10`，和 worker 的
#     `STALE_TASK_MINUTES = 10` 靠一句注释"两边保持一致"约束着——
#     那正是本项目反复记录的那个形状：**同一条规则写两遍，迟早会分叉。**
#
#     当时那句注释还说不出口自己在被谁验证：tests/test_tasks.py 只读了
#     worker 那一份，接口这份改成 15 的话，测试照样全绿。
#     现在两份拷贝物理上不存在了，测试也改成断言两边读到的是同一个值。
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
        # ★ 夹到 [0, 100]：如果哪天 total 被改小（比如重跑时数据少了），
        #   进度条的宽度会超出容器。这是界面问题，但根因是脏数据，
        #   所以在这里夹一次，前端就不用再防一遍。
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


# ===========================================================================
# 端点
# ===========================================================================


@router.post("/crawl", response_model=TaskOut)
def enqueue_crawl(db: DbDep, source: SourceDep) -> TaskOut:
    """入队一个采集任务。**立即返回，不在这个请求里采集。**

    ★ 必须指定具体的源（`?source=mock` 或 `?source=smzdm`）。
      `?source=all` 会返回 422——"抓全部数据源"不是一个能执行的动作。
    """
    if source is SourceFilter.all:
        raise HTTPException(
            status_code=422,
            detail=(
                "采集必须指定具体的源：?source=mock 或 ?source=smzdm。"
                "all 只对统计查询有意义——它不是一个可以抓的东西。"
            ),
        )

    # ★ 一条 SQL 完成"检查 + 插入"，**不能写成先查后插**。
    #
    #   先查后插是最自然的写法（`existing = _active_task(...)`，
    #   是 None 就 `db.add(...)`），读起来完全正确，但在并发下不成立：
    #   两个请求各自查到自己想看的"没有待办任务"，然后各自插入。
    #
    #   实测（真实文件库，4 个并发 POST ?source=smzdm，
    #   把"查完、还没写"的那条缝隙从微秒级撑到 50ms 以便稳定复现）：
    #
    #       响应：(200, id=1) (200, id=2) (200, id=3) (200, id=4)
    #       库里：4 行 pending
    #
    #   ★ 四个请求**没有一个**认为自己被挡下了——全都返回 200 + 新 id。
    #     而这正是本文件顶部那段承诺要防的事：worker 会把 4 个任务依次跑一遍，
    #     也就是 4 倍的请求量，对 SMZDM 的额度是 4 倍的消耗。
    #     更要命的是它看起来完全正常：界面上只是"任务多跑了几轮"。
    #
    #   把判断塞进 INSERT 的 WHERE 里就成立了：SQLite 的写是串行的，
    #   两条语句必然一前一后执行，后到的那条会看到前一条刚提交的行。
    #   ★ 形状和 worker.py 的 claim_next_task 一模一样
    #     （那边是"一条 SQL 完成选中 + 占住"，注释在它上面）——
    #     同一个问题的同一个解法，只是这边守的是"入队"、那边守的是"认领"。
    now = utcnow()
    already_queued = (
        select(literal(1))
        .where(CrawlTask.source == source.value, CrawlTask.status.in_(_ACTIVE_STATUSES))
        .exists()
    )
    new_id = db.execute(
        insert(CrawlTask)
        .from_select(
            # ★ 列要写全（`total` 除外，它本来就该是 NULL）。
            #   不写的话，这些 NOT NULL 列不会被填上模型里的 default——
            #   `default=utcnow` 是 SQLAlchemy 在**参数集**上做的客户端默认值，
            #   而 INSERT ... SELECT 走的是另一条路，不经过它。
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
        # 这里不需要 expire：上面是 Core 插入，会话里没有对应的 ORM 对象，
        # 所以这次 get 必然真的去查库。（批量 UPDATE 那条路就必须 expire，
        # 理由见 worker.py 的 recover_stale_tasks——这里不是那种情况。）
        return _to_out(db.get(CrawlTask, new_id))

    # 没插进去 = 有别人先提交了一个待办任务。这不是错误，把那个任务返回回去。
    existing = _active_task(db, source.value)
    if existing is None:
        # ★ 理论上到不了：EXISTS 为真就说明查得到那一行。
        #   但如果真发生了，**绝不能返回 200**——那等于告诉调用方
        #   "任务已经在队列里了"，而实际上一个都没有，于是前端会一直
        #   等一个不存在的任务。宁可报出来。
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

    ★ 前端刷新页面后要靠这个恢复"刚才那个任务跑到哪了"——
      任务状态在数据库里，不在浏览器内存里，
      这正是"任务表而不是 BackgroundTasks"最直接的收益。
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

    ★ 找不到返回 404 而不是"空任务"：
      这里和 stats 接口的空态契约不同。空态契约（200 + notice）
      用于"查询成功但结果为空"这种情况——比如某个数据源没有销量数据。
      而"这个 id 不存在"是**请求写错了**，该走 404。
      两者混用会让前端把"任务被删了"显示成"任务还没开始"。
    """
    task = db.get(CrawlTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"没有 id={task_id} 的采集任务")
    return _to_out(task)
