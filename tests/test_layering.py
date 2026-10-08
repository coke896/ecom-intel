r"""分层方向：把"注释里的规矩"变成"跑测试时会红的东西"。

================================================================================
这份测试是为什么写的
================================================================================
`app/api/images.py` 里写着本项目的架构规矩：

    "那个 import 会让 **Web 层依赖采集层**，方向正好和本项目的架构相反"

而同一个仓库里，`app/api/alerts.py` 有一行

    from scraper.check_alerts import RULE_PRICE_DROP_PCT, RULE_TYPES, check_alerts

**规矩是它自己立的，破规矩的也是它自己。**

★ 这件事的教训不是"记得改注释"，而是：

    写下来的规矩没有**执行者**。它唯一的守卫是"下次有人刚好看见"。

  这和本项目错 12 是同一个形状（那份文档把 854 字节的源码列在"空文件"
  名单里，于是它既没被贴出来、又被说成是空的）：**名单会漂移，
  而现实不会提醒你**。当时的修法是加一条守门断言，这里也是。

★ 所以下面这些用例检查的不是"行为对不对"，是"**目录结构有没有走样**"。
  它们用 AST 解析源码，不 import 被测模块本身——因为要检查的正是
  "某个 import 存不存在"，而 import 一下就会真的执行它。

================================================================================
本项目实际的分层（以及为什么是这个形状）
================================================================================
                      app/db  (engine + 7 张表的模型)
                        ▲                    ▲
        ┌───────────────┘                    └──────────────┐
   app/  （Web 进程）                              scraper/ （采集进程）
   ├── source_scope / textutil / keywords / aggregate    ├── cleaner / dedup
   ├── alerts_engine   ← 告警状态机（纯 DB 逻辑）          ├── sources/
   ├── analysis/       ← 4 个单向依赖的模块               ├── store / ingest
   └── api/            ← FastAPI 端点                     └── worker / seed

规矩一条，没有例外：

    **scraper/ 可以 import app/；app/ 绝不 import scraper/。**

★ 为什么是这个方向而不是反过来：
  `scraper/` 是**部署单元**（另一个进程、另一份职责），
  `app/` 是**被依赖的领域层**（表结构、数据源约定、告警规则）。
  采集进程当然要知道表长什么样；而 Web 进程没有任何理由知道
  采集器怎么发请求——它连 Playwright 都不该 import 进来。

★ 这条规矩里"反向"曾经有过两处例外，都是 2026-10-08 修掉的：
  · `app/api/alerts.py` → `scraper.check_alerts`
    （模块已挪到 `app/alerts_engine.py`）
  · `app/analysis/{payload,reporter}.py` → `app.api.deps`
    （`SourceFilter` / `apply_source` 已挪到 `app/source_scope.py`）

  第二处不是"跨进程"，是**跨层**：分析层 import 了 Web 层。
  下面第三条用例把它也钉住了。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.config import settings

REPO = Path(__file__).resolve().parent.parent


# ===========================================================================
# 小工具：把一棵目录下的所有 .py 解析成 AST，回抛 (文件, import 的模块名) 对
# ===========================================================================


def _imported_modules(path: Path):
    r"""回抛 `(文件, 被 import 的模块名字符串)`。

    ★ 用 `ast.walk` 而不是正则：正则看不出 `import a.b` 和
      `"import a.b"`（字符串里的一句话）的区别，而本项目里有**大量**
      注释和文档在引用 import 语句当例子——`app/alerts_engine.py` 的
      docstring 里就原样写着 `from scraper.check_alerts import ...`，
      那是历史说明，不是真的 import。正则会把它们全部误报。

    ★ 两种 import 形式都要覆盖：
        import a.b.c          → `a.b.c`
        from a.b import c     → `a.b`（模块部分）
      `level > 0` 的是相对 import（`from . import x`），在本项目里
      一处都没有，但真出现了也不该被当成绝对 import 去误判，所以跳过。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield path, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:            # 相对 import，跳过
                continue
            if node.module:
                yield path, node.module


