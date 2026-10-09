"""采集任务：worker 的队列语义 + 任务接口。

这里的失败方式不会让数据变错，而是让流程**卡住或者空转**：
任务在跑界面永远"等待中"、一条脏数据把 worker 弄崩后面全部饿死、
一条都没抓到却显示"成功 100%"于是没人去修解析器、连点五次采集就入队五次、
进程被 Ctrl-C 后任务永远停在 47%——共同点是**都不抛异常**，只能靠测试钉。

★ 用假源 FakeSource 而不是真源：要能构造"抓回 0 条"（真源正常时永远不空，
而"空结果该判成功还是失败"恰是本文件最重要的断言）、要能控制条数
（"计数跨批次累加"需要精确的 50 条 / 每批 20 条）、要能不联网测失败路径。
它不继承任何东西——正是 DataSource 用 Protocol 而不是 ABC 的价值。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.base import get_db, utcnow
from app.db.models import CrawlTask, Product
from app.main import app
from scraper import worker
from scraper.base import FetchBlocked, FetchStats, NetworkDisabled, RawDeal
from scraper.sources.mock import MockSource


# 测试替身


class FakeSource:
    """可控的采集源：条数、内容、要不要抛异常，全由测试指定。

    ★ 每次 fetch 返回**新的 source_id**（`{prefix}-{序号}`），"新商品"的计数才可预期；
    用固定 source_id 会让第二轮变成"刷新"而不是"新建"。

    ★ **每条爆料给一个不同的品牌**，让硬门槛全部判成新商品——这不是偷懒，是必要的隔离：
    第一版用同一品牌 + 只差一个数字的标题，25 条里只有 1 条"新商品"、24 条被判成灰色地带
    （归并逻辑没错，那批标题本来就像同一商品的不同爆料），而"计数跨批次累加"就没法用
    `products_new == 50` 断言了。★ 教训：**测试替身造的假数据必须离判定边界足够远**——
    归并判定本身由 test_dedup.py / test_ingest.py 专门覆盖。
    """

    name = "fake"

    def __init__(
        self,
        count: int = 0,
        *,
        prefix: str = "f",
        pages_ok: int = 1,
        pages_blocked: int = 0,
        pages_ugc: int = 0,
        pages_unparsed: int = 0,
        raises: BaseException | None = None,
    ) -> None:
        self.count = count
        self.prefix = prefix
        self.pages_ok = pages_ok
        self.pages_blocked = pages_blocked
        self.pages_ugc = pages_ugc
        self.pages_unparsed = pages_unparsed
        self.raises = raises
        self.calls = 0

    def fetch(self, *, pages: int = 1) -> tuple[str, list[RawDeal], FetchStats]:
        self.calls += 1
        if self.raises is not None:
            raise self.raises

        stats = FetchStats(
            pages_ok=self.pages_ok,
            pages_blocked=self.pages_blocked,
            pages_ugc=self.pages_ugc,
            pages_unparsed=self.pages_unparsed,
            layer="fake",
        )
        deals = [
            RawDeal(
                source="fake",
                source_id=f"{self.prefix}-{self.calls:03d}-{i:03d}",
                title=f"测试商品 {self.prefix} {self.calls} {i}",
                price_raw=f"{100 + i}元",
                recorded_at=utcnow(),
                # 每条一个品牌 → 硬门槛一票否决 → 全部 NEW，不受灰区影响
                brand=f"FakeBrand{i}/假牌{i}",
                platform="京东",
                # ★ 通道必须由源自己声明，管道不兜底（见 ingest.py 的 ValueError）；
                #   假源叫什么就填什么，不用假装是 mock/jingxuan——那些名字有语义
                channel="fake",
            )
            for i in range(self.count)
        ]
        return "fake", deals, stats


# 夹具与工具


@pytest.fixture()
def client(db):
    """把接口的数据库依赖换成测试会话。

    ★ 和 test_api_smoke.py 里那份同一个写法，包括"不用 with"——
    不带 with 时不触发 lifespan，于是不会去碰开发库（理由见那边 docstring）。
    """
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def add_task(db, source: str = "mock", status: str = "pending", **kw) -> CrawlTask:
    """造一个任务行。默认是最普通的"待办"。"""
    kw.setdefault("progress", 0)
    task = CrawlTask(source=source, status=status, **kw)
    db.add(task)
    db.commit()
    return task


def status_of(db, task_id: int) -> str:
    return db.execute(
        select(CrawlTask.status).where(CrawlTask.id == task_id)
    ).scalar_one()


# 一、认领：原子、FIFO、不抢别人的


def test_claim_returns_none_on_empty_queue(db):
    """空队列返回 None，而不是抛异常或者造一个任务出来。"""
    assert worker.claim_next_task(db) is None


def test_claim_is_fifo(db):
    """先入队的先被认领。

    ★ FIFO 不是形式主义：队列乱了，"排队中"的顺序就毫无意义，而人判断"还要等多久"正是靠它。
    """
    first = add_task(db, source="mock")
    second = add_task(db, source="smzdm")

    claimed = worker.claim_next_task(db)

    assert claimed is not None
    assert claimed.id == first.id
    assert status_of(db, first.id) == "running"
    assert status_of(db, second.id) == "pending", "第二个不该被认领"


def test_claim_skips_finished_tasks(db):
    """已经成功 / 失败的任务不会被再认领一次。

    ★ 少了这条，一个跑完的任务会被无限重跑——表现是"采集一直在跑、进度条一直在动"，
    看起来像在工作，实际上在空转。
    """
    done = add_task(db, status="success")
    failed = add_task(db, status="failed")
    pending = add_task(db)

    claimed = worker.claim_next_task(db)

    assert claimed.id == pending.id
    assert status_of(db, done.id) == "success"
    assert status_of(db, failed.id) == "failed"


def test_claim_resets_progress_and_total(db):
    """认领时把 progress / total 清零。

    ★ 不清零的话，重跑的任务从上次的 100% 开始——进度条一开始就是满的，真正跑起来还会**往回退**。
    """
    task = add_task(db, progress=99, total=100, message="上次跑到这里")

    claimed = worker.claim_next_task(db)

    assert claimed.progress == 0
    assert claimed.total is None


# 二、自愈：崩溃遗留的任务要能被打回重跑


def test_recover_stale_running_task(db):
    """running 且很久没更新的任务，打回 pending。

    ★ 场景：worker 被 Ctrl-C。没有这个函数，任务永远停在 47%——既不会被重跑，
    也不会报错，只能手工改数据库。
    """
    stale = add_task(db, status="running")
    stale.updated_at = utcnow() - timedelta(minutes=worker.STALE_TASK_MINUTES + 1)
    db.commit()

    revived = worker.recover_stale_tasks(db)

    assert revived == 1
    assert status_of(db, stale.id) == "pending"
    # ★ 重试的原因要写进 message：不写的话任务"自己动了一下"而没有任何解释
    assert "自动重试" in db.get(CrawlTask, stale.id).message


def test_recover_leaves_fresh_running_task_alone(db):
    """★ 正在正常跑的任务**不能**被打回。

    ★ 判据用 updated_at 而不是 created_at 就是为了这条：跑了 30 分钟的长任务
    created_at 很早但进度一直在更新，用 created_at 判会被打回，然后**两个 worker
    同时跑同一个任务**——重复的请求量对 SMZDM 是实打实的合规风险。
    """
    running = add_task(db, status="running", progress=50, total=100)

    revived = worker.recover_stale_tasks(db)

    assert revived == 0
    assert status_of(db, running.id) == "running"


def test_recover_ignores_success_and_failed(db):
    """成功 / 失败的任务不归自愈管，哪怕它们很旧。"""
    old_success = add_task(db, status="success")
    old_success.updated_at = utcnow() - timedelta(days=30)
    db.commit()

    assert worker.recover_stale_tasks(db) == 0
    assert status_of(db, old_success.id) == "success"


# 三、执行一个任务


def test_run_task_writes_products_and_progress(db):
    """正常路径：商品入库、进度推到 100%、报告计数来自数据库。"""
    task = add_task(db)
    source = FakeSource(count=25)

    run = worker.run_task(db, task, source=source, pages=1)
    worker.finish_task(db, task, run)

    assert run.report.fetched == 25
    assert run.report.products_new == 25
    assert task.status == "success"
    assert task.total == 25
    assert task.progress == 25
    assert db.execute(select(func.count()).select_from(Product)).scalar_one() == 25


def test_report_counters_accumulate_across_batches(db):
    """★★ 回归测试：报告里的计数必须**跨批次累加**。

    ★ "报告变成假数字"的第二个现场（第一个在 ingest 里）：分批入库时只报**最后一批**
    的计数，50 条的任务会显示"新商品 10"而真相是 50。它比第一个更隐蔽——数字变小了，
    而"新商品 10"看起来完全像一批正常的小采集，**没有人会怀疑一个偏小的数字**。
    """
    task = add_task(db)
    source = FakeSource(count=50)

    run = worker.run_task(db, task, source=source, pages=1, progress_every=20)

    # 50 条 / 每批 20 = 3 批（20 + 20 + 10）
    assert run.report.fetched == 50
    assert run.report.products_new == 50, "只报了最后一批的计数"
    assert db.execute(select(func.count()).select_from(Product)).scalar_one() == 50


def test_progress_advances_in_steps(db):
    """进度按批次前进，不是"0 然后突然 100"。

    ★ 分步推进是进度条有用的前提——一次性跳到 100 的进度条和一个转圈的图标没有区别。
    """
    task = add_task(db)
    seen: list[int] = []

    # 每批入库时记一次进度（真实场景里进度是前端轮询读的，这里直接读同一行的值）
    source = FakeSource(count=50)
    original_ingest = worker.ingest.ingest_deals

    def spy(db_, deals, *, source):  # noqa: ARG001
        rep = original_ingest(db_, deals, source=source)
        db_.commit()
        seen.append(task.progress)  # 这一批**之前**的进度（由上一批写入）
        return rep

    worker.ingest.ingest_deals = spy
    try:
        run = worker.run_task(db, task, source=source, pages=1, progress_every=20)
    finally:
        worker.ingest.ingest_deals = original_ingest

    worker.finish_task(db, task, run)

    # 第一批开始时进度还是 0，第二/三批时已经不是了
    assert seen[0] == 0
    assert 0 < seen[1] < 50
    assert task.progress == 50


def test_empty_fetch_is_marked_failed_not_success(db):
    """★★ 一条都没抓到 → **failed**，不是 success。

    ★ "假进度比没有进度更糟"的直接应用：按"没抛异常就算成功"写的话，一条都没抓到的
    任务会显示成 success / "没有解析到任何爆料"、和正常跑完长得一模一样——于是页面改版、
    URL 填错、被 WAF 拦这三种**需要立刻处理**的情况，看起来都像"今天没什么好价"。
    """
    task = add_task(db)

    run = worker.run_task(db, task, source=FakeSource(count=0), pages=1)
    worker.finish_task(db, task, run)

    assert run.report.fetched == 0
    assert task.status == "failed", "一条都没抓到却被判成了成功"
    assert task.progress == 0


@pytest.mark.parametrize(
    ("kwargs", "keyword", "why"),
    [
        ({"pages_blocked": 3}, "拦截", "被 WAF 拦了 —— 对方在挡我们"),
        ({"pages_ugc": 3}, "分类页", "URL 填错了 —— 抓的是分类页"),
        ({"pages_unparsed": 3}, "结构", "页面改版了 —— 该改解析器"),
    ],
)
def test_empty_fetch_message_says_which_kind(db, kwargs, keyword, why):
    """★★ 一条都没抓到时，必须说清楚**是哪一种**没抓到。

    ★ 三种情况界面上长得一样（都是"0 条"），处置却完全相反：被拦截→别重试、别加频率；
    分类页→改配置里的 URL；解析为 0→改解析器。合成一句"没有解析到任何爆料"的话，
    人会跑去研究 WAF，而真相可能是"URL 少写了一个字符"。
    """
    task = add_task(db)

    run = worker.run_task(db, task, source=FakeSource(count=0, **kwargs), pages=1)
    worker.finish_task(db, task, run)

    assert first_fetch_message(task) is not None
    assert keyword in first_fetch_message(task), f"{why}：message 里没提这回事"


def first_fetch_message(task: CrawlTask) -> str:
    """任务 message 的读取点。包一层是为了让上面的断言读起来是句子。"""
    return task.message or ""


@pytest.mark.parametrize(
    ("kwargs", "keyword", "why"),
    [
        ({"pages_blocked": 1}, "拦截", "被 WAF 拦了"),
        ({"pages_ugc": 1}, "分类页", "URL 填错了"),
        ({"pages_unparsed": 1}, "结构", "页面改版了"),
    ],
)
def test_partial_fetch_message_still_says_which_kind(db, kwargs, keyword, why):
    """★★ **只坏了一页**的时候，那句话同样必须出现——这才是页面改版真正发生的样子
    （改版不会让 5 页全部同时坏，通常是"4 页还好、1 页解析出 0 条"）。

    ★ 此形态下任务状态是 success（有数据入库，不是空抓）——**这是对的**，但消息里原先
    一个字都不提那一页：任务看起来完全正常，而 20 条数据已经静默地不在了。
    ★ 断言的是**消息里有那个词**而不是"计数器等于 1"：计数器一直是对的，
    错的是它没有一条路径被送到人眼前——等于没计。
    """
    task = add_task(db)

    # count=20：有数据入库 → 走 success 分支 → 消息来自 as_message()
    run = worker.run_task(db, task, source=FakeSource(count=20, pages_ok=4, **kwargs), pages=5)
    worker.finish_task(db, task, run)

    assert task.status == "success", "「只坏了一页」不该被判成失败"
    msg = first_fetch_message(task)
    assert keyword in msg, f"{why}：部分成功时 message 里没提这回事（{msg!r}）"


# 四、失败路径


def test_fail_task_marks_failed_and_keeps_the_reason(db):
    """失败任务要留下原因，且只留有用的一行（不留 traceback）。

    ★ message 是给前端直接显示的一行字；塞进 traceback 会变成一坨没人看的堆栈、
    反而盖住真正有用的那句话。完整堆栈由 logger.exception 记到日志里。
    """
    task = add_task(db)
    try:
        raise ValueError("解析器里的某个假设不成立了")
    except ValueError as exc:
        worker.fail_task(db, task, exc)

    assert task.status == "failed"
    assert "ValueError" in task.message
    assert "解析器里的某个假设不成立了" in task.message
    assert "Traceback" not in task.message


def test_fail_task_says_blocked_should_not_be_retried(db):
    """★ 被拦截的失败，message 必须明说"不重试"。

    ★ 不说的话，下一个人看到失败任务第一反应是再跑一次——而那恰好是合规上不该做的事
    （重试只会让对方更确定这是脚本）。
    """
    task = add_task(db)
    worker.fail_task(db, task, FetchBlocked("第 1 页被拦截（HTTP 403）"))

    assert task.status == "failed"
    assert "不重试" in task.message


def test_network_disabled_is_not_described_as_blocked(db):
    """★★ 「没联网」不能写成「被拦截」——两者处置完全相反。

    ★ 被真实输出逼出来的：source=smzdm 的任务用默认参数跑，失败信息写着
    "被拦截，已按约定停止（不重试）"，而当时**一个网络请求都没有发出去**——
    照着那句话去查方向是 WAF、换 IP、今天别采了，真相是命令行少了一个 `--allow-network`。

    ★ 两者都会让"采集停止"、很容易合并成一个类型，而合并之后 message 只能是笼统的
    "采集停止"，等于把判断的负担推给读日志的人。
    """
    task = add_task(db)
    worker.fail_task(db, task, NetworkDisabled("SmzdmSource 默认不联网"))

    assert task.status == "failed"
    assert "未联网" in task.message
    assert "--allow-network" in task.message, "没说怎么修"
    # ★ 判据用 FetchBlocked 分支的**独有措辞**，而不是"消息里不能出现'拦截'两个字"——
    #   消息里有句"未联网（不是被拦截）"用来预先打消错误诊断，恰好也含"拦截"；
    #   断言比它要守的规则更严，就会惩罚正确的实现
    assert "已按约定停止" not in task.message, "把'没联网'说成了'被拦截'"
    assert "不重试" not in task.message, "把'没联网'说成了'被拦截'"


def test_one_bad_task_does_not_kill_the_queue(db):
    """★★ 一个坏任务不能让后面排队的全部饿死。

    ★ run_once 里必须捕获**所有**异常：让异常冒出去会杀掉整个 worker 进程，
    后面所有任务永远停在 pending——一个坏任务毒死一整条队列，
    症状是"采集突然就不工作了"，看起来像 worker 没启动。
    """
    bad = add_task(db, source="bad")
    good = add_task(db, source="good")

    def build(name, *, allow_network):
        if name == "bad":
            return FakeSource(raises=RuntimeError("这个源坏了"))
        return FakeSource(count=3, prefix="g")

    original = worker.build_source
    worker.build_source = build
    try:
        ran = worker.run_once(db, pages=1, allow_network=False)
    finally:
        worker.build_source = original

    assert ran == 2, "应该两个任务都被处理过"
    assert status_of(db, bad.id) == "failed"
    assert status_of(db, good.id) == "success", "坏任务把好任务饿死了"
    assert db.execute(select(func.count()).select_from(Product)).scalar_one() == 3


def test_run_once_returns_zero_on_empty_queue(db):
    """没有待办时安静地返回 0，不抛异常。

    ★ 这是常驻轮询模式的正常状态（每秒都要走一次这条路径），抛异常的话 worker 一秒崩一次。
    """
    assert worker.run_once(db, pages=1, allow_network=False) == 0


# 五、数据源工厂


def test_build_source_rejects_unknown_name():
    """未知源名报错，而不是"安静地返回一个 Mock 源"。

    ★ 静默回退的后果：任务显示 success、采的却是 Mock 数据，而库里 source 写着未知的名字——
    统计图上多出一批来路不明的商品，且没有任何报错。
    """
    with pytest.raises(ValueError, match="未知的采集源"):
        worker.build_source("bogus", allow_network=False)


def test_smzdm_source_is_offline_by_default():
    """★ `build_source("smzdm")` 默认**不联网**，哪怕调用方没说。

    SMZDM 有 WAF，实测约 50~100 次请求后会被拦——"今天要不要花掉那 50 次额度"
    必须是有意识的决定（命令行显式加 `--allow-network`），而不是跑个命令的副作用。
    """
    source = worker.build_source("smzdm", allow_network=False)

    assert source.name == "smzdm"
    assert source.allow_network is False
    # ★ 抛的是 NetworkDisabled 而不是 FetchBlocked：「没联网」和「被拦截」必须能分辨，
    #   否则失败任务的提示语会指错方向（见下一个测试）
    with pytest.raises(NetworkDisabled):
        source.fetch(pages=1)


def test_mock_source_uses_defaults_so_it_converges_with_seed(db):
    """★ Mock 源必须用**默认参数**，否则和 seed 直写的库状态不收敛。

    收敛的前提是两边同一组 (count, days, seed)：build_source 里随手传一组小参数，
    `?source=mock` 下的商品就和 seed 灌的那批对不上，采集一次凭空多出一批商品——
    而那批和原来那批长得一模一样，只有 source_id 差一位。
    断言的是参数不是行为（真跑 1000 商品太慢），而"参数是不是默认值"正是会被人改坏的地方。
    """
    from scraper.seed import DEFAULT_DAYS, DEFAULT_PRODUCT_COUNT, DEFAULT_SEED

    source = worker.build_source("mock", allow_network=False)

    assert isinstance(source, MockSource)
    assert source.count == DEFAULT_PRODUCT_COUNT
    assert source.days == DEFAULT_DAYS
    assert source.seed_value == DEFAULT_SEED


# 六、离线重放


def test_replay_missing_files_explains_where_to_look(tmp_path):
    """语料缺失时的报错要给出**目录**，而不是一句 FileNotFoundError。

    ★ 报错不说"该去哪儿找"的话，看到的人只能去翻代码。
    """
    with pytest.raises(FileNotFoundError, match="回放语料缺失"):
        worker.replay(db=None, paths=[tmp_path / "nope.html"])  # type: ignore[arg-type]


def test_replay_parses_real_pages(db):
    """★ 重放真实页面：100 条唯一爆料进库，且 source 是 smzdm（用的是仓库里 5 个真实页面）。

    断言的关键不是"能跑"而是**只有 100 条**：5 个页面原始产出可能超过 100（页面之间有重叠），
    `dedupe_deals` 必须把它们压到 100。★ 不去重数据不会错（唯一约束会挡住重复价格点），
    但报告计数会翻倍、界面显示"解析 140 条"——**统计错了比数据错了更难发现**。
    """
    paths = sorted(worker.DEFAULT_REPLAY_DIR.glob("smzdm_jingxuan_page*.html"))
    assert len(paths) == 5, f"回放语料不全，只找到 {len(paths)} 个页面"

    report = worker.replay(db, paths)

    assert report.fetched == 100, f"去重后应该是 100 条，得到 {report.fetched}"
    assert db.execute(
        select(func.count()).select_from(Product).where(Product.source == "smzdm")
    ).scalar_one() == 100
    # ★ 真实数据里有 2 条没有价格，所以价格点不是 100 个（实测见 test_ingest.py）
    assert report.prices_new == 98


# 七、任务接口


def test_enqueue_creates_pending_task(client, db):
    """入队 → 返回 pending 的任务，**且不在这个请求里采集**。"""

    resp = client.post("/api/tasks/crawl?source=mock")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["source"] == "mock"
    assert body["progress"] == 0
    assert body["total"] is None
    assert body["percent"] is None, "total 未知时 percent 必须是 None，不是 0"
    assert db.execute(select(func.count()).select_from(Product)).scalar_one() == 0


def test_enqueue_rejects_all(client):
    """★ `?source=all` 返回 422 —— "抓全部数据源"不是一个能执行的动作。"""
    resp = client.post("/api/tasks/crawl?source=all")

    assert resp.status_code == 422
    assert "具体的源" in resp.json()["detail"]


def test_enqueue_requires_source(client):
    """不给 source 也是 422（默认值 all 对采集没有意义）。"""
    assert client.post("/api/tasks/crawl").status_code == 422


def test_enqueue_does_not_double_queue_same_source(client, db):
    """★★ 同一个源已有待办时，再提交**不新建任务**，而是返回那一个。

    ★ 这不是防误触的小功能，是合规约束的实现：SMZDM 实测约 50~100 次请求后开始拦截，
    页面上连点五次"采集"就是 500 次请求，足以把额度一次性用光——而且看起来完全像正常操作。
    挡在后端而不是只在前端防抖：前端防抖挡不住刷新页面、挡不住 curl、挡不住两个标签页。

    ★ 返回 **200 + 已有任务**而不是 409：用户意图（"我要看这个源的最新数据"）已经满足了；
    409 会让人以为出错然后去重试——那正是要避免的行为。
    """
    first = client.post("/api/tasks/crawl?source=mock").json()
    second = client.post("/api/tasks/crawl?source=mock")

    assert second.status_code == 200
    assert second.json()["id"] == first["id"], "重复入队造出了第二个任务"
    assert "不再重复入队" in second.json()["message"]
    assert db.execute(select(func.count()).select_from(CrawlTask)).scalar_one() == 1


def test_enqueue_different_sources_are_independent(client, db):
    """不同源之间互不影响——挡的是"同一个源重复提交"，不是全局串行。"""
    mock = client.post("/api/tasks/crawl?source=mock").json()
    smzdm = client.post("/api/tasks/crawl?source=smzdm").json()

    assert mock["id"] != smzdm["id"]
    assert db.execute(select(func.count()).select_from(CrawlTask)).scalar_one() == 2


def test_enqueue_allowed_again_after_task_finishes(client, db):
    """跑完的任务不挡新的——否则这个源就永远采不了了。

    ★ 防重复入队最容易写坏的地方：判定写成"有没有这个源的任务"而不是"有没有**活跃**的任务"，
    于是一个源只要采过一次就再也采不了，症状是"点采集没反应"而任务列表里全是成功的任务。
    """
    task = add_task(db, source="mock", status="success")
    resp = client.post("/api/tasks/crawl?source=mock")

    assert resp.json()["id"] != task.id, "跑完的任务把新任务挡住了"


def test_get_task_404_for_unknown_id(client):
    """不存在的 id → 404。

    ★ 和 stats 的空态契约（200 + notice）不同：那个用于"查询成功但结果为空"，
    而这个 id 是**请求写错了**——两者混用会让前端把"任务被删了"显示成"任务还没开始"。
    """
    resp = client.get("/api/tasks/999999")

    assert resp.status_code == 404
    assert "999999" in resp.json()["detail"]


def test_get_task_reports_percent(client, db):
    """有 total 时 percent 是算好的整数。"""
    task = add_task(db, status="running", progress=30, total=120)

    body = client.get(f"/api/tasks/{task.id}").json()

    assert body["percent"] == 25


def test_percent_is_clamped(client, db):
    """★ percent 夹在 [0,100]：脏数据不能让进度条溢出容器。

    场景：任务重跑时 total 变小（这次只抓到 50 条、上次 100），而 progress 是上一轮的 80 → 160%。
    """
    task = add_task(db, status="running", progress=80, total=50)

    body = client.get(f"/api/tasks/{task.id}").json()

    assert body["percent"] == 100


def test_stale_flag_for_stuck_running_task(client, db):
    """★ running 但很久没更新 → stale=true。

    ★ 它**不改状态**（打回 pending 是 worker 启动时的事），true 的意思是"现在这一刻
    它没有在前进"——前端据此把进度条变成"可能已中断"，而不是无限转圈。
    """
    stuck = add_task(db, status="running", progress=47, total=100)
    stuck.updated_at = utcnow() - timedelta(minutes=worker.STALE_TASK_MINUTES + 1)
    db.commit()

    body = client.get(f"/api/tasks/{stuck.id}").json()

    assert body["stale"] is True
    assert body["status"] == "running"


def test_fresh_running_task_is_not_stale(client, db):
    """正在正常跑的任务不能标成 stale。"""
    task = add_task(db, status="running", progress=47, total=100)

    assert client.get(f"/api/tasks/{task.id}").json()["stale"] is False


def test_list_tasks_newest_first(client, db):
    """任务列表最新在前，且带 limit。

    ★ 前端刷新页面后靠这个恢复"刚才那个任务跑到哪了"：任务状态在数据库里而不是浏览器内存里，
    这正是"任务表而不是 BackgroundTasks"最直接的收益。
    """
    for i in range(5):
        add_task(db, source=f"s{i}")

    body = client.get("/api/tasks?limit=3").json()

    assert body["total"] == 3
    assert [t["source"] for t in body["items"]] == ["s4", "s3", "s2"]


def test_list_tasks_empty(client):
    """没有任务时返回空列表，而不是 404。"""
    body = client.get("/api/tasks").json()

    assert body == {"items": [], "total": 0}
