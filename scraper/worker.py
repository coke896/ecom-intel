"""独立采集 worker —— 采集层的**进程边界**就在这个文件上。

    python -m scraper.worker [--once | --replay]   # 常驻轮询 / 跑完即退 / 离线重放

为什么必须是独立进程：`uvicorn --reload` 的 worker 在 Windows 上落到
SelectorEventLoop，不支持子进程（Playwright 必崩），也绝不手动加
WindowsSelectorEventLoopPolicy（老教程会加，加了 Playwright 就废）；
长任务不能绑在请求生命周期上（任务是表里的一行，不是内存里的协程）；
采集是突发的 CPU + 网络负载，Web 要求低延迟，混跑会让页面卡。

队列要点：认领必须是一条原子 SQL（UPDATE ... WHERE status='pending'），先查再改
会被两个 worker 同时选中同一行；崩溃遗留的 running 按 updated_at 打回 pending 重跑。
进度每 PROGRESS_EVERY 条写一次（WAL 写写串行，频繁写会卡住 Web），所以一个任务
不是一个事务，中途崩溃靠幂等重跑兜住（见 ingest.ingest_deals 的文档）。
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

from app.config import settings
from app.db.base import SessionLocal, init_database, utcnow
from app.db.models import CrawlTask
from scraper import ingest
from app.alerts_engine import check_alerts
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

# 用 `__name__` 而不是手打 "scraper.worker"：手打的名字在模块搬走后会留下
# （app/alerts_engine.py 栽过同一行），而 __name__ 不可能错——唯一启动方式是
# `python -m scraper.worker`（直跑 python scraper/worker.py 会 import 失败）。
logger = logging.getLogger(__name__)

# 每处理多少条爆料更新一次进度：取 20 = 两个源每页都是 20 条，即"每页更新一次"。
PROGRESS_EVERY = 20

# 启动时把多久没动静的 running 任务打回 pending（分钟）。10 分钟明显大于正常
# 任务最长耗时（5 页 ≈ 15~20 秒）；值取自 app/config.py（与 API 侧共用同一常量）。
STALE_TASK_MINUTES = settings.stale_task_minutes

# 轮询间隔（秒）。表做队列只能轮询；1 秒让提交后几乎立刻开始，又不空转烧 CPU。
POLL_INTERVAL = 1.0

# 告警检查间隔（秒）。★ 按"秒"而不是"每 N 轮"：轮询间隔可被 --interval 改，
# 轮数换算的间隔会跟着变；也不该每轮都查——秒级轮询下是无用查询，还白抢 SQLite 写锁。
ALERT_CHECK_INTERVAL = 300.0

# ★ 离线重放的语料目录：5 个真实抓下来的 SMZDM 精选页（共 100 条唯一爆料）。
# 重放得到的是真实数据，与"伪造 source='smzdm' 的行"有本质区别
# （docs/可行性评估.md 14.6 / 15.7），"这数据是真的还是 Mock"是面试第一问。
# ★ 与测试共用目录而非复制：复制会分叉（测试验的和演示用的不是同一批）——
# 分叉不报错，引用 tests/ 的不美观看得见，选看得见的。
DEFAULT_REPLAY_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


# --- 数据源工厂 ---


def build_source(name: str, *, allow_network: bool) -> DataSource:
    """按名字造一个采集源。

    ★ worker 里唯一知道"有哪些源"的地方（显式分派表），管道不认识源的名字。
    ★ `--source smzdm` 默认**不联网**（SMZDM 有 WAF：实测约 50~100 次请求后被 TCaptcha 拦住）：
    真联网必须显式打开（--allow-network），"要不要花掉那 50 次请求额度"是有意识的决定，不是跑个命令的副作用。
    """
    if name == "mock":
        return _mock_source()
    if name == "smzdm":
        return SmzdmSource(allow_network=allow_network)
    raise ValueError(f"未知的采集源：{name!r}（可选：mock / smzdm）")


def _mock_source() -> DataSource:
    """Mock 源。单独包一个函数，是为了让上面那张分派表读起来是一行一个源。

    ★ 不传 count/days/seed——Mock 源的价值是"和 seed.py 直写收敛到同一库状态"，
    收敛前提是两边同一组参数（见 tests/test_source_mock.py）；随手传小参数会让
    `?source=mock` 下凭空多出一批商品，长得和原来那批一样、只差一位 source_id。
    """
    from scraper.sources.mock import MockSource

    return MockSource()


def mock_pages_for_full_dataset() -> int:
    """跑完整个 Mock 数据集需要几页。只给 CLI 提示用，不参与采集逻辑。"""
    return math.ceil(DEFAULT_PRODUCT_COUNT / MOCK_PER_PAGE)


# --- 任务队列：认领与自愈 ---


def recover_stale_tasks(db: Session, *, minutes: int = STALE_TASK_MINUTES) -> int:
    """把卡住的 running 任务打回 pending，返回打回了几个。

    ★ 没有它时 Ctrl-C 留下的任务既不会重跑也不报错（界面永远卡 47%）。
    ★ 判据用 updated_at 而不是 created_at：长任务 created_at 早但进度刚更新过，
    用 created_at 会把正在跑的任务判成崩溃遗留，然后两个 worker 同跑同一个任务。
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

    # ★ 不能省：批量 UPDATE 不会把新值同步到会话里已有的对象上（synchronize_session
    # 不保证）——库改对了、读到的是旧值。expire 后下次访问重新查库；一轮只跑一次，
    # 多出的 SELECT 可忽略。
    db.expire_all()
    return int(result.rowcount or 0)


