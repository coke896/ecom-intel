"""配置：路径从哪来、环境变量说了算不算。

================================================================================
这份测试守的是什么
================================================================================
配置层的坏法很特殊：**它不报错**。

代码错了会抛异常，数据错了会对不上，而配置错了的表现是
"程序一切正常，只是和你以为的不是一回事"——它悄悄用了另一个路径、
另一个库、另一份停用词表。这个项目已经反复记录过这个形状的问题
（见 docs 16.10："库改了，读的人不知道"），配置层是它的又一个落点。

具体两条，都是**实测复现过**的：

【1】`DATA_DIR` 只管自己，不管数据
    设 `DATA_DIR=/tmp/x` 之后：/tmp/x 被创建出来了，**但它是空的**。
    库、图片、停用词表、自定义词典全都还指着 `PROJECT_ROOT/data`。
    看到目录出现，人就会以为搬成功了。这比"没这个配置项"更坏——
    没有的配置项会报错，而这个只会让人相信一件假事。

【2】`DATABASE_URL` 的相对路径是相对于**启动目录**的
    `sqlite:///data/ecom.db` 在项目根启动时正常；换一个目录启动，
    如果那个目录下恰好也有 `data/`，SQLite 会**静默建出一个全新的空库**
    ——全集表都在、商品 0 条、不报错。服务照常起来，界面上什么都没有，
    而人只会以为"还没采集"。

【3】`.env.example` 把【1】放了回来（2026-09-29 实测）
    模板里有一行**没注释掉**的 `DATABASE_URL=sqlite:///data/ecom.db`。
    它盖住了由 `DATA_DIR` 派生的默认值，于是上面那条
    `test_data_dir_env_moves_everything` 在 `cp .env.example .env` 之后立刻变红。

    ★ 关键在于：**它在此之前一直是绿的**——绿的原因不是代码对，
      是没人真的照 README 做过第一步。而 README 让你做的正是那一步。

    ★ 也在于：模板里那个值**恰好等于默认值**，所以平时完全看不出差别，
      只有 `DATA_DIR` 一动才暴露"库没跟着搬"。
      这是【1】的同一个形状换了个门进来：代码修好了，模板又放回去。
      所以补了一条直接测 `.env.example` 的用例 —— 它测的不是代码，是交付物。

================================================================================
★ 为什么这里有一条 subprocess 测试
================================================================================
`DATA_DIR` 是在**模块导入时**读的（类属性在类体执行时求值）。
同一个进程里改环境变量再 import 是没用的——模块已经进 `sys.modules` 了，
拿到的是第一次那份。而"换个环境变量启动会怎样"恰恰是要测的东西本身。

所以只有开个子进程、给它一份干净的环境，才真的测到了这件事。
在进程内 monkeypatch 一个假的 settings 再断言，测的是**测试自己写的替身**，
不是真实行为——那种测试会在实现坏掉时照样变绿。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT, _resolve_database_url, settings


# ===========================================================================
# 一、默认值：不设任何环境变量时，路径必须还是老样子
# ===========================================================================


def test_default_paths_all_live_under_data_dir() -> None:
    """★ 这一条把【1】的病根钉住：四个路径必须是同一个根的孩子。

    ★ 断言的是**关系**（都在 data_dir 下面），不是四个写死的字符串。
      写死字符串的话，把 data_dir 换个默认值就要改四处——
      而漏改的那一处正是"某个文件跑到别处去了"，没人会发现。
    """
    assert settings.data_dir == PROJECT_ROOT / "data"
    assert settings.images_dir.parent == settings.data_dir
    assert settings.stopwords_file.parent == settings.data_dir
    assert settings.userdict_file.parent == settings.data_dir


def test_default_database_is_the_project_one() -> None:
    """默认库就是项目目录下的 data/ecom.db，且是绝对路径。

    ★ 绝对路径这一点是**不能松**的：相对路径会跟着启动目录走（见【2】）。
      这一条断言等于给默认值上了一道锁——哪天有人把它改成相对写法，
      这里会红，而不是等到某个部署上莫名多出一个空库。
    """
    assert settings.database_url == f"sqlite:///{(PROJECT_ROOT / 'data' / 'ecom.db').as_posix()}"


# ===========================================================================
# 二、DATA_DIR：设了它，数据得真的跟着走
# ===========================================================================


def test_data_dir_env_moves_everything(tmp_path: Path) -> None:
    """★ 给一个子进程设 `DATA_DIR`，四个派生路径必须**全都**跟着变。

    ★ 为什么不能省掉 subprocess：见模块顶部。这里测的就是
      "换一份环境启动"，进程内改不出来。
    """
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

    # ★ 先断言"环境变量确实传进去了"，再断言派生结果。
    #   反过来的话，一个"环境变量没传到"的夹具会让这条用例以一个
    #   看不懂的方式失败（全是默认值），而不是直接说"环境没设上"。
    assert out["data_dir"] == str(fake), "子进程没拿到 DATA_DIR，这条用例没测到东西"

    assert Path(out["images"]) == fake / "images"
    assert Path(out["stopwords"]) == fake / "stopwords.txt"
    assert Path(out["userdict"]) == fake / "userdict.txt"
    assert out["db"] == f"sqlite:///{(fake / 'ecom.db').as_posix()}"

    # ★ 一条反向断言：**不能**还有东西留在老地方。
    #   只断言"新的对了"漏得掉"旧的也还在"——那正是【1】的形态：
    #   新目录建出来了，而数据还在老地方。
    assert str(PROJECT_ROOT / "data") not in "".join(out.values())


def test_a_single_path_can_still_be_overridden_on_its_own(tmp_path: Path) -> None:
    """单项覆盖仍然是独立的：设了 `IMAGES_DIR`，不该被 `DATA_DIR` 的派生吃掉。

    ★ 这条守的是修复的**边界**。把四个路径都改成从 `DATA_DIR` 派生之后，
      很容易顺手写死成"必须是 DATA_DIR 的子目录"——那样"只把图片放
      别的盘"这个合理需求就没了（而且不会报错，只会安静地不生效）。
    """
    pics = tmp_path / "pics"
    out = _run_with_env(
        "import json;from app.config import settings;"
        "print(json.dumps({'images': str(settings.images_dir),"
        "'data_dir': str(settings.data_dir)}))",
        {"IMAGES_DIR": str(pics)},
    )
    assert Path(out["images"]) == pics
    assert Path(out["data_dir"]) == PROJECT_ROOT / "data"


# ===========================================================================
# 二之补：.env.example 是"模板"，不许悄悄关掉上面那条链
# ===========================================================================


def test_env_example_does_not_pin_the_derived_paths() -> None:
    """★ `.env.example` 里不许出现**没注释**的路径覆盖项。

    这是 2026-09-29 实测逼出来的一条（【1】的第三次复现）：

      照 README 执行 `cp .env.example .env` 之后，
      上面那条 `test_data_dir_env_moves_everything` **立刻变红** ——
      而在此之前它一直是绿的。绿的原因不是代码对，
      是**没人真的照 README 做过那一步**。

    模板里那行 `DATABASE_URL=sqlite:///data/ecom.db` 的值恰好等于默认值，
    所以平时看不出任何差别；只有把 `DATA_DIR` 指到别处时才暴露：
    图片、停用词表、自定义词典都搬走了，**只有库没搬**。
    而它看起来像成功了——新目录确实建出来了，只是里面少一个库文件。

    ★ 所以这条不是在测代码，是在测**交付物**：
      "README 让你做的第一件事，不能把 README 承诺的另一件事弄坏。"

    ★ 允许的写法是把它注释掉（可选项照旧保留在那儿），
      不允许的写法是取消注释 —— 哪怕填的是"默认值"。
    """
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


# ===========================================================================
# 三、DATABASE_URL：相对路径必须被解析掉
# ===========================================================================


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
    """★ 这个修复最容易伤到的地方就是"不该动的动了"。

    尤其 `sqlite:///:memory:`——测试夹具用它。要是被拼成
    `sqlite:///E:/VScodde/ecom-intel/:memory:`，那测试就会**往磁盘上
    写一个名叫 :memory: 的文件**，而且测试仍然是绿的（它确实能读写）。
    """
    assert _resolve_database_url(raw) == raw


def test_a_relative_env_url_does_not_create_a_second_database(
    tmp_path: Path,
) -> None:
    """★ 端到端复现【2】：从别的目录启动，engine 必须仍然指向项目自己的库。

    ★ 这个夹具是**最坏情形**：临时目录下故意先建一个 `data/` 子目录。
      没有这个子目录的话，SQLite 会因为父目录不存在而直接报错
      （"unable to open database file"）——那是会被人看见的失败。
      而**有**这个子目录时它静默建库，才是真正要防的那一种。

    ★ 只 import engine、**不调 `init_database()`**，这是刻意的：
      调了就会真的连库并跑一遍建表/迁移，而这个子进程的 cwd 虽然
      在临时目录，解析后的 URL 却指向**项目真实的 `data/ecom.db`**。
      那正是本项目的规矩"测试绝不能碰 data/ecom.db"（docs 14.6）要挡的事。
      `create_engine` 是惰性的，只读 `engine.url` 不会打开任何文件——
      而"URL 解析成什么"恰好就是这个修复要验的全部内容。
    """
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
    # ★ 兜底：那个目录下面不许出现 .db 文件。
    #   万一哪天有人在这里加了"顺手连一下"的代码，这条会先响。
    strays = list(workdir.rglob("*.db"))
    assert not strays, f"在那个目录下冒出了库文件：{strays}"


# ===========================================================================
# 小工具
# ===========================================================================


def _run_with_env(
    code: str,
    extra_env: dict[str, str],
    *,
    cwd: Path | None = None,
    raw: bool = False,
):
    """在**干净的环境**里跑一段代码，返回它的输出。

    ★ `env=` 从 `os.environ` 复制而不是从零构造：
      子进程仍然需要 PATH 之类的东西才能启动 python 自己。
      但我们在副本上改，所以**不会污染父进程**（测试之间互不影响）。

    ★ `PYTHONPATH` 指向项目根，让子进程能 `import app`——
      它可能是在 `tmp_path` 里启动的（那正是【2】要复现的场景）。
    """
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