def _py_files(subdir: str):
    return sorted((REPO / subdir).rglob("*.py"))


# ===========================================================================
# 一、规矩的主体：app/ 绝不 import scraper/
# ===========================================================================


def test_app_never_imports_scraper():
    r"""★ 本文件存在的全部理由。

    这条断言失败时，报错信息要能直接告诉人**怎么办**——
    因为它的读者多半是"刚写了个很自然的 import、然后被这里拦住"的人，
    而不是"正在重构分层的人"。所以下面把两条出路都写出来。
    """
    offenders = [
        f"{p.relative_to(REPO).as_posix()}:{i}  import {mod}"
        for p in _py_files("app")
        for i, (f, mod) in enumerate(_imported_modules(p), 1)
        if mod == "scraper" or mod.startswith("scraper.")
    ]
    assert not offenders, (
        "app/ 里出现了对 scraper/ 的 import —— 依赖方向反了。\n  "
        + "\n  ".join(offenders)
        + "\n\n两条出路，按'这段代码到底属于谁'选：\n"
        "  · 它要是纯逻辑（读库、算数、状态机），挪进 app/ —— "
        "采集进程照样能 import app/，方向是通的；\n"
        "  · 它要是真的在发请求 / 解析页面，那 Web 层就不该用它，"
        "把调用点从 Web 层删掉。\n"
        "★ 别改这条测试，也别把 import 挪进函数体里——"
        "两种都是把问题藏起来而不是解决它。"
    )


def test_the_layering_rule_is_stated_where_people_will_read_it():
    r"""规矩本身也得有人写着，否则下一个人只会看到一条"莫名其妙的测试"。

    ★ 这条检查的是 `app/api/images.py` 里那段注释还在不在。
      听起来像在测注释——但它测的其实是**这段话有没有被静默删掉**：
      那段注释里包含一个 3 行的 User-Agent 字符串副本，重构时很容易
      被当成"重复代码"清掉（它会让人想"为什么不 import 那个现成的？"），
      而一旦清掉，`from scraper... import HEADERS` 看起来就完全合理了，
      下一个人加回去的时候不会有任何阻力。
    """
    text = (REPO / "app" / "api" / "images.py").read_text(encoding="utf-8")
    assert "Web 层依赖采集层" in text, (
        "app/api/images.py 里那段解释'为什么 User-Agent 要重写一遍而不敢 import'"
        "的注释不见了。它是这条规矩的说明书，删了之后下面这条测试就变成了"
        "一条没有理由的约束。"
    )


# ===========================================================================
# 二、同一件事，换个方向：分析层不 import Web 层
# ===========================================================================


def test_analysis_layer_does_not_import_the_web_layer():
    r"""★ 分析层（`app/analysis/`）不许 import `app/api/`。

    这一条比上面那条更容易被无意破坏，因为它**在同一个包内部**，
    看起来只是"两个模块互相帮忙"，没有跨进程那么扎眼。

    实际发生过的事：`app/analysis/payload.py` 需要"按数据源过滤"这个能力，
    而它当时住在 `app/api/deps.py` 里，于是很自然地写了

        from app.api.deps import SourceFilter, apply_source

    ★ 这个 import 之所以显得自然，是因为**那个模块放错了地方**——
      不是这个 import 错了。`SourceFilter` 是个纯枚举，`apply_source`
      是个纯 SQLAlchemy 函数，两者都不知道 HTTP 是什么。
      它们被 Web 层、分析层、脚本层共用，却住在 Web 层的目录下。

    → 修法是挪模块（`app/source_scope.py`），不是在这里加例外。
    """
    offenders = [
        f"{p.relative_to(REPO).as_posix()}  import {mod}"
        for p in _py_files("app/analysis")
        for _f, mod in _imported_modules(p)
        if mod == "app.api" or mod.startswith("app.api.")
    ]
    assert not offenders, (
        "app/analysis/ 里出现了对 app/api/ 的 import —— 领域层依赖了 Web 层。\n  "
        + "\n  ".join(offenders)
        + "\n\n多半是某个共用的小东西住在 app/api/ 下面（比如 SourceFilter）。\n"
        "正确的修法是把它挪到 app/ 根下（和 source_scope / textutil / aggregate 并列），\n"
        "让 Web 层和领域层都去 import 它，而不是让领域层 import Web 层。"
    )


