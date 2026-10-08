"""报告缓存测试 —— 用假客户端证明"第二次调用没有碰网络"。

================================================================================
这个文件唯一的武器：一个会数调用次数的假客户端
================================================================================

"缓存生效了"这件事**没法从返回值上看出来**：

    命中缓存  →  返回一份报告
    没命中    →  调模型，返回一份报告

两者看起来一模一样。想区分只有两条路：

  ① 看耗时（缓存是毫秒级、调模型是十几秒）
  ② **看模型到底被调了几次**

① 是"看现象"，跑在 CI 上会因为机器快慢而飘；② 是"看事实"。
所以 `FakeClient.calls` 是下面所有断言的基石——
`assert client.calls == 1` 比任何"响应看起来对不对"都硬。

★ 这也是 `reporter.generate_report(client=...)` 这个参数存在的唯一理由。
  它不是为了"可测试性"而加的钩子，而是因为**这条链路的正确性
  只能通过替换客户端来验证**。

================================================================================
时间也要控住
================================================================================

`force=1` 之后 `created_at` 应该刷新——这条断言直接比较两次的时间戳，
而 `utcnow()` 的精度是微秒。两次调用挨得足够近时它们会相等，
测试就变成偶发失败（本地全绿、换台机器红，最难查的那种）。

所以 `clock` 夹具把 `reporter` 里的 `utcnow` 换成一个**每次调用前进 1 秒**的假钟。
时间变成确定的，"第二次比第一次晚"才是确定的。
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.analysis import reporter
from app.analysis.glm_client import AnalysisError, GlmClient, GlmResult
from app.source_scope import SourceFilter
from app.db.models import AnalysisReport

# ---------------------------------------------------------------------------
# 假客户端
# ---------------------------------------------------------------------------


def _report_content(marker: str) -> dict:
    """一份结构合法的报告，`marker` 用来区分"这是第几次生成的"。"""
    return {
        "headline": f"该品类价格集中在 100~200 元（{marker}）",
        "sections": [
            {"title": "价格结构", "finding": "价格带较窄。", "evidence": "中位数 149 元"},
            {"title": "品牌格局", "finding": "头部集中在三家。", "evidence": "见 top_brands"},
        ],
        "opportunities": ["可考虑补一个 200 元以上的档位"],
        "risks": ["样本偏向头部"],
        "data_limits": ["这批数据没有销量，无法判断畅销程度"],
    }


class FakeClient:
    """只数调用次数、不联网的分析客户端。

    ★ 它没有继承任何东西——这正是 `AnalysisClient` 用 Protocol
      而不是抽象基类的原因：测试替身不必为了满足类型而背上一个父类，
      将来重构 `GlmClient` 的继承关系也不会连累这里。

    ★ 每次调用的内容**都不一样**（带 `第 N 次调用` 标记）：
      否则"force=1 之后内容被覆盖了"这条断言没法写——
      两次返回同样的内容时，"覆盖成功"和"根本没覆盖"看起来一样。
      这是造测试替身时最容易漏的一点：**替身要能暴露变化**。
    """

    def __init__(self, model: str = "fake-model", tokens_per_call: int = 100) -> None:
        self.model = model
        self.calls = 0
        self.payloads: list[dict] = []
        self._tokens_per_call = tokens_per_call

    def analyze(self, payload: dict) -> GlmResult:
        self.calls += 1
        self.payloads.append(payload)
        return GlmResult(
            content=_report_content(f"第 {self.calls} 次调用"),
            tokens_used=self._tokens_per_call * self.calls,
            raw_text='{"headline": "…"}',
        )


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def clock(monkeypatch):
    """把 `reporter` 用的 utcnow 换成每次调用前进 1 秒的假钟。

    ★ 只补 `app.analysis.reporter` 这一个名字，不动 `app.db.base.utcnow`。
      因为 reporter 是 `from app.db.base import utcnow` 拿进来的——
      它持有的是**一个函数对象**，所以补它自己的那个引用才有效，
      改 `app.db.base.utcnow` 是没用的（那个名字改完之后，reporter 里的
      引用还指向老函数）。这是 monkeypatch 最容易搞错的地方：
      **补的是"谁在用"，不是"东西在哪定义的"。**
    """
    start = datetime(2026, 1, 1, 12, 0, 0)
    ticks = itertools.count()

    def fake_utcnow() -> datetime:
        return start + timedelta(seconds=next(ticks))

    monkeypatch.setattr(reporter, "utcnow", fake_utcnow)
    return start


def _seed(make_product) -> None:
    """灌 3 个 Mock 商品，让数据源非空。"""
    make_product(sid="m-1", title="安克 充电宝 20000毫安", price=199.0, sales=1000, points=3)
    make_product(sid="m-2", title="绿联 充电宝 10000毫安", price=99.0, sales=2000, brand="绿联")
    make_product(sid="m-3", title="倍思 氮化镓充电器 65W", price=149.0, sales=500, brand="倍思")


def _rows_from_db(db) -> list[AnalysisReport]:
    """重新从**数据库**读报告行。

    ★ `expunge_all()` 不是可有可无的一句，去掉它整个测试就废了。

      `_upsert_report` 用的是 Core 的 `insert().on_conflict_do_update()`，
      它**绕过 ORM 的 unit of work**——SQLAlchemy 不会去更新身份映射里
      那个已经加载过的 `AnalysisReport` 对象。再叠加测试会话的
      `expire_on_commit=False`（见 conftest），那个对象会一直带着**旧值**。

      后果非常难看出来：`db.query(AnalysisReport).first()` 返回的是
      **同一个 Python 对象**，于是
        · `content_json` 还是旧的 → "覆盖没生效"
        · `id == old_id` 变成恒真     → 这条断言实际上是空的
      两条断言一条假红、一条假绿，而且都不会有任何提示。

    ★ 那线上会不会有同样的问题？不会——`get_db()` 每请求一个新会话，
      身份映射是空的。所以这是测试替身引入的假象，不是产品缺陷；
      但**测试必须自己负责把这个假象消掉**，否则它测的是内存而不是数据库。
    """
    db.expunge_all()
    return list(db.execute(select(AnalysisReport)).scalars())


# ===========================================================================
# 一、★ 缓存命中：第二次调用不许碰客户端
# ===========================================================================


def test_the_first_call_goes_to_the_model(db, make_product) -> None:
    """先确认基线：第一次一定是真的调了模型。

    ★ 这条看着多余，其实必须有：下面所有"第二次没调"的断言，
      只有在"第一次确实调了"成立时才有意义。
      没有它的话，一个**从来没调过模型的实现**能让下面全部测试变绿。
    """
    _seed(make_product)
    client = FakeClient()

    outcome = reporter.generate_report(db, SourceFilter.all, client=client)

    assert client.calls == 1
    assert outcome.from_cache is False
    assert outcome.notice is None


def test_the_second_call_never_touches_the_client(db, make_product, clock) -> None:
    """★★ 同一份数据第二次生成报告，客户端调用次数必须**仍然是 1**。

    这是"缓存生效"唯一的硬证据。断言写成 `== 1` 而不是 `+= 0`：
    后者只说明"这次没调"，前者还同时保证了"总共只调过一次"——
    如果哪次改动让第一次调用变成两次，这里也会红。
    """
    _seed(make_product)
    client = FakeClient()

    reporter.generate_report(db, SourceFilter.all, client=client)
    outcome = reporter.generate_report(db, SourceFilter.all, client=client)

    assert client.calls == 1, "第二次调用又去打了一次模型——缓存没生效"
    assert outcome.from_cache is True
    assert outcome.notice is None


def test_the_cached_call_returns_the_same_content(db, make_product, clock) -> None:
    """命中缓存时拿到的内容，和第一次**逐字相同**。

    ★ 用 `==` 比较整个 dict，而不是挑几个字段比。
      "缓存返回的内容"应该就是"第一次那份内容"，
      挑字段比会漏掉"某一层被重新组装过"这类问题——
      而重新组装正是缓存实现里常见的走样方式。
    """
    _seed(make_product)
    client = FakeClient()

    first = reporter.generate_report(db, SourceFilter.all, client=client)
    second = reporter.generate_report(db, SourceFilter.all, client=client)

    assert second.content == first.content
    assert second.fingerprint == first.fingerprint
    assert second.model == first.model


def test_the_cached_call_keeps_the_original_created_at(db, make_product, clock) -> None:
    """★ 从缓存返回时，`created_at` 必须是**原来那个**，不是 now()。

    这条不说的话很容易写错：`ReportOutcome` 是新建的对象，
    顺手填 `utcnow()` 是最自然的写法——而那样界面上的"生成时间"
    会显示"刚刚"，可内容是三天前的。**报告内容和生成时间对不上，
    比显示旧报告更让人困惑**：你会以为它是新的，然后照着过期的数字做判断。

    ★ `clock` 让每次 utcnow 前进 1 秒，所以"第一次"和"第二次"的
      时间戳在假钟下**必然不同**——这条断言才是确定的。
    """
    _seed(make_product)
    client = FakeClient()

    first = reporter.generate_report(db, SourceFilter.all, client=client)
    second = reporter.generate_report(db, SourceFilter.all, client=client)

    assert second.created_at == first.created_at, "从缓存返回时把生成时间刷成了现在"
    assert second.created_at != clock + timedelta(seconds=1)


def test_cached_content_survives_the_json_round_trip(db, make_product, clock) -> None:
    """★ 缓存的内容经过 `content_json` 存了又读，结构不能走样。

    它测的是"报告被真正落库了"，而不是"被塞在某个进程内存字典里"。
    ★ 内存缓存也能让上面几条全绿——但重启服务就没了，
      而且换一个数据源、换一个模型时容易串。
      这里额外断言数据库里确实有那一行，把"落库了"钉死。
    """
    _seed(make_product)
    client = FakeClient()

    first = reporter.generate_report(db, SourceFilter.all, client=client)
    # 把会话里的一切都忘掉，逼第二次真的从库里读
    db.expunge_all()

    second = reporter.generate_report(db, SourceFilter.all, client=client)

    assert second.content == first.content
    assert client.calls == 1
    assert len(_rows_from_db(db)) == 1


# ===========================================================================
# 二、★ force=1：必须重算，而且必须**覆盖**那一行
# ===========================================================================


def test_force_calls_the_model_again(db, make_product, clock) -> None:
    """`force=1` 的意义是"我知道有缓存，但我就是要重算一次"。

    ★ 这个出口是必需的：指纹缓存是对的，但**没有强制刷新的出口
      会在演示现场变成尴尬**——你说"我再生成一次"，它秒回旧报告，
      看起来就像坏了。
    """
    _seed(make_product)
    client = FakeClient()

    reporter.generate_report(db, SourceFilter.all, client=client)
    outcome = reporter.generate_report(db, SourceFilter.all, client=client, force=True)

    assert client.calls == 2
    assert outcome.from_cache is False


def test_force_overwrites_the_row_instead_of_inserting_a_new_one(db, make_product, clock) -> None:
    """★★ `force=1` 必须 **UPDATE 那一行**，不能 INSERT 一行新的。

    ★ 为什么这条是重点：指纹没变，所以那一行已经在表里了。
      用 INSERT 会直接撞 `fingerprint` 上的唯一索引 → `IntegrityError`。
      也就是说这个 bug 的表现是"点重新生成 → 报错"，
      **功能完全不可用**，而且在开发时不一定被发现——
      因为开发时你通常是在"数据变了"的情况下点的（那时指纹是新的，不冲突）。
      它只在"数据没变、我就是想重算一次"时炸——恰好是 force 唯一的用途。

    ★ 四条断言合起来才说明"是同一行被改了"：
        行数还是 1  → 没插新的
        id 没变     → 确实是同一行
        内容变了     → 确实被更新了
        token 变了   → 更新的不只是内容
    """
    _seed(make_product)
    client = FakeClient()

    reporter.generate_report(db, SourceFilter.all, client=client)
    before = _rows_from_db(db)
    assert len(before) == 1
    old_id, old_created_at = before[0].id, before[0].created_at
    old_content, old_tokens = before[0].content_json, before[0].tokens_used

    reporter.generate_report(db, SourceFilter.all, client=client, force=True)
    after = _rows_from_db(db)

    assert len(after) == 1, "force=1 插了一行新的——两个指纹相同的缓存项会同时存在"
    assert after[0].id == old_id, "id 变了，说明不是同一行"
    assert after[0].content_json != old_content  # ★ 能读到新内容 = 真的落库了
    assert after[0].tokens_used != old_tokens
    assert after[0].created_at > old_created_at, "覆盖之后生成时间没有刷新"


def test_force_does_not_change_the_fingerprint(db, make_product, clock) -> None:
    """★ `force=1` 重算之后指纹不变——它本来就是为了"重刷同一个缓存项"。

    ★ 这条是上一条的前提：如果 force 顺带把指纹也改了，
      那就是"插了一行新的"而不是"覆盖"，上一条的行数断言会红。
      单独拎出来测是因为它更能说明意图：**force 改的是内容，不是身份。**
    """
    _seed(make_product)
    client = FakeClient()

    first = reporter.generate_report(db, SourceFilter.all, client=client)
    forced = reporter.generate_report(db, SourceFilter.all, client=client, force=True)

    assert forced.fingerprint == first.fingerprint


# ===========================================================================
# 三、★ 空数据源：不调模型
# ===========================================================================


def test_empty_source_never_calls_the_model(db, clock) -> None:
    """★★ 一个商品都没有的数据源，不能去调模型。

    ★ 断言方式是"调用次数为 0"，**不是**"返回的内容是什么"。
      因为后者区分不了这两种情况：
        · 没调模型，直接返回了"数据不足"
        · 调了模型，模型自己写了"数据不足"
      而这两件事的成本天差地别：一个是零，另一个要花掉 token 并等十几秒，
      然后**必然**得到一段废话（没有数据，模型除了说"数据不足"写不出别的）。

    ★ 判据是 payload 里的商品数，不是"报告表里有没有行"。
      后者会把"数据被清空了、但旧报告还在"当成有数据，
      然后生成一份描述**已经不存在的清空**的报告。
    """
    client = FakeClient()  # 库是空的，一个商品都没有

    outcome = reporter.generate_report(db, SourceFilter.all, client=client)

    assert client.calls == 0, "空数据源也去调模型了"
    assert outcome.notice == reporter.NO_DATA_NOTICE
    assert outcome.content == {}
    assert _rows_from_db(db) == [], "空数据源不该往报告表里写东西"


def test_empty_source_returns_a_notice_instead_of_raising(db, clock) -> None:
    """空态沿用全项目的契约：不抛异常，给一句**能直接显示给用户**的话。

    ★ 换成抛异常或返回 404 的话，前端要么白屏、要么把栈回溯显示出来。
      而这句 `notice` 是设计给"显示在报告位置上"的文案——
      它解释了**为什么空**（尚未采集）和**怎么办**（先灌数据）。
    """
    outcome = reporter.generate_report(db, SourceFilter.all, client=FakeClient())

    assert outcome.notice
    assert "数据" in outcome.notice  # 至少让用户看出这是数据问题而非程序坏了
    assert outcome.fingerprint == ""
    assert outcome.from_cache is False


def test_empty_source_does_not_consult_the_cache(db, make_product, clock) -> None:
    """★ 先把数据源灌满、生成报告，再把数据清空——此时**必须**返回空态提示。

    ★ 这条测的是"判据用的是 payload 里的商品数，而不是报告表里有没有行"。
      如果实现写成"表里有报告就直接返回"，这里会返回那份**描述已经不存在的
      数据**的旧报告，而且不报错——是那种"看起来一切正常，但屏幕上说的
      东西你已经删掉了"的错误。

    ★ 顺带也覆盖了删除路径：外键约束要求先删价格历史再删商品。
    """
    _seed(make_product)
    client = FakeClient()
    reporter.generate_report(db, SourceFilter.all, client=client)
    assert len(_rows_from_db(db)) == 1

    # ★ 清空的顺序受外键约束：价格历史引用商品，必须先删它。
    #   （这个顺序在 scraper/seed.py 里也是同一条，
    #     只是那边清的是 source='mock' 的那一批。）
    from app.db.models import PriceHistory, Product

    db.query(PriceHistory).delete()
    db.query(Product).delete()
    db.commit()

    outcome = reporter.generate_report(db, SourceFilter.all, client=client)

    assert outcome.notice == reporter.NO_DATA_NOTICE
    assert client.calls == 1, "数据清空之后又去调了一次模型"
    # ★ 旧报告那一行还在（这是对的——缓存本身没问题），
    #   只是这次请求的返回不该用它。
    assert len(_rows_from_db(db)) == 1


# ===========================================================================
# 四、缓存键：什么该命中、什么不该
# ===========================================================================


def test_different_sources_are_separate_cache_entries(db, make_product, clock) -> None:
    """★ mock 和 smzdm 是两个独立的缓存项。

    同一个库、同一张报告表，靠指纹分开。如果它们共用缓存，
    `?source=smzdm` 会拿到一份根据 Mock 数据写的报告——
    而 Mock 有销量、真实数据没有，报告里会出现"销量最高的商品是……"这种
    在真实数据上**根本不成立**的结论。
    """
    _seed(make_product)
    make_product(sid="s-1", title="今日必买：京觅 猕猴桃", source="smzdm", price=17.5, sales=None)
    client = FakeClient()

    mock = reporter.generate_report(db, SourceFilter.mock, client=client)
    smzdm = reporter.generate_report(db, SourceFilter.smzdm, client=client)

    assert client.calls == 2, "两个数据源共用了缓存"
    assert mock.fingerprint != smzdm.fingerprint
    assert len(_rows_from_db(db)) == 2

    # 各自再取一次，都应该命中自己那一份
    assert reporter.generate_report(db, SourceFilter.mock, client=client).from_cache is True
    assert reporter.generate_report(db, SourceFilter.smzdm, client=client).from_cache is True
    assert client.calls == 2


def test_changed_data_misses_the_cache(db, make_product, clock) -> None:
    """★ 数据变了，缓存必须失效——否则报告会说已经过期的数字。

    这条是"指纹缓存"这个设计的全部意义所在：
    它必须对**数据变化**敏感，同时对**时间流逝**不敏感。
    """
    _seed(make_product)
    client = FakeClient()
    reporter.generate_report(db, SourceFilter.all, client=client)

    # 加一个商品（模拟又采集到一条）
    make_product(sid="m-4", title="酷态科 充电宝 10000毫安", price=129.0, sales=800, brand="酷态科")
    db.commit()

    outcome = reporter.generate_report(db, SourceFilter.all, client=client)

    assert client.calls == 2, "数据变了还命中缓存——报告会基于过期数字"
    assert outcome.from_cache is False
    assert len(_rows_from_db(db)) == 2, "新指纹应该是一个新的缓存项"


def test_a_price_change_alone_misses_the_cache(db, make_product, clock) -> None:
    """★ 只改一个价格也要重新分析。

    ★ 这条单独写是因为"价格"是那种容易被判定为"无所谓"的字段：
      有人会觉得"同一个商品、同一个标题，改个价格不用重算吧"——
      而报告里恰恰有"价格结构""性价比"这些直接依赖价格的结论。
    """
    make_product(sid="m-1", title="安克 充电宝", price=199.0, sales=1000)
    client = FakeClient()
    reporter.generate_report(db, SourceFilter.all, client=client)

    from app.db.models import Product

    row = db.query(Product).filter_by(source_id="m-1").one()
    row.price = 249.0
    # ★ price_max 一起改。单值时两者相等（见 models.py 的语义说明），
    #   只改一个会造出 `price > price_max` 这种库里不该存在的状态，
    #   而它会让这条测试测到的东西变得含糊。
    row.price_max = 249.0
    db.commit()

    assert reporter.generate_report(db, SourceFilter.all, client=client).from_cache is False
    assert client.calls == 2


def test_a_different_model_is_a_different_cache_entry(db, make_product, clock) -> None:
    """★ 换模型必须换缓存项：同样的数据在 glm-4-flash 和 glm-4-plus 上
    会得到不同的结论，一份报告不能冒充另一份。

    ★ 模型名来自**客户端自己**（`client.model`），不是 settings。
      用 settings 的话，测试里注入一个叫 fake-model 的假客户端时，
      指纹会按配置里的模型名算——于是"换了模型指纹就该变"这条
      **在测试里永远测不到真东西**（因为两次都取到同一个 settings 值）。
    """
    _seed(make_product)

    flash = reporter.generate_report(db, SourceFilter.all, client=FakeClient(model="glm-4-flash"))
    plus = reporter.generate_report(db, SourceFilter.all, client=FakeClient(model="glm-4-plus"))

    assert flash.fingerprint != plus.fingerprint
    assert plus.from_cache is False
    assert plus.model == "glm-4-plus"
    assert len(_rows_from_db(db)) == 2


def test_a_client_without_a_model_attribute_falls_back(db, make_product, clock) -> None:
    """★ 假客户端没有 `model` 属性时回落到 "unknown-model"，而不是抛异常。

    ★ 这是 `_client_model` 用 `getattr(client, "model", None)` 而不是
      给 Protocol 加一个属性要求的理由：Protocol 该规定"至少要有什么"，
      而这里要的是"最好有什么"。逼所有测试替身都实现一个 model 属性，
      只会让测试更难写，换来的只是一个本来就有默认值的值。

    ★ 回落到 `"unknown-model"` 而不是抛异常——它是**缓存键的一部分**，
      不是要显示给用户的东西。多一个"unknown-model"的缓存项，
      比"分析接口 500"好得多。
    """

    class MinimalClient:
        """只有 analyze 一个方法——Protocol 要求的全部。"""

        def __init__(self) -> None:
            self.calls = 0

        def analyze(self, payload: dict) -> GlmResult:
            self.calls += 1
            return GlmResult(content=_report_content("最小客户端"), tokens_used=1, raw_text="{}")

    _seed(make_product)
    outcome = reporter.generate_report(db, SourceFilter.all, client=MinimalClient())

    assert outcome.model == "unknown-model"
    assert outcome.fingerprint


# ===========================================================================
# 五、latest_report：页面打开时"有没有报告可以显示"
# ===========================================================================


def test_latest_report_on_an_empty_table_returns_a_notice(db) -> None:
    """★ 还没有任何报告时，返回 200 + 一句提示，**不是 404**。

    ★ 这是全项目的空态契约（和 stats 接口一致）：
      "这里现在没有东西"是一个**正常的答案**，不是"你请求的资源不存在"。
      改成 404 的话，前端要为一个正常状态写一段错误处理，
      而那段错误处理的文案最终还是会写成"还没有生成过报告"——
      绕一圈回到同一句话，只是多了一个分支。
    """
    outcome = reporter.latest_report(db)

    assert outcome.notice == reporter.NO_REPORT_NOTICE
    assert outcome.content == {}
    assert outcome.fingerprint == ""


def test_latest_report_returns_the_newest_one(db, make_product, clock) -> None:
    """★ 表里有报告时，返回**最近**生成的那一份。

    ★ 顺序按 `created_at` 排，并用 `id` 兜平局：SQLite 把 DateTime 存成
      `"YYYY-MM-DD HH:MM:SS.ffffff"` 字符串，字典序等于时间序，
      所以直接 `ORDER BY created_at DESC` 是对的；
      但同一微秒写入两行时（批量生成就会遇到），必须有个确定的兜底。
    """
    _seed(make_product)
    make_product(sid="s-1", title="今日必买：京觅 猕猴桃", source="smzdm", price=17.5, sales=None)
    client = FakeClient()

    reporter.generate_report(db, SourceFilter.mock, client=client)
    newer = reporter.generate_report(db, SourceFilter.smzdm, client=client)

    outcome = reporter.latest_report(db)

    assert outcome.fingerprint == newer.fingerprint
    assert outcome.content == newer.content


def test_latest_report_never_calls_the_model(db, make_product, clock) -> None:
    """★ 这个接口只是"看看有没有"，绝不能顺手生成一份。

    ★ 页面每次打开都会调它。如果它顺手生成报告，
      那打开分析页面就等于花钱调模型——而且是在用户没点按钮的情况下。
    """
    _seed(make_product)
    client = FakeClient()
    reporter.generate_report(db, SourceFilter.all, client=client)

    reporter.latest_report(db)
    reporter.latest_report(db)

    assert client.calls == 1


# ===========================================================================
# 六、没配 key：错误的形状
# ===========================================================================


def test_missing_key_raises_an_actionable_error(monkeypatch) -> None:
    """★★ 没配 key 时，抛出的必须是**能照着做**的错误。

    ★ 断言的是错误信息里"有文件路径、有下一步动作"，不是文案好看。
      对比一下两种做法：

        · 让 openai SDK 自己去报 → `AuthenticationError: Error code: 401`
          （key 为空时甚至是 `TypeError: __init__() got an unexpected
           keyword argument` 这类完全指错方向的消息）
        · 自己先检查           → "请在 .env 里填入……然后重启服务"

      前者的后果不是"用户体验差"，而是**排查方向被带偏**：
      401 会让人去怀疑 key 过期、怀疑网络、怀疑额度，
      唯独不会想到"我根本没填"。

    ★ 用 `monkeypatch.setattr(settings, "glm_api_key", "")` 而不是
      改真的 .env —— 测试必须能在"没配 key 的机器"和"配了 key 的机器"上
      跑出一模一样的结果。
    """
    from app.config import settings

    monkeypatch.setattr(settings, "glm_api_key", "")

    with pytest.raises(AnalysisError) as excinfo:
        GlmClient().analyze({"summary": {"product_count": 1}})

    message = str(excinfo.value)
    assert "GLM_API_KEY" in message, "没说清楚是哪个配置项"
    assert ".env" in message, "没告诉用户去哪儿改"
    assert "重启" in message, "没说明改完要重启（配置是启动时读进内存的）"


def test_a_placeholder_key_is_treated_as_missing(monkeypatch) -> None:
    """★ `.env.example` 里的示例值被当成真的填进去 —— 这是真实发生过的。

    ★ 它的症状和"没配"完全不同：key 非空，于是会真的发出请求，
      然后收到 401。而 401 的排查方向是"key 过期/额度用尽"，
      不是"你填的是示例值"。所以在**本地**就判为未配置，
      等于把一个线上才发现的错误提前到了本机。
    """
    from app.config import settings

    monkeypatch.setattr(settings, "glm_api_key", "your_key_here")

    with pytest.raises(AnalysisError):
        GlmClient().analyze({"summary": {"product_count": 1}})


def test_an_explicit_key_on_the_client_is_respected(monkeypatch) -> None:
    """★ 显式传给 `GlmClient(api_key=...)` 的 key 必须"算数"。

    ★ 这条测的是一个**检查对象和实际使用对象不一致**的漏洞：
      `_get_client()` 原本调的是不带参数的 `key_configured()`，
      它读的是 settings——于是 `GlmClient(api_key="真key")` 在
      settings 没配时会被判为"未配置"，报了错，可 key 明明传进来了。

      这类问题的共同形状是"校验的是 A，用的是 B"，
      它不会出现在主路径上（主路径两边都是 settings），
      只在覆盖参数被用到时才现形——而那时你会以为覆盖参数本身坏了。

    ★ 这里没法真的走通调用（会去联网），只断言"不再抛未配置错误"。
      所以给一个**能过检查但连不上的地址**，让它在网络层失败——
      失败的原因不同就够了，这正是要区分的两件事。
    """
    from app.config import settings

    monkeypatch.setattr(settings, "glm_api_key", "")

    client = GlmClient(
        api_key="explicitly-passed-key-0123456789",
        base_url="http://127.0.0.1:1/v1",  # 必然连不上
        timeout=0.5,
        max_retries=0,
    )

    with pytest.raises(AnalysisError) as excinfo:
        client.analyze({"summary": {"product_count": 1}})

    assert "未配置" not in str(excinfo.value), "显式传入的 key 被当成了未配置"
    assert "调用 GLM 失败" in str(excinfo.value)


def test_key_configured_never_returns_the_key_itself(monkeypatch) -> None:
    """★ `key_configured` 只回答"配没配"，**绝不回显 key**。

    ★ `/api/health` 调它，而健康检查的返回值最容易被随手贴进
      issue、截图、聊天记录里。一旦它带上了 key，
      泄露就发生在"为了排查问题而分享信息"的过程中，
      而且当事人完全没有感觉。
      （`test_api_smoke.py` 里那条健康检查的断言守着同一个点。）
    """
    from app.analysis.glm_client import key_configured
    from app.config import settings

    secret = "a-real-looking-secret-key-0123456789"
    monkeypatch.setattr(settings, "glm_api_key", secret)

    result = key_configured()

    assert result is True, "返回值必须是布尔值，不能是 key 本身"
    assert secret not in repr(result)
