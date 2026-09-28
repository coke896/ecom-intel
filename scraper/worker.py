"""独立采集 worker —— 采集层的**进程边界**就在这个文件上。

    python -m scraper.worker                    # 常驻，轮询 crawl_tasks 表
    python -m scraper.worker --once             # 把当前待办跑完就退出
    python -m scraper.worker --replay           # 离线重放，把真实 SMZDM 数据灌进开发库

================================================================================
为什么必须是一个【独立进程】，而不是 FastAPI 里的一段代码
================================================================================
三个理由，按重要性排序。第一条是硬的（不这么做功能就是坏的），
后两条是设计取舍。

【1】Windows 上 uvicorn 的事件循环不支持子进程 —— Playwright 必崩

    `uvicorn --reload` 内部用 `use_subprocess=True`，worker 落到
    `SelectorEventLoop`；而 SelectorEventLoop **不支持子进程**，
    `asyncio.create_subprocess_exec` 直接抛 NotImplementedError。
    Playwright 启动 Chromium 走的正是这条路径。

    ★ 所以有两条互相独立的禁令，两条都不是"风格问题"：
        · 绝不让 Playwright 在 API 进程里运行，哪怕只是一个调试接口
        · 绝不写 `asyncio.set_event_loop_policy(WindowsSelectorEventLoopPolicy())`
          （老教程会加它，加了 Playwright 就废，而且症状是启动时才炸）
      本文件用默认的 ProactorEventLoop，是 Playwright 唯一被允许跑的地方。

【2】长任务不能绑在请求生命周期上

    一次采集是"分页抓取 + 解析 + 写库"，几秒到几分钟。
    用 `BackgroundTasks` 的话，请求返回后进程随时可能被回收，
    任务跑一半就断了，而且前端拿不到进度（详见 app/db/models.py 里
    CrawlTask 的注释）。所以任务是**表里的一行**，不是内存里的一个协程。

【3】采集和 Web 的负载特征不同，混在一起会互相影响

    采集是 CPU + 网络密集且突发，Web 是 IO 密集且要求低延迟。
    放一个进程里，一次采集就能让页面卡住几秒。

================================================================================
任务队列：为什么是一张表，以及"认领"为什么要原子
================================================================================
不用 Redis / Celery 的理由写在 CrawlTask 的模型注释里。这里说表做队列的实现要点。

★ **认领必须是原子的**（`UPDATE ... WHERE status='pending'`），
  而不是"先 SELECT 出来、再 UPDATE 成 running"。后者在两个 worker
  同时轮询时会**同时选中同一行**，于是同一个任务被抓两遍：
  数据不会错（入库是幂等的），但报告会重复计数，
  而且两倍请求量对 SMZDM 来说是实打实的合规风险。
  用带条件的 UPDATE + rowcount 判断，抢不到的那个会拿到 0 行，安静地找下一个。

★ **崩溃遗留的任务要能自愈**。进程被 Ctrl-C 或者断电时，
  任务会永远停在 `running`——而它既不会被重跑，也不会报错，
  在界面上就是一个"卡在 47%"的进度条。
  所以 worker 启动时扫一遍：running 且超过 STALE_TASK_MINUTES 没动静的，
  打回 pending 重跑。入库是幂等的，重跑是安全的。

================================================================================
进度更新：为什么每 N 条才写一次
================================================================================
SQLite 在 WAL 下读写不阻塞，但**写写仍然是串行的**。
每处理一条爆料就 UPDATE 一次进度，会让写锁被高频抢占，
Web 端的查询要排队等锁——表现是"采集一开，页面就卡"。
所以每 PROGRESS_EVERY 条才写一次（约等于一页爆料）。

★ 这也意味着**一个任务不是一个事务**，中途崩溃会留下半截数据。
  这是主动接受的取舍，理由是入库是幂等的：
  重跑同一批爆料不会改变数据库内容（见 ingest.ingest_deals 的文档）。
  换句话说，"整批原子"和"进度可见"二选一，而幂等性让前者变得不必要。
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
import time
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.base import SessionLocal, init_database, utcnow
from app.db.models import CrawlTask
from scraper import ingest
from scraper.check_alerts import check_alerts
from scraper.base import (
    CrawlReport,
    DataSource,
    FetchBlocked,
    FetchStats,
    NetworkDisabled,
    RawDeal,
    SourceError,
)
from scraper.seed import DEFAULT_PRODUCT_COUNT
from scraper.sources.mock import PER_PAGE as MOCK_PER_PAGE
from scraper.sources.smzdm import SmzdmSource, dedupe_deals

logger = logging.getLogger("scraper.worker")

# 每处理多少条爆料更新一次进度。
# 取 20 是因为两个源的每页都是 20 条——所以这个数字实际的含义是"每页更新一次"。
# ★ 它比"每 50 条"更贴合直觉：进度条前进的节奏和人看到的页数一致。
PROGRESS_EVERY = 20

# 启动时把多久没动静的 running 任务打回 pending（分钟）。
# ★ 取值要明显大于"一个正常任务的最长耗时"：
#   5 页 × (2~3 秒间隔 + 解析) ≈ 15~20 秒，加上写库不到 1 分钟。
#   取 10 分钟，正常任务绝不会被误判成崩溃遗留。
STALE_TASK_MINUTES = 10

# 轮询间隔（秒）。表做队列没有"有新任务就通知我"的机制，只能轮询。
# 1 秒足够让前端觉得"提交完马上就开始了"，又不至于空转烧 CPU。
POLL_INTERVAL = 1.0

# 告警检查的间隔（秒）。
#
# ★ 为什么是"每 N 秒"而不是计划里写的"每 N 轮"：
#   轮询间隔是可以通过 --interval 配的，"轮数"换算出来的实际间隔会跟着变。
#   而"5 分钟查一次告警"是一个关于**时间**的需求，不该由另一个参数间接决定——
#   否则有人把 --interval 调成 0.1 之后，告警就变成每 30 秒查一次，
#   谁也不会往那个方向想。
#
# ★ 为什么不每轮都查：告警看的是"价格跌破阈值"这种一天发生几次的事件，
#   而采集的轮询是秒级的。每秒查一次 = 每秒一次无用查询 + 日志噪声，
#   还会让 SQLite 的写锁被这个只读检查白白抢占。
ALERT_CHECK_INTERVAL = 300.0

# ★ 离线重放的语料目录。
#
#   这里放的是 5 个**真实抓下来**的 SMZDM 精选页（每页 20 条，共 100 条唯一爆料）。
#   重放它们得到的是真实数据——这和"伪造 source='smzdm' 的行"有本质区别，
#   区别在于数据是不是从真站点来的。这一点在 docs/可行性评估.md 里
#   反复强调过（14.6 / 15.7），因为"这数据是真的还是 Mock"正是面试会问的第一个问题。
#
#   ★ 为什么和测试共用同一个目录，而不是复制一份到 data/ 下：
#     复制一份 = 两份语料，而它们**会分叉**。
#     分叉的后果是 test_ingest.py 验的那批页面和演示时重放的那批不是同一批，
#     于是"测试通过"不再能推出"演示不会出意外"——而这类不一致
#     恰恰是本项目反复踩到的那一类（见 docs/可行性评估.md 第十六节 16.13）。
#     共用一份的代价是"生产代码引用了 tests/ 目录"，这个不美观是看得见的；
#     而语料分叉是不报错的。两害相权，选看得见的那个。
DEFAULT_REPLAY_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


# ===========================================================================
# 数据源工厂
# ===========================================================================


def build_source(name: str, *, allow_network: bool) -> DataSource:
    """按名字造一个采集源。

    ★ 这是 worker 里**唯一**知道"有哪些源"的地方，是一张显式的分派表。
      管道（ingest）从头到尾不认识任何一个源的名字——
      这正是 base.py 里设计 DataSource 协议的目的。

    ★ `--source smzdm` 默认**不联网**。
      SMZDM 有 WAF，实测约 50~100 次请求后会被腾讯 TCaptcha 拦住
      （HTTP 200 返回挑战页，随后 403）。所以真网络采集必须由人显式打开，
      这样"我今天要不要花掉那 50 次请求额度"是一个有意识的决定，
      而不是跑个命令的副作用。
    """
    if name == "mock":
        return _mock_source()
    if name == "smzdm":
        return SmzdmSource(allow_network=allow_network)
    raise ValueError(f"未知的采集源：{name!r}（可选：mock / smzdm）")


def _mock_source() -> DataSource:
    """Mock 源。单独包一个函数，是为了让上面那张分派表读起来是一行一个源。

    ★ 不在这里传 count/days/seed —— 用默认值。
      因为 Mock 源的价值就在于"和 seed.py 直写收敛到同一个库状态"，
      而收敛的前提是两边用**同一组参数**（见 tests/test_source_mock.py）。
      在这里随手传一组小参数，会让 `?source=mock` 下的商品
      和 seed 灌的那批对不上，于是采集一次就凭空多出一批商品——
      而"多出来的那批"看起来和原来那批长得一模一样，只有 source_id 差一位。
    """
    from scraper.sources.mock import MockSource

    return MockSource()


def mock_pages_for_full_dataset() -> int:
    """跑完整个 Mock 数据集需要几页。只给 CLI 提示用，不参与采集逻辑。"""
    return math.ceil(DEFAULT_PRODUCT_COUNT / MOCK_PER_PAGE)


# ===========================================================================
# 任务队列：认领与自愈
# ===========================================================================


def recover_stale_tasks(db: Session, *, minutes: int = STALE_TASK_MINUTES) -> int:
    """把卡住的 running 任务打回 pending，返回打回了几个。

    ★ 为什么需要它：worker 被 Ctrl-C 时任务停在 running，
      于是它既不会被重跑也不会报错——界面上就是一个永远卡在
      47% 的进度条。没有这个函数，唯一的修法是手工改数据库。

    ★ 判据用 updated_at 而不是 created_at：
      一个跑了 30 分钟的长任务，created_at 很早但 updated_at 刚刚才动过
      （进度在更新），不该被打回。用 created_at 会把正在正常跑的任务
      判成崩溃遗留，然后**两个 worker 同时跑同一个任务**。
    """
    cutoff = utcnow() - timedelta(minutes=minutes)
    result = db.execute(
        update(CrawlTask)
        .where(CrawlTask.status == "running", CrawlTask.updated_at < cutoff)
        .values(
            status="pending",
            message=f"上一次运行中断，已自动重试（超过 {minutes} 分钟没有进度更新）",
            updated_at=utcnow(),
        )
    )
    db.commit()

    # ★ 这一行不能省：批量 UPDATE **不会**把新值同步到会话里已有的对象上。
    #
    #   实测（SQLAlchemy 2.0.54 / SQLite）：执行完之后，
    #   同一会话里那个任务对象的 `status` 可能已经变了而 `message` 还是 None，
    #   或者两个都没变——`synchronize_session` 的 'evaluate' 和 'fetch'
    #   两种策略在多列赋值上都不保证同步，而**数据库里的行是对的**。
    #
    #   于是症状是"库改对了，但读它的人看到旧值"——
    #   和本项目反复踩到的"价格静默变旧"是同一类问题。
    #   显式 expire 之后，下次访问属性会重新查库，读到的必然是真值。
    #
    #   代价是这次会话里的对象全部过期，随后的访问各多一次 SELECT。
    #   这个函数一轮启动只跑一次，这点开销可以忽略；
    #   而"读到旧值"是不可接受的。
    db.expire_all()
    return int(result.rowcount or 0)


def claim_next_task(db: Session) -> CrawlTask | None:
    """原子地认领一个待办任务，没有就返回 None。

    ★ 关键在于**一条 SQL 完成"选中 + 占住"**，而不是先 SELECT 再 UPDATE。
      先查后改的写法在两个 worker 同时轮询时都会选中同一行，
      然后两个都把它当自己的任务跑——重复的请求量对 SMZDM 是合规风险。

    ★ 内层子查询挑"最早创建的那一个"（FIFO），
      外层再带一次 `status='pending'` 做守卫。
      两条 UPDATE 撞车时，后到的那个 rowcount 是 0，
      于是它安静地去找下一个任务——不会报错，也不会空跑。
    """
    target = (
        select(CrawlTask.id)
        .where(CrawlTask.status == "pending")
        .order_by(CrawlTask.id)
        .limit(1)
        .scalar_subquery()
    )
    claimed_id = db.execute(
        update(CrawlTask)
        .where(CrawlTask.id == target, CrawlTask.status == "pending")
        .values(
            status="running",
            progress=0,
            total=None,
            message="已认领，正在抓取…",
            updated_at=utcnow(),
        )
        .returning(CrawlTask.id)
    ).scalar_one_or_none()
    db.commit()

    if claimed_id is None:
        return None
    # ★ expire 的理由和 recover_stale_tasks 里那段一样：
    #   批量 UPDATE 不会同步会话里已有对象的属性。
    #   这里尤其要小心——认领会把 progress 清零、total 置空，
    #   如果会话里那个对象还带着**上一次运行**的 progress=100，
    #   那么紧接着 run_task 写进度时就会从 100 开始，
    #   于是前端看到一个一开始就满的进度条。
    db.expire_all()
    return db.get(CrawlTask, claimed_id)


# ===========================================================================
# 执行一个任务
# ===========================================================================


@dataclass(slots=True)
class TaskRun:
    """一次任务执行的结果。给测试和 CLI 共用。

    ★ `stats` 和 `report` 都要留着，因为它们是**两个阶段**的统计：
      stats 是抓取阶段（几页成功、几页被拦），report 是入库阶段。
      把 stats 丢掉的话，"一条都没抓到"就没法解释是哪一种没抓到。
    """

    task_id: int
    layer: str
    report: CrawlReport
    stats: FetchStats


def run_task(
    db: Session,
    task: CrawlTask,
    *,
    source: DataSource,
    pages: int,
    progress_every: int = PROGRESS_EVERY,
) -> TaskRun:
    """抓取 + 入库，全过程更新任务进度。**不写终态**，终态由 finish_task 决定。

    ★ 分两个阶段：先抓完，再入库。
      抓取阶段进度条是**不动的**（停在 0），因为总数要抓完才知道。
      这是有意的：抓取阶段的进度需要源支持"边抓边回调"，
      而为了一个进度条把源的接口复杂化不值得。所以
      `total` 在抓取完成后才写入，前端在此之前显示"抓取中"而不是百分比。
    """
    layer, deals, stats = source.fetch(pages=pages)

    task.total = len(deals)
    task.message = f"已抓取 {len(deals)} 条（{stats.pages_ok} 页），正在入库…"
    task.updated_at = utcnow()
    db.commit()

    if not deals:
        # 一条都没抓到。终态交给 finish_task —— 它会把这种情况判成 failed。
        return TaskRun(
            task_id=task.id, layer=layer, report=CrawlReport(), stats=stats
        )

    report = _ingest_in_batches(db, task, deals, source.name, progress_every)
    # ★ 四个页数统计**一个都不能漏**。原先只并了 ok / blocked 两个，
    #   于是"5 页里 1 页结构变了"的任务：状态 success、消息里不提，
    #   而那一页的 20 条数据已经静默地不在了。
    report = replace(
        report,
        pages_ok=stats.pages_ok,
        pages_blocked=stats.pages_blocked,
        pages_ugc=stats.pages_ugc,
        pages_unparsed=stats.pages_unparsed,
    )
    return TaskRun(task_id=task.id, layer=layer, report=report, stats=stats)


def _describe_empty_fetch(stats: FetchStats) -> str:
    """一条都没抓到时，说清楚**是哪一种**没抓到。

    ★ 三个数字对应三种完全不同的处置：
        pages_blocked   被 WAF 拦了        → 对方在挡我们，别重试、别加频率
        pages_ugc       抓的是分类页/UGC   → URL 填错了，改配置
        pages_unparsed  页面正常但解析出 0 → 页面改版了，改解析器
      合成一句"没有解析到任何爆料"的话，人会跑去研究 WAF，
      而真相可能是"URL 少写了一个字符"。
    """
    if stats.pages_blocked:
        return (
            f"★ 被拦截：{stats.pages_blocked} 页（WAF / 验证码）。"
            "已按合规约定停止，不重试。"
        )
    if stats.pages_ugc:
        return f"抓到的是分类页/UGC 页面（{stats.pages_ugc} 页），不是好价精选页"
    if stats.pages_unparsed:
        return f"页面拿到了但解析出 0 条（{stats.pages_unparsed} 页），页面结构可能变了"
    return "源没有返回任何页面"


def _ingest_in_batches(
    db: Session,
    task: CrawlTask,
    deals: list[RawDeal],
    source_name: str,
    progress_every: int,
) -> CrawlReport:
    """分批入库并推进进度。

    ★ 每批一次 commit：进度必须**提交后**才可见（别的连接要读它）。
      这代价是任务不是一个事务，崩溃会留下半截数据——
      用幂等性兜住（重跑同一批不改变库内容）。理由见模块顶部。

    ★ 报告要把每一批的计数**累加**起来，而不是只报最后一批。
      只报最后一批的话，100 条的任务会显示"新商品 7"，
      而真相是 100——这个 bug 不会报错，只会让报告变成假数字。
      （同一个坑在 ingest 里踩过一次，那次是"报告显示归并 100 条而实际 0 条"。）
    """
    total_new = total_merged = total_marked = total_refreshed = 0
    total_prices = 0
    done = 0

    for start in range(0, len(deals), progress_every):
        batch = deals[start : start + progress_every]
        rep = ingest.ingest_deals(db, batch, source=source_name)
        db.commit()

        total_new += rep.products_new
        total_merged += rep.products_merged
        total_marked += rep.products_marked
        total_refreshed += rep.products_refreshed
        total_prices += rep.prices_new

        done += len(batch)
        task.progress = done
        task.message = f"已入库 {done}/{len(deals)} 条…"
        task.updated_at = utcnow()
        db.commit()

    return CrawlReport(
        fetched=len(deals),
        products_new=total_new,
        products_merged=total_merged,
        products_marked=total_marked,
        products_refreshed=total_refreshed,
        prices_new=total_prices,
    )


def finish_task(db: Session, task: CrawlTask, run: TaskRun) -> None:
    """把执行结果写回任务行（终态）。

    ★ **"一条都没抓到"判成 failed，而不是 success。**

      这是本函数唯一一处需要判断的地方，也是刻意放在这里的：
      如果按"没抛异常就算成功"来写，一条都没抓到的任务会显示成
      success / 100% / "没有解析到任何爆料"——界面上和正常跑完一模一样。
      于是页面改版、URL 填错、被 WAF 拦这三种需要**立刻处理**的情况，
      看起来都像"今天没什么好价"。

      判成 failed 之后，前端会把任务标红，人就一定会去看 message；
      而 message 里带着"是哪一种没抓到"，直接指向该改的东西。
    """
    if run.report.fetched == 0:
        task.status = "failed"
        task.progress = 0
        task.message = _describe_empty_fetch(run.stats)
    else:
        task.status = "success"
        task.progress = task.total or 0
        # ★ 摘要来自数据库的真实反馈，不是"我处理了几条"（见 CrawlReport 的注释）。
        task.message = run.report.as_message()
    task.updated_at = utcnow()
    db.commit()


def fail_task(db: Session, task: CrawlTask, exc: BaseException) -> None:
    """把任务标成失败，并把**有用的那部分**错误信息留在 message 里。

    ★ 只留最后一行异常信息，不留完整 traceback：
      message 是给前端直接显示的一行字，塞进 traceback 会变成
      一坨没人看的堆栈，反而盖住了真正有用的那句话。
      完整堆栈由 logger.exception 记到日志里，排查时去那里看。

    ★ FetchBlocked 单独处理：它不该被重试，所以 message 要说清楚
      "已停止、不是 bug"。不说的话，下一个人看到失败任务的第一反应
      是再跑一次——而那恰好是合规上不该做的事。

    ★ NetworkDisabled 也单独处理，而且**和 FetchBlocked 截然相反**：
      它不是"被拒绝"，是"我们自己关着开关"。
      这两种情况混成一句话的代价是实测出来的：往队列里发一个 source=smzdm
      的任务、用默认参数跑 worker，失败信息写着"被拦截，已按约定停止"——
      而当时一个网络请求都没发出去。
      照着那句话去查，方向是 WAF；真相是命令行少了一个参数。
    """
    if isinstance(exc, NetworkDisabled):
        task.message = (
            f"未联网（不是被拦截）：{exc}\n"
            "★ 修法：加 --allow-network 走真网络，或加 --replay 走离线重放。"
        )
    elif isinstance(exc, FetchBlocked):
        task.message = f"被拦截，已按约定停止（不重试）：{exc}"
    elif isinstance(exc, SourceError):
        task.message = f"采集源出错：{exc}"
    else:
        task.message = f"{type(exc).__name__}: {exc}"
    task.status = "failed"
    task.updated_at = utcnow()
    db.commit()

    # ★ 预期内的失败不打堆栈。
    #
    #   `logger.exception` 会打印完整 traceback，而 traceback 在日志里的含义是
    #   "这里有个 bug，去看代码"。但被拦截和没联网**都不是 bug**——
    #   它们是已经预料到、并且有明确处置的状态。
    #
    #   实测的后果：跑一次 `worker --once`（没联网），日志里出现 20 行
    #   Python 堆栈，看起来像程序崩了，而实际上它规规矩矩地把任务标成了失败、
    #   写好了修法。**把正常行为显示成崩溃，会让人去修一个不存在的问题。**
    if isinstance(exc, (NetworkDisabled, FetchBlocked)):
        logger.warning("任务 #%s 未执行：%s", task.id, task.message)
    else:
        logger.exception("任务 #%s 失败", task.id)


# ===========================================================================
# 离线重放
# ===========================================================================


def replay(
    db: Session,
    paths: list[Path],
    *,
    source_name: str = "smzdm",
) -> CrawlReport:
    """从本地 HTML 文件重放一次采集。

    ★ 这是本项目**实际使用**的真实采集路径（不是降级方案）。
      磁盘上那批页面是从 smzdm.com 真实抓下来的，所以重放得到的是
      **真实数据**——不是伪造的 source='smzdm' 行。

    ★ 为什么开发演示走重放而不是真联网：
        · SMZDM 的 WAF 在约 50~100 次请求后开始拦截，额度要省着用
        · 重放可复现：同一批页面跑两次得到同样的 100 条，
          所以"第二轮采集"这个验证点才能稳定演示
        · CI 和别人的机器上都能跑，不需要联网、不会被拦

    ★ 重放**不写 crawl_tasks 表**。它是开发者的手工动作，
      不是"系统里的一个采集任务"——混进去会让任务历史里
      出现一堆没有来源的条目，反而看不清自动采集的真实情况。
    """
    missing = [p for p in paths if not p.exists()]
    if missing:
        raise FileNotFoundError(
            "回放语料缺失：\n  "
            + "\n  ".join(str(p) for p in missing)
            + f"\n\n默认语料目录是 {DEFAULT_REPLAY_DIR}，"
            "里面有 5 个真实抓取的 SMZDM 精选页。"
        )

    layer, deals, stats = SmzdmSource.parse_files(paths)
    # ★ 必须先按 source_id 去重：多个页面之间会重叠
    #   （实测 140 条原始结果里只有 100 条唯一，重放 5 个页面时
    #     jingxuan.html 和 jingxuan_p1.html 是同一页）。
    #   不去重的话数据不会错（唯一约束挡住），但"归并 N 条"的统计会翻倍——
    #   而统计错了比数据错了更难发现。
    deals = dedupe_deals(deals)

    logger.info(
        "重放 %d 个文件：%d 页成功 / %d 页被拦 / %d 页未解析，去重后 %d 条",
        len(paths),
        stats.pages_ok,
        stats.pages_blocked,
        stats.pages_unparsed,
        len(deals),
    )
    if not deals:
        raise RuntimeError(
            f"重放没有解析出任何爆料（{_describe_empty_fetch(stats)}）"
        )

    report = ingest.ingest_deals(db, deals, source=source_name)
    db.commit()
    # ★ 把抓取阶段的页数统计并进报告：`as_message()` 会在有页面被拦时
    #   追加一句"★ N 页被拦截"。重放 6 个文件里有 1 个是 WAF 拦截页，
    #   不并进去的话那句话不会出现，看起来像"6 个文件全都解析成功了"。
    #   ★ 另外两个统计同理——演示走的正是这条重放路径，
    #     消息里少一句"有页面没解析出来"，看起来就像全部成功了。
    return replace(
        report,
        pages_ok=stats.pages_ok,
        pages_blocked=stats.pages_blocked,
        pages_ugc=stats.pages_ugc,
        pages_unparsed=stats.pages_unparsed,
    )


# ===========================================================================
# 主循环
# ===========================================================================


def run_once(
    db: Session,
    *,
    pages: int,
    allow_network: bool,
    progress_every: int = PROGRESS_EVERY,
) -> int:
    """把当前所有待办任务跑完，返回跑了几个。"""
    count = 0
    while True:
        task = claim_next_task(db)
        if task is None:
            return count

        count += 1
        logger.info("认领任务 #%s（source=%s）", task.id, task.source)
        try:
            source = build_source(task.source, allow_network=allow_network)
            run = run_task(
                db, task, source=source, pages=pages, progress_every=progress_every
            )
        except Exception as exc:  # noqa: BLE001 —— 任何异常都只能让这一个任务失败
            # ★ 这里必须捕获所有异常。让异常冒出去会杀掉整个 worker 进程，
            #   于是后面排队的任务全部停在 pending —— 一个坏任务毒死一整条队列。
            db.rollback()  # 先回滚，否则失败的任务留在会话里没法写终态
            fail_task(db, db.get(CrawlTask, task.id), exc)
            continue

        finish_task(db, task, run)
        logger.info("任务 #%s 完成：%s", task.id, task.message)


# ===========================================================================
# 告警检查（每个 N 秒做一次，和采集共用这个进程）
# ===========================================================================


def check_alerts_once(db: Session) -> None:
    """跑一轮告警检查，把结果写进日志。

    ★ 只记日志，不做任何"推送"——本项目没有外部通知渠道。
      触发记录落在 alerts 表里，用户打开界面看得到（是【拉】，不是【推】）。
      这件事在 app/db/models.py 和 scraper/check_alerts.py 里都写明了。

    ★ 异常必须在这里被吞掉（只记日志），理由和 run_once 里那条一样：
      告警检查是循环里的**附带动作**，它出问题不该让整个 worker 停摆。
      一旦让异常冒到 main() 的循环外，采集也就一起停了——
      而"告警查不出来"和"采集停了"是完全不同的两个故障等级。

    ★ 而且必须先 rollback。检查过程中可能已经执行了一半的写操作
      （比如状态改成 fired、alerts 里加了一行），异常抛出时那个事务是**开着的**。
      不回滚的话，这个会话后续的每一次操作都会在一个已经出错的事务里跑，
      报的错会离真正的原因越来越远。
    """
    try:
        report = check_alerts(db)
    except Exception:  # noqa: BLE001 —— 见上面：附带动作不能拖垮主循环
        db.rollback()
        logger.exception("告警检查失败，本轮跳过（下一轮会重试）")
        return

    # ★ 只在"有事发生"时打 INFO。
    #   每 5 分钟打一行"检查了 3 条规则，无变化"会让日志里真正重要的
    #   那两行（触发、重新武装）被淹没——而告警的全部价值就在那两行上。
    if report.fired or report.rearmed or report.undecidable:
        logger.info("告警检查：%s", report.as_message())
        for reason in report.reasons:
            logger.warning("告警规则判不了：%s", reason)
    else:
        logger.debug("告警检查：%s", report.as_message())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scraper.worker",
        description="采集 worker（独立进程；Playwright 只允许在这里运行）",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=5,
        help=(
            f"每个任务抓几页（默认 5，即约 100 条）。"
            f"Mock 源跑完整个数据集需要 {mock_pages_for_full_dataset()} 页。"
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="把当前待办跑完就退出（测试和脚本用），而不是常驻轮询",
    )
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help=(
            "允许 smzdm 源真的联网。默认关闭——"
            "SMZDM 有 WAF，实测 50~100 次请求后会被拦，额度要省着用"
        ),
    )
    parser.add_argument(
        "--replay",
        nargs="?",
        const=str(DEFAULT_REPLAY_DIR),
        default=None,
        metavar="目录或文件",
        help=(
            f"离线重放本地 HTML 而不是联网采集（默认语料：{DEFAULT_REPLAY_DIR}）。"
            "这是本项目实际使用的真实数据采集路径"
        ),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=POLL_INTERVAL,
        help=f"轮询间隔秒数（默认 {POLL_INTERVAL}）",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="打印 DEBUG 日志")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    # ★ 建表。worker 完全可能先于 Web 服务启动（比如部署时并行拉起），
    #   那时库文件还不存在。/init_database 是幂等的，重复调用无害。
    init_database()

    db = SessionLocal()
    try:
        # ---- 离线重放：跑完就走，不进轮询循环 ----
        if args.replay is not None:
            target = Path(args.replay)
            paths = sorted(target.glob("*.html")) if target.is_dir() else [target]
            if not paths:
                logger.error("在 %s 里没找到 .html 文件", target)
                return 2
            # ★ 语料里可能混着反爬拦截页或分类页（_raw/ 目录就是这样）。
            #   parse_files 会分别计数并跳过它们，这里不用额外过滤——
            #   但要把结果打出来，否则"重放 7 个文件只得到 100 条"
            #   看起来像丢数据。
            report = replay(db, paths)
            print(f"重放完成：{report.as_message()}")
            return 0

        # ---- 启动自愈 ----
        revived = recover_stale_tasks(db)
        if revived:
            logger.warning("有 %d 个任务卡在 running，已打回 pending 重跑", revived)

        if args.once:
            n = run_once(db, pages=args.pages, allow_network=args.allow_network)
            # ★ --once 也要跑一次告警检查。
            #   它的语义是"把该做的事做一遍就走"，而告警检查是循环里
            #   唯一不依赖任务队列的周期性动作。不跑的话，
            #   "我手动跑一次试试告警"会什么也不发生——而那正是演示时的用法。
            check_alerts_once(db)
            logger.info("共处理 %d 个任务，退出", n)
            return 0

        logger.info(
            "worker 已启动：轮询间隔 %.1fs，每任务 %d 页，联网=%s，告警每 %.0fs 查一次",
            args.interval,
            args.pages,
            "允许" if args.allow_network else "禁止（仅 --replay 可用）",
            ALERT_CHECK_INTERVAL,
        )
        # ★ 初始化为 0（而不是"当前时间"），让 worker 一启动就先查一次告警。
        #   否则改完规则要等 5 分钟才看到结果，而人的第一反应是"是不是没生效"。
        last_alert_check = 0.0
        while True:
            ran = run_once(db, pages=args.pages, allow_network=args.allow_network)
            # ★ 用 monotonic 而不是 time.time()：
            #   系统时间被 NTP 往回拨时，time.time() 会倒退，
            #   于是这个判断会变成"还没到时间"并一直等下去（或反过来连查多次）。
            #   monotonic 只保证单调递增，这正是"隔了多久"需要的语义。
            now = time.monotonic()
            if now - last_alert_check >= ALERT_CHECK_INTERVAL:
                check_alerts_once(db)
                last_alert_check = now
            if ran == 0:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("收到中断，退出。正在跑的任务会在 %d 分钟后自动重试", STALE_TASK_MINUTES)
        return 130
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