def test_the_new_home_of_those_shared_things_is_not_under_api():
    r"""★ 防止"挪出去的又被挪回来"。

    上一条用例只说了"分析层不许 import app.api"。如果有人把
    `SourceFilter` 挪回 `app/api/deps.py`，然后让 payload 改从别处
    （或者干脆在 payload 里重新定义一份）拿——上一条会绿，但问题回来了。

    这条直接钉住结果：这两个符号必须住在 `app/` 根下那个模块里，
    而且**只能有一份定义**。
    """
    scope = REPO / "app" / "source_scope.py"
    assert scope.exists(), (
        "app/source_scope.py 不见了。SourceFilter / apply_source 必须住在"
        " app/ 根下（谁都能 import 的中立位置），不能回 app/api/ 下面去。"
    )
    tree = ast.parse(scope.read_text(encoding="utf-8"))
    defined = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef))
    }
    assert {"SourceFilter", "apply_source"} <= defined, (
        f"app/source_scope.py 里少了定义，只找到 {sorted(defined)}"
    )


# ===========================================================================
# 三、同一条规则只许写一遍（本项目第七类形状的守门）
# ===========================================================================


def _stale_assignment_source(path: Path):
    r"""回抛该文件里 STALE 常量的赋值**来源**（模块级赋值的右值 AST 节点）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for t in targets:
            if isinstance(t, ast.Name) and "STALE" in t.id.upper():
                yield t.id, value


@pytest.mark.parametrize(
    "rel",
    ["app/api/tasks.py", "scraper/worker.py"],
)
def test_stale_minutes_is_assigned_from_settings(rel: str):
    r"""★ 陈旧任务的分钟数，两边都必须**赋值自 `settings.stale_task_minutes`**。

    这一条针对本项目反复出现的第七类形状：**同一条规则写两遍，就会分叉。**

    曾经的样子：`app/api/tasks.py` 写 `_STALE_MINUTES = 10`，
    `scraper/worker.py` 写 `STALE_TASK_MINUTES = 10`，
    中间靠一句注释"两边保持一致"约束着。

    ★ 那处注释说不出口的一件事是：**它在被谁验证？答案是没人。**
      `tests/test_tasks.py` 用的是 `worker.STALE_TASK_MINUTES`，
      也就是只读了其中一份——接口那份改成 15，全部测试照样绿。

    ★★ 这条测试的**第一版是错的**，写在这里因为它是个真陷阱：

      第一版断言的是 `tasks_mod._STALE_MINUTES is worker_mod.STALE_TASK_MINUTES`
      ——"必须是同一个对象，而不是两个恰好相等的值"。想法是对的，
      但它**永远不会红**：把 `_STALE_MINUTES = 10` 改回字面量之后，
      两边都是整数 10，而 **CPython 会把 -5~256 的小整数驻留（intern）**，
      所以 `10 is 10` 是 `True`。变异测试当场证明了这一点：
      把字面量放回去，这条断言照样通过。

      → 结论：**在 Python 里用 `is` 去验证"值来自同一个地方"是行不通的**，
        因为小整数、短字符串、`True`/`False`/`None` 都可能被驻留或缓存。
        想知道"这个值是从哪来的"，就得去看**源码**，不能去看对象身份。

    ★ 所以改成 AST：右值必须是一个 `settings.<attr>` 的属性访问。
      这是可证伪的——改回 `= 10` 时右值是 `ast.Constant`，当场红。
    """
    path = REPO / rel
    found = list(_stale_assignment_source(path))
    assert len(found) == 1, (
        f"{rel} 里 STALE 常量的模块级赋值应该恰好一处，实际 {len(found)} 处"
    )
    name, value = found[0]
    assert isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name) \
        and value.value.id == "settings", (
        f"{rel} 的 {name} 不是从 settings 取的，右值是 {ast.dump(value)[:90]}。\n"
        "它必须写成 `settings.stale_task_minutes`，"
        "这样全项目只有 app/config.py 一处定义。"
    )
    assert value.attr == "stale_task_minutes", (
        f"{rel} 的 {name} 取自 settings.{value.attr}，应该是 settings.stale_task_minutes"
    )


def _module_level_stale_int_assignments(path: Path):
    r"""回抛 `(行号, 变量名, 值)`：模块级的、名字带 STALE 的、**值为整数字面量**的赋值。

    ★ 为什么必须用 AST 而不是逐行扫文本 —— 这条测试的第一版就是文本扫的，
      结果是**四个假阳性**，一个真的都没有：

        app/config.py:195   stale_task_minutes = int(os.getenv(..., "10"))
                            ← 这就是唯一合法的定义处，反而被抓了
        tests/test_layering.py:231-232
                            ← 我在**本文档字符串里**引用旧代码当例子，
                              被当成了真的赋值
        tests/test_tasks.py:241/762
                            ← `worker.STALE_TASK_MINUTES + 1`，
                              右边那个 `1` 被当成了"写死的分钟数"
        tests/test_tasks.py:275
                            ← `assert worker.recover_stale_tasks(db) == 0`，
                              函数名里有 stale、右边有个 0

      ★ 而这四个假阳性逼出了一个真问题：**我当时的第一反应是"给它开个例外名单"。**
        那份名单会长大，然后它自己就变成第七类形状的又一个实例——
        一条测试为了守住"规则只写一遍"，反而维护起一份自己的规则清单。
        所以改成了 AST：看得见"赋值"，看不见注释和算术表达式。

    ★ 为什么恰好能放过 config.py 那一条：
      那里写的是 `int(os.getenv(...))`，是个**调用**，不是 `ast.Constant`。
      于是"唯一的定义处"天然被排除，不需要写例外。
      这不是巧合——**唯一的来源本来就不该是一个字面量**，
      它应该是"从环境变量读，读不到给默认值"。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:                       # 只看模块级，函数内的局部变量不管
        targets = []
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if not isinstance(value, ast.Constant) or not isinstance(value.value, int):
            continue
        # bool 是 int 的子类，`X = True` 不该被当成分钟数
        if isinstance(value.value, bool):
            continue
        for t in targets:
            if isinstance(t, ast.Name) and "STALE" in t.id.upper():
                yield node.lineno, t.id, node.value


