"""前端脚本的语法检查：4 个页面里的内联脚本 + 2 个共用 js。

前端是唯一没有自动化检查的部分（零构建，见 style.css 顶部）：内联脚本里
一个多余的括号、一个全角引号，浏览器什么都不说，只白屏或按钮没反应，
所以把"浏览器会静默吞掉的东西变成测试失败"——只查语法，不查行为。
提取前先去掉 HTML 注释：注释正文里的 `<script>` 曾被一直匹配到下一个
`</script>`，把中文散文当 JS 送检、报出看起来像真的假错误（踩过两次）。
没装 Node 时整份跳过：项目运行不需要 Node，不能让 clone 下来能跑变红。
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
    """把源码写进临时文件交给 `node --check`，失败信息里带上**哪个页面的第几个脚本块**。
    必须落盘再查：`node --check` 只接受文件路径，不支持从 stdin 读。"""
    target = tmp_path / "snippet.js"
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        ["node", "--check", str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, f"{username} 语法错误：\n{result.stderr}"


@pytest.fixture(scope="module", autouse=True)
def require_node():
    """没有 node 就整份跳过——语法检查是加分项，而"clone 下来能跑"是硬承诺，冲突时后者优先。"""
    if shutil.which("node") is None:
        pytest.skip("本机没有 node，跳过前端语法检查（不影响项目运行）")


def test_pages_have_inline_scripts_to_check():
    """★ 守的是**检查器本身**：正则写错会让参数化测试收集到 0 个用例，pytest 显示"通过"。"""
    for page in _pages():
        assert _inline_scripts(page.read_text(encoding="utf-8")), f"{page.name} 没提取到内联脚本"


def test_html_comments_are_stripped_before_extracting():
    """★ 守那个踩过两次的假错误：注释里出现 `<script>` 是正常的，不先去注释会把中文散文当脚本。"""
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