def claim_next_task(db: Session) -> CrawlTask | None:
    """原子地认领一个待办任务，没有就返回 None。

    ★ 必须一条 SQL 完成"选中 + 占住"（FIFO 子查询 + 外层 status='pending' 守卫），
    先查后改会让两个 worker 同时选中同一行（重复请求量对 SMZDM 是合规风险）；
    抢不到的 rowcount=0，安静去找下一个。
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
    # ★ expire 理由同 recover_stale_tasks；尤其防"认领清零了 progress，会话里
    # 旧对象还带着上次的 100，导致进度条一开始就满"。
    db.expire_all()
    return db.get(CrawlTask, claimed_id)


# --- 执行一个任务 ---


@dataclass(slots=True)
class TaskRun:
    """一次任务执行的结果。给测试和 CLI 共用。

    ★ stats（抓取阶段）和 report（入库阶段）都留着——丢掉 stats 的话，
    "一条都没抓到"就没法解释是哪一种没抓到。
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

    ★ 分两个阶段（先抓完再入库）：抓取阶段进度条不动，因为总数要抓完才知道；
    total 在抓取完成后才写入，之前前端显示"抓取中"而不是百分比。
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
    # ★ 四个页数统计一个都不能漏：只并 ok/blocked 时，"5 页里 1 页结构变了"
    # 的任务会显示 success 且消息不提，那一页的 20 条数据静默消失。
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

    ★ 三个数字三种处置：blocked=被 WAF 拦（别重试）、ugc=URL 填错（改配置）、
    unparsed=页面改版（改解析器）——合成一句"没有解析到任何爆料"会让人跑去研究 WAF。
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

    ★ 每批一次 commit：进度提交后才可见（别的连接要读它）；代价是任务不是一个
    事务，崩溃留下半截数据，靠幂等性兜住（见模块顶部）。
    ★ 报告计数必须逐批**累加**：只报最后一批会让 100 条的任务显示"新商品 7"——
    不报错的假数字（同一个坑在 ingest 里踩过一次）。
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

    ★ "一条都没抓到"判成 failed，而不是按"没抛异常就算成功"来写：后者会让页面
    改版、URL 填错、被 WAF 拦都显示成 success/"今天没什么好价"，而它们都需要
    立刻处理；判 failed 后前端标红，message 里直接写着是哪一种没抓到。
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
    """把任务标成失败，只留最后一行异常信息进 message（完整 traceback 去日志看）。

    ★ FetchBlocked 与 NetworkDisabled 必须分开说：前者"被拦截、不重试"（否则
    下一个人会再跑一次——恰是合规上不该做的事），后者是"我们自己关着开关"。
    混成一句话的实测代价：一个网络请求都没发出去，message 却写"被拦截"，
    照着去查 WAF，真相是命令行少了个参数。
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

    # ★ 预期内的失败不打堆栈：被拦截/没联网都不是 bug。logger.exception 打 20 行
    # 堆栈显得像程序崩了——把正常行为显示成崩溃，会让人去修不存在的问题。
    if isinstance(exc, (NetworkDisabled, FetchBlocked)):
        logger.warning("任务 #%s 未执行：%s", task.id, task.message)
    else:
        logger.exception("任务 #%s 失败", task.id)


# --- 离线重放 ---


def replay(
    db: Session,
    paths: list[Path],
    *,
    source_name: str = "smzdm",
) -> CrawlReport:
    """从本地 HTML 文件重放一次采集。

    ★ 这是本项目**实际使用**的真实采集路径（不是降级方案）：语料是从 smzdm.com
    真实抓下来的，重放得到真实数据——不是伪造的 source='smzdm' 行。且可复现
    （同批页面跑两次得到同样 100 条）、不耗 WAF 额度、CI 和别人的机器都能跑。
    ★ 重放**不写 crawl_tasks 表**：它是开发者的手工动作，混进去会污染任务历史。
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
    # ★ 必须先按 source_id 去重：多个页面之间有重叠（实测 140 条原始结果里只有
    # 100 条唯一）。不去重数据也不会错（唯一约束挡住），但"归并 N 条"的统计会翻倍。
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
    # ★ 把抓取阶段的四个页数统计并进报告：as_message() 会在有页面被拦/未解析时
    # 追加提示（重放语料里正好有 1 个 WAF 拦截页），不并的话演示看起来全成功了。
    return replace(
        report,
        pages_ok=stats.pages_ok,
        pages_blocked=stats.pages_blocked,
        pages_ugc=stats.pages_ugc,
        pages_unparsed=stats.pages_unparsed,
    )


