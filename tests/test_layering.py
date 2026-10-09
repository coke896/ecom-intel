r"""分层方向：把"注释里的规矩"变成"跑测试时会红的东西"。

规矩一条，没有例外：**scraper/ 可以 import app/；app/ 绝不 import scraper/**。
`scraper/` 是部署单元（另一个进程），`app/` 是被依赖的领域层——写下来的规矩没有执行者，
唯一的守卫是"下次有人刚好看见"，所以用 AST 解析源码（不 import 被测模块——
要检查的正是"某个 import 存不存在"）。

反向的例外修过两处（2026-10-08），都是"挪模块"解决的：`app/api/alerts.py` → `scraper.check_alerts`
（已挪到 app/alerts_engine.py）、`app/analysis/{payload,reporter}.py` → `app.api.deps`
（SourceFilter / apply_source 已挪到 app/source_scope.py）——后者是**跨层**，第三条用例钉住。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.config import settings

REPO = Path(__file__).resolve().parent.parent


# 小工具：把一棵目录下的所有 .py 解析成 AST，回抛 (文件, import 的模块名) 对


def _imported_modules(path: Path):
    r"""回抛 `(文件, 被 import 的模块名字符串)`。

    ★ 用 `ast.walk` 而不是正则：正则看不出 `import a.b` 和字符串里的 `"import a.b"` 的区别，
    而本项目大量注释/文档把 import 语句原样当例子引用（alerts_engine.py 的 docstring 里就有），
    正则会把它们全部误报。两种形式都覆盖：`import a.b.c` → `a.b.c`；
    `from a.b import c` → `a.b`；`level > 0` 的相对 import 跳过。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield path, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对 import，跳过
                continue
            if node.module:
                yield path, node.module


def _py_files(subdir: str):
    return sorted((REPO / subdir).rglob("*.py"))


# 一、规矩的主体：app/ 绝不 import scraper/


def test_app_never_imports_scraper():
    r"""★ 本文件存在的全部理由。

    失败时的报错要直接告诉人**怎么办**——读者多半是"刚写了个很自然的 import、
    被这里拦住"的人，而不是"正在重构分层的人"，所以下面把两条出路都写出来。
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

    ★ 检查的是 app/api/images.py 里那段注释还在不在——测的其实是**它有没有被静默删掉**：
    注释含一个 3 行的 User-Agent 字符串副本，容易被当"重复代码"清掉；
    一旦清掉，`from scraper... import HEADERS` 看起来就完全合理了。
    """
    text = (REPO / "app" / "api" / "images.py").read_text(encoding="utf-8")
    assert "Web 层依赖采集层" in text, (
        "app/api/images.py 里那段解释'为什么 User-Agent 要重写一遍而不敢 import'"
        "的注释不见了。它是这条规矩的说明书，删了之后下面这条测试就变成了"
        "一条没有理由的约束。"
    )


# 二、同一件事，换个方向：分析层不 import Web 层


def test_analysis_layer_does_not_import_the_web_layer():
    r"""★ 分析层（`app/analysis/`）不许 import `app/api/`。

    比上一条更容易被无意破坏（同一个包内部，看着只是"两个模块互相帮忙"）。
    实际发生过：payload.py 写了 `from app.api.deps import SourceFilter, apply_source`。
    那个 import 显得自然是因为**模块放错了地方**——SourceFilter 是纯枚举、
    apply_source 是纯 SQLAlchemy 函数，都不知道 HTTP 是什么。修法是挪模块（app/source_scope.py），不是加例外。
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

    上一条只查"分析层不 import app.api"——把 SourceFilter 挪回 app/api/deps.py、
    再让 payload 改从别处拿，上一条照样绿。这条直接钉住结果：两个符号必须住在
    app/ 根下（app/source_scope.py），而且**只能有一份定义**。
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


# 三、同一条规则只许写一遍（本项目第七类形状的守门）


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

    第七类形状（同一条规则写两遍就会分叉）：tasks.py 曾写 `_STALE_MINUTES = 10`、
    worker.py 曾写 `STALE_TASK_MINUTES = 10`，靠一句"两边保持一致"的注释约束——
    而那句注释**没有任何执行者**（test_tasks.py 只读其中一份，接口那份改成 15 照样全绿）。

    ★ 第一版断言 `a is b` 永远不会红：CPython 驻留 -5~256 的小整数，`10 is 10` 是 True
    （变异测试当场证明）——用 `is` 验证"值来自同一个地方"在 Python 里行不通。
    → 改成 AST：右值必须是 `settings.<attr>` 属性访问，改回 `= 10` 时右值是 ast.Constant，当场红。
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

    ★ 第一版逐行扫文本，四个假阳性、一个真的都没有：config.py 的 `int(os.getenv(...))`
    （唯一合法的定义处反而被抓）、本文档里引用的旧代码、test_tasks.py 的
    `STALE_TASK_MINUTES + 1` 和 `recover_stale_tasks(db) == 0`。第一反应"开个例外名单"会长大、
    自己变成第七类形状的又一实例——所以改成 AST。恰好放过 config.py 那条不是巧合：
    唯一的来源本来就不该是字面量（它是调用，不是 ast.Constant），而该是"读环境变量、读不到给默认值"。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:  # 只看模块级，函数内的局部变量不管
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

    与上一条分工：上一条**精确**（盯已知的两个点，新加第三个模块定义一份它看不见）；
    这条**宽**（扫全目录找字面量，补上缺口）。两条都必要——失败方式不同，一条会漏、一条会误报。
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


# 四、模块搬走了，名字也得跟着搬（logger 名）


def _literal_logger_names(path: Path):
    r"""回抛 `(行号, 传给 getLogger 的字符串字面量)`。

    ★ 只认字符串字面量：`getLogger("scraper.alerts")` 抓；`getLogger(__name__)` 放过
    （那就是要的写法）；f-string 放过（拼出来的，静态判不了对错）。
    和"死分钟数"同一个取舍：能静态证伪的才断言，不用试探性规则制造假阳性。
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

    真实发生过而且全身而退的错：check_alerts.py 搬进 app/alerts_engine.py 时，
    `logging.getLogger("scraper.alerts")` 留在了原地——不报错、不报警告、不让任何测试变红。
    但日志行自称来自 scraper.alerts 而代码跑在 app/ 下：按 logger 名过滤"今晚的告警检查"
    会照着 app.alerts_engine 配、然后**一条都收不到**，而配置本身完全正确，排查方向被带偏很久。

    断言"写法"而不是"名字对不对"（正确名字要从模块相对位置现算，包结构一变可能算错、
    测试自己就开始撒谎）；必须是 `__name__` 则物理上不可能错。真需要跨模块共用的 logger 名
    （整个 scraper 记在 "scraper" 名下）就**改这条测试并写清楚为什么**——它拦"顺手打错"，不拦"有意设计"。
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
