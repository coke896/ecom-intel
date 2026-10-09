"""配置：路径从哪来、环境变量说了算不算。

★ 配置层的坏法很特殊：**它不报错**——程序正常，只是和你以为的不是一回事。实测
复现过两种：设了 `DATA_DIR` 后新目录被创建出来但**是空的**（库、图片、停用词表
仍指着 PROJECT_ROOT/data）；`DATABASE_URL` 的相对路径相对**启动目录**，换个目录
启动且那里恰好有 `data/` 时 SQLite 会**静默建出一个全新的空库**（表全在、商品 0
条）。`.env.example` 里没注释掉的 DATABASE_URL 会盖住 `DATA_DIR` 派生的默认值——
那条用例测的不是代码，是交付物。
★ 必须开 subprocess 测：`DATA_DIR` 在**模块导入时**读，进程内改环境变量再 import
没用（模块已进 `sys.modules`）；monkeypatch 假 settings 测的是替身。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT, _resolve_database_url, settings


# 一、默认值：不设任何环境变量时，路径必须还是老样子


def test_default_paths_all_live_under_data_dir() -> None:
    """★ 把"DATA_DIR 只管自己"的病根钉住：四个路径必须是同一个根的孩子——断言的
    是**关系**而不是四个写死的字符串（写死的话换个默认值要改四处，漏改的那处没人发现）。"""
    assert settings.data_dir == PROJECT_ROOT / "data"
    assert settings.images_dir.parent == settings.data_dir
    assert settings.stopwords_file.parent == settings.data_dir
    assert settings.userdict_file.parent == settings.data_dir


def test_default_database_is_the_project_one() -> None:
    """默认库是项目目录下的 data/ecom.db，且是绝对路径——相对路径会跟着启动目录走
    （见模块顶部）；有人改成相对写法这里就红，而不是部署上莫名多出一个空库。"""
    assert settings.database_url == f"sqlite:///{(PROJECT_ROOT / 'data' / 'ecom.db').as_posix()}"


# 二、DATA_DIR：设了它，数据得真的跟着走


def test_data_dir_env_moves_everything(tmp_path: Path) -> None:
    """★ 给子进程设 `DATA_DIR`，四个派生路径必须**全都**跟着变。
    ★ 不能省掉 subprocess：测的就是"换一份环境启动"，进程内改不出来。"""
    fake = tmp_path / "elsewhere"
    code = (
        "import json;"
        "from app.config import settings;"
        "print(json.dumps({"
        "'data_dir': str(settings.data_dir),"
        "'images': str(settings.images_dir),"
        "'stopwords': str(settings.stopwords_file),"
        "'userdict': str(settings.userdict_file),"
        "'db': settings.database_url,"
        "}))"
    )
    out = _run_with_env(code, {"DATA_DIR": str(fake)})

    # ★ 先断言"环境变量确实传进去了"，再断言派生结果——反过来的话，
    #   一个"环境变量没传到"的夹具会让失败信息全是默认值，看不懂。
    assert out["data_dir"] == str(fake), "子进程没拿到 DATA_DIR，这条用例没测到东西"

    assert Path(out["images"]) == fake / "images"
    assert Path(out["stopwords"]) == fake / "stopwords.txt"
    assert Path(out["userdict"]) == fake / "userdict.txt"
    assert out["db"] == f"sqlite:///{(fake / 'ecom.db').as_posix()}"

    # ★ 一条反向断言：**不能**还有东西留在老地方——只断言"新的对了"漏得掉
    #   "旧的也还在"，那正是本文件要防的形态。
    assert str(PROJECT_ROOT / "data") not in "".join(out.values())


def test_a_single_path_can_still_be_overridden_on_its_own(tmp_path: Path) -> None:
    """单项覆盖仍然独立：设了 `IMAGES_DIR`，不该被 `DATA_DIR` 的派生吃掉。
    ★ 守修复的**边界**：写死成"必须是 DATA_DIR 的子目录"的话，"只把图片放别的
      盘"就安静地不生效了（且不报错）。"""
    pics = tmp_path / "pics"
    out = _run_with_env(
        "import json;from app.config import settings;"
        "print(json.dumps({'images': str(settings.images_dir),"
        "'data_dir': str(settings.data_dir)}))",
        {"IMAGES_DIR": str(pics)},
    )
    assert Path(out["images"]) == pics
    assert Path(out["data_dir"]) == PROJECT_ROOT / "data"


# 二之补：.env.example 是"模板"，不许悄悄关掉上面那条链