@pytest.mark.parametrize("module_path", ["app", "scraper", "tests"])
def test_no_module_defines_its_own_stale_minutes(module_path: str):
    r"""★ 更宽的一张网：任何模块都不许再写一份"死分钟数"。

    和上一条的分工：
      · 上一条是**精确**的（"这两个变量必须是同一个对象"）——
        它盯着已知的两个点，但如果有人新加**第三个**模块也定义一份，它看不见；
      · 这一条是**宽**的（扫全目录找字面量），补上那个缺口。

    两条都必要，因为它们的失败方式不同：一条会漏，一条会误报。
    宽的那条之所以敢宽，是因为用 AST 之后它**只看得见赋值语句**，
    注释、文档字符串、`X + 1` 这类表达式它一律不碰——假阳性面收得很小。
    """
    hits = [
        f"{p.relative_to(REPO).as_posix()}:{line}  {name} = {val}"
        for p in _py_files(module_path)
        for line, name, val in _module_level_stale_int_assignments(p)
    ]
    assert not hits, (
        "源码里又出现了写死的陈旧任务分钟数：\n  " + "\n  ".join(hits) +
        "\n改成从 settings.stale_task_minutes 取（app/config.py 是唯一定义处）。"
    )


# ===========================================================================
# 四、模块搬走了，名字也得跟着搬（logger 名）
# ===========================================================================


