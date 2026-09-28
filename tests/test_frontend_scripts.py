"""前端脚本的语法检查：4 个页面里的内联脚本 + 2 个共用 js。

================================================================================
为什么前端也需要一个"测试"
================================================================================
前端是这个项目里**唯一没有自动化检查**的部分：纯 HTML + 内联脚本，
没有构建步骤（这是有意的，见 style.css 顶部），所以也就没有
编译器/打包器在每次改动时替我们看一遍。

代价是：内联脚本里一个多余的括号、一个中文全角引号、
一个 `await` 写在了非 async 函数里 —— 浏览器**什么都不说**，
页面只是白屏或者按钮没反应。而这一类错误在 Node 里是一条命令就能查出来的。

所以这里做的是"把浏览器会静默吞掉的东西变成测试失败"。
它检查的是**语法**，不是行为 —— 行为那一层只能靠人去点，
这一点在 docs/可行性评估.md 里如实记着（本机没装 chromium，
所以没有浏览器端的自动化验证）。

================================================================================
★ 提取前必须先去掉 HTML 注释（踩过两次的坑）
================================================================================
`<script>` 这个词会出现在**注释的正文里**。比如 index.html 顶部那段
解释"为什么兜底要用 document.write 而不是 appendChild"的注释里，
正文有一句"动态插入的 `<script>` 默认是异步的"。

只写正则的话，会从注释里那个 `<script>` 一直匹配到下一个 `</script>`，
把一小段中文散文当成 JS 送去检查 —— 然后报一个
"SyntaxError: Invalid or unexpected token"，位置指向一句中文。

★ 这个假错误**踩过两次**（第一次是手工脚本，第二次是把它搬进测试时）。
  它难对付的地方不是"不好修"，是**它看起来像真的**：
  报错里有一个合法的文件名和一个行号，于是你会去那个文件的第 1 行找问题。
  去注释就一劳永逸了。

================================================================================
★ 没装 Node 时跳过，不失败
================================================================================
本项目跑起来**不需要 Node**（前端零构建），所以不能因为
"这台机器没有 node"就让别人 clone 下来的 `pytest` 变红。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

# 没有 src 属性的 <script>，内容捕获到第 1 组
_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


def _inline_scripts(html: str) -> list[str]:
    """页面里的内联脚本，按出现顺序。空块（只有空白）丢掉。"""
    # ★ 顺序要紧：先去注释，再找 script。
    without_comments = _HTML_COMMENT.sub("", html)
    return [b for b in _INLINE_SCRIPT.findall(without_comments) if b.strip()]


def _pages() -> list[Path]:
    return sorted(FRONTEND.glob("*.html"))


def _shared_js() -> list[Path]:
    return sorted((FRONTEND / "static" / "js").glob("*.js"))


def _node_check(username: str, source: str, tmp_path: Path) -> None:
    """把源码写进临时文件，交给 `node --check`。语法有问题就断言失败。

    写成函数而不是每处重复调用，是为了让失败信息里带上
    **哪个页面的第几个脚本块** —— 否则给到的只有临时文件名。

    ★ 必须落盘再查：`node --check` 只接受文件路径，
      不支持从 stdin 读（`node --check -` 会报错）。
    """
    target = tmp_path / "snippet.js"
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        ["node", "--check", str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, f"{username} 语法错误：\n{result.stderr}"


@pytest.fixture(scope="module", autouse=True)
def require_node():
    """没有 node 就整份跳过。

    ★ 用 skip 而不是 fail：语法检查是**加分项**，
      而"clone 下来能跑"是这个项目的硬承诺（见 app/config.py 顶部）。
      两者冲突时后者优先。
    """
    if shutil.which("node") is None:
        pytest.skip("本机没有 node，跳过前端语法检查（不影响项目运行）")


def test_pages_have_inline_scripts_to_check():
    """★ 守的是**检查器本身**。

    正则写错的话，`_inline_scripts` 会返回空列表，
    下面的参数化测试于是收集到 0 个用例 —— pytest 不报错，
    显示的是"通过"。一个静默失效的检查比没有检查更糟。
    """
    for page in _pages():
        assert _inline_scripts(page.read_text(encoding="utf-8")), f"{page.name} 没提取到内联脚本"


def test_html_comments_are_stripped_before_extracting():
    """★ 这条守的是那个踩过两次的假错误。

    注释里出现 `<script>` 是**正常**的（这一页的注释就在解释它），
    不先去注释的话，提取出来的"脚本"是一段中文散文。"""
    html = (
        "<!-- 解释：动态插入的 <script> 是异步的 -->\n"
        "<script>var ok = 1;</script>"
    )
    scripts = _inline_scripts(html)
    assert scripts == ["var ok = 1;"]


def test_shared_js_syntax(tmp_path):
    js_files = _shared_js()
    assert js_files, "frontend/static/js/ 下一个 js 都没有？"
    for js in js_files:
        _node_check(js.name, js.read_text(encoding="utf-8"), tmp_path)


def test_page_script_syntax(tmp_path):
    for page in _pages():
        html = page.read_text(encoding="utf-8")
        for index, script in enumerate(_inline_scripts(html)):
            _node_check(f"{page.name} 的内联脚本 #{index}", script, tmp_path)