def test_env_example_does_not_pin_the_derived_paths() -> None:
    """★ `.env.example` 里不许出现**没注释**的路径覆盖项——测的不是代码，是**交付物**。
    ★ 实测（2026-09-29）：照 README `cp .env.example .env` 之后上面那条 DATA_DIR
      用例**立刻变红**；模板里 `DATABASE_URL=sqlite:///data/ecom.db` 的值恰好等于
      默认值，平时看不出差别，只有把 DATA_DIR 指到别处才暴露"图片、停用词表都搬
      走了，只有库没搬"，而它看起来像成功了。允许注释掉，不允许取消注释——哪怕
      填的是"默认值"。"""
    pins = ("DATABASE_URL", "IMAGES_DIR", "STOPWORDS_FILE", "USERDICT_FILE")
    active: list[str] = []
    for raw in (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = line.partition("=")[0].strip()
        if name in pins:
            active.append(line)
    assert not active, (
        ".env.example 里有没注释掉的路径覆盖项，"
        "它们会盖掉由 DATA_DIR 派生的默认值：\n  " + "\n  ".join(active) +
        "\n改成注释掉的示例（前面加 #），并说明它何时才该被启用。"
    )


# 三、DATABASE_URL：相对路径必须被解析掉


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 相对路径 → 按项目根解析成绝对
        ("sqlite:///data/ecom.db", f"sqlite:///{(PROJECT_ROOT / 'data' / 'ecom.db').as_posix()}"),
        ("sqlite:///x.db", f"sqlite:///{(PROJECT_ROOT / 'x.db').as_posix()}"),
        # 已经在项目根之外的相对路径，`..` 要正常生效
        ("sqlite:///../other/x.db", f"sqlite:///{(PROJECT_ROOT.parent / 'other' / 'x.db').as_posix()}"),
    ],
)
def test_relative_sqlite_paths_are_resolved_against_the_project(raw: str, expected: str) -> None:
    assert _resolve_database_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        # 已经绝对：四斜杠是 SQLAlchemy 的绝对写法，一个字符都不该动
        "sqlite:////tmp/x.db",
        # 内存库：没有路径可解析
        "sqlite:///:memory:",
        "sqlite://",
        # 非 sqlite：有它自己的解析规则，不该被 sqlite 的逻辑碰
        "postgresql://user:pw@localhost:5432/db",
    ],
)
def test_other_urls_are_left_alone(raw: str) -> None:
    """★ 这个修复最容易伤到的地方就是"不该动的动了"。尤其 `sqlite:///:memory:`
    ——测试夹具用它；被拼成 `sqlite:///E:/VScodde/ecom-intel/:memory:` 的话会
    **往磁盘上写一个名叫 :memory: 的文件**，而且测试仍然是绿的（确实能读写）。"""
    assert _resolve_database_url(raw) == raw


def test_a_relative_env_url_does_not_create_a_second_database(
    tmp_path: Path,
) -> None:
    """★ 端到端复现"相对路径跟着启动目录走"：从别的目录启动，engine 必须仍指向
    项目自己的库。
    ★ 夹具是**最坏情形**：临时目录下故意先建 `data/`——没有它 SQLite 会因父目录
      不存在直接报错（看得见的失败），有它才会静默建库。
    ★ 只 import engine、**不调 `init_database()`**：调了会真的连库并跑建表/迁移，
      而解析后的 URL 指向**项目真实的 data/ecom.db**——那违反"测试绝不能碰
      data/ecom.db"（docs 14.6）。create_engine 是惰性的，读 `engine.url` 不会
      打开文件，而"URL 解析成什么"正是要验的全部。"""
    workdir = tmp_path / "somewhere-else"
    (workdir / "data").mkdir(parents=True)  # ← 没有这一行，测的就是另一种失败了

    code = (
        "from app.db.base import engine;"
        "print(engine.url.database)"
    )
    out = _run_with_env(
        code, {"DATABASE_URL": "sqlite:///data/ecom.db"}, cwd=workdir, raw=True
    )
    used = out.strip().splitlines()[-1]

    assert Path(used) == (PROJECT_ROOT / "data" / "ecom.db"), (
        f"它指向了 {used}，而不是项目自己的库"
    )
    # ★ 兜底：那个目录下面不许出现 .db 文件——万一有人在这里加了
    #   "顺手连一下"的代码，这条会先响。
    strays = list(workdir.rglob("*.db"))
    assert not strays, f"在那个目录下冒出了库文件：{strays}"


# 小工具


def _run_with_env(
    code: str,
    extra_env: dict[str, str],
    *,
    cwd: Path | None = None,
    raw: bool = False,
):
    """在**干净的环境**里跑一段代码，返回它的输出。
    ★ `env=` 从 `os.environ` 复制而不是从零构造：子进程需要 PATH 才能启动 python；
      在副本上改所以**不会污染父进程**。`PYTHONPATH` 指向项目根，让子进程能
      `import app`——它可能是在 `tmp_path` 里启动的（那正是要复现的场景）。"""
    import json
    import os

    env = dict(os.environ)
    env.pop("DATA_DIR", None)  # 防止跑测试的人自己的环境变量串进来
    env.pop("DATABASE_URL", None)
    env.update(extra_env)
    env["PYTHONPATH"] = str(PROJECT_ROOT)

    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=str(cwd or PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, f"子进程挂了：\n{proc.stdout}\n{proc.stderr}"
    if raw:
        return proc.stdout
    return json.loads(proc.stdout.strip().splitlines()[-1])