def _literal_logger_names(path: Path):
    r"""回抛 `(行号, 传给 getLogger 的字符串字面量)`。

    ★ 只认**字符串字面量**这一种形态：
        logging.getLogger("scraper.alerts")   ← 抓
        logging.getLogger(__name__)           ← 放过（它就是我们要的写法）
        logging.getLogger(f"app.{x}")         ← 放过（拼出来的，静态判不了对错）

      和上面那条"死分钟数"是同一个取舍：能静态证伪的才断言，
      判不了的宁可不做声，也不用试探性规则去制造假阳性。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "getLogger"):
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            yield node.lineno, arg.value


@pytest.mark.parametrize("module_path", ["app", "scraper"])
def test_logger_name_comes_from_dunder_name(module_path: str):
    r"""★ 每一处 `getLogger(...)` 都必须传 `__name__`，不许手打字符串。

    ==========================================================================
    这条用例守的是一个**真实发生过、而且全身而退**的错
    ==========================================================================
    `scraper/check_alerts.py` 在 2026-10-08 被搬到 `app/alerts_engine.py`，
    搬得很干净：测试全绿、import 方向修好、文档跟着更新。
    漏掉的是文件里的这一行：

        logger = logging.getLogger("scraper.alerts")     ← 留在了原地

    ★ 它为什么能全身而退：这三件事**一件都不会发生**——
        · 不报错
        · 不报警告
        · 不让任何一条测试变红（当时没有任何测试读 logger 名）

    ★ 它为什么还是得修：日志行会自称来自 `scraper.alerts`，
      而那段代码跑在 app/ 下。等到哪天要按 logger 名过滤——
      "今晚这次告警检查是谁触发的"——你会照着 `app.alerts_engine` 去配，
      然后**一条都收不到**。而那份配置本身完全正确，
      排查方向会被这条假线索带偏很久。

    ==========================================================================
    为什么断言的是"写法"而不是"名字对不对"
    ==========================================================================
    "名字对不对"要跟**模块的虚拟路径**比（`app/alerts_engine`），
    而那个路径得从文件相对位置现算——在包结构会变的情况下，
    算出来的可能和 import 时的真实 `__name__` 不一致，
    于是这条测试本身开始撒谎。

    → 换成断言写法：**必须是 `__name__`**。
      它把"名字对不对"从"需要一个正确的值"变成"物理上不可能错"：
      模块搬到哪，logger 名就跟到哪。这比任何一份看得见的清单都可靠。

    ★ 万一哪天真的需要一个**跨模块共用**的 logger 名
      （比如整个 scraper 都记在 "scraper" 名下），这条会拦住你——
      那就**改这条测试，并在改的时候写清楚为什么**。
      它拦的是"顺手打错"，不是"有意设计"。
    """
    hits = [
        f"{p.relative_to(REPO).as_posix()}:{line}  getLogger(\"{name}\")"
        for p in _py_files(module_path)
        for line, name in _literal_logger_names(p)
    ]
    assert not hits, (
        "这些地方把 logger 名写成了字符串字面量：\n  " + "\n  ".join(hits) +
        "\n\n改成 `logging.getLogger(__name__)`。"
        "\n手打的字符串在模块改名/搬目录时不会跟着变，"
        "\n而它错了不会有任何东西告诉你——本项目已经这么错过一次"
        "\n（scraper/check_alerts.py → app/alerts_engine.py）。"
    )