# --- 主循环 ---


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
            # ★ 必须捕获所有异常：冒出去会杀掉整个 worker 进程，后面排队的任务
            # 全停在 pending——一个坏任务毒死一整条队列。
            db.rollback()  # 先回滚，否则失败的任务留在会话里没法写终态
            fail_task(db, db.get(CrawlTask, task.id), exc)
            continue

        finish_task(db, task, run)
        logger.info("任务 #%s 完成：%s", task.id, task.message)


# --- 告警检查（每 N 秒一次，与采集共用进程） ---


def check_alerts_once(db: Session) -> None:
    """跑一轮告警检查，把结果写进日志（触发记录落在 alerts 表，是【拉】不是【推】）。

    ★ 异常必须在这里吞掉并先 rollback：告警检查是循环里的**附带动作**，出问题
    不能停摆采集；不回滚的话，已执行到一半的写事务会让会话后续操作越错越远。
    """
    try:
        report = check_alerts(db)
    except Exception:  # noqa: BLE001 —— 见上面：附带动作不能拖垮主循环
        db.rollback()
        logger.exception("告警检查失败，本轮跳过（下一轮会重试）")
        return

    # ★ 只在"有事发生"时打 INFO：每 5 分钟一行"无变化"会淹没真正重要的两行（触发、重新武装）。
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

    # ★ 建表：worker 可能先于 Web 服务启动，那时库文件还不存在；幂等，重复无害。
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
            # ★ 语料里可能混着拦截页/分类页，parse_files 会分别计数并跳过；
            # 结果要打出来，否则"重放 7 个文件只得到 100 条"看起来像丢数据。
            report = replay(db, paths)
            print(f"重放完成：{report.as_message()}")
            return 0

        # ---- 启动自愈 ----
        revived = recover_stale_tasks(db)
        if revived:
            logger.warning("有 %d 个任务卡在 running，已打回 pending 重跑", revived)

        if args.once:
            n = run_once(db, pages=args.pages, allow_network=args.allow_network)
            # ★ --once 也要跑告警检查：它是循环里唯一不依赖任务队列的周期动作，
            # 不跑的话"手动跑一次试试告警"什么都不会发生——而那正是演示时的用法。
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
        # ★ 让 worker 一启动就先查一次告警——否则改完规则要等 5 分钟才见结果。
        # ★★ 不能初始化为 0.0：time.monotonic() 的原点是**开机时刻**，`now - 0.0`
        # 会变成"开机时长 >= 300 秒"（实测刚开机启动 worker 时首查被推迟）；
        # 写成"现在 - 间隔"则与 monotonic 的原点在哪里完全无关。
        last_alert_check = time.monotonic() - ALERT_CHECK_INTERVAL
        while True:
            ran = run_once(db, pages=args.pages, allow_network=args.allow_network)
            # ★ 用 monotonic 而不是 time.time()：NTP 往回拨时 time.time() 会倒退，
            # 判断会一直"还没到时间"；monotonic 单调递增，正是"隔了多久"的语义。
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
