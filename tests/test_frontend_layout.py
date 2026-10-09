"""前端布局的静态不变量。

背景（它为什么存在）：`table.data th` 曾写 `top: 58px`，但表包在
`overflow-x:auto` 的 div 里——按 CSS 规范一个轴不是 visible，另一个就会被
计算成 auto，sticky 于是相对该容器定位，表头被推开 58px 压住数据行。
它不报错、不影响任何测试，只能靠人眼发现。

这些测试只钉住"约束还在"；解析 CSS 前必须先去注释（注释里写着 `top: 58px`）。
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
CSS = FRONTEND / "static" / "css" / "style.css"

_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)
# 只匹配没有嵌套的简单规则块（本项目的 CSS 没有 @media 嵌套规则块）
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_TABLE = re.compile(r'<table\s+class="data"')


def _declarations(selector: str) -> dict[str, str]:
    """取出选择器列表里**恰好等于** selector 的那条规则，解析成 dict（必须去注释后再解析，见文件头）。"""
    css = _CSS_COMMENT.sub("", CSS.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for raw_sel, raw_body in _RULE.findall(css):
        parts = [p.strip() for p in raw_sel.split(",")]
        if selector not in parts:
            continue
        for decl in raw_body.split(";"):
            if ":" not in decl:
                continue
            prop, _, value = decl.partition(":")
            found[prop.strip().lower()] = value.strip()
    return found


# 守门：先确认解析器真的读到了东西
def test_css_parser_actually_finds_the_rule():
    """★ 守的是**解析器本身**：选择器或去注释写错会让 `_declarations` 返回空 dict，
    一个失效的检查比没有检查更糟。"""
    decls = _declarations("table.data th")
    assert decls, "没解析到 `table.data th` 规则 —— 是选择器变了，还是去注释写错了？"
    assert decls.get("position") == "sticky", (
        f"`table.data th` 不再是 sticky 了（现在是 {decls.get('position')!r}）。"
        "如果不是有意的，说明这条规则被改坏了；如果是有意的，"
        "请一并删掉 .table-scroll 那段注释和本文件。"
    )


# 真正的不变量
def test_sticky_table_header_uses_zero_offset():
    """粘住的表头，top 必须是 0——任何非 0 的 top 都会把它往下推、压住数据行。
    这条不能证明视觉上没问题，只钉住"别再写回顶栏高度那个数"。"""
    decls = _declarations("table.data th")
    top = decls.get("top")
    assert top == "0", (
        f"`table.data th` 的 top 是 {top!r}，应该是 '0'。\n"
        "如果写的是顶栏高度（58px 之类）：那正是 2026-09 那次人眼确认"
        "抓到的 bug —— 表头会被顶下去压住数据行。"
    )


def test_every_data_table_sits_in_a_scroll_container():
    """每张 `table.data` 都要包在 `.table-scroll` 里——裸的 overflow div 不会让表头粘住
    （滚动容器从不纵向滚动），CSS 里那句 sticky 就成了不干活的承诺。
    页面上没有表格的文件直接跳过，不是失败。"""
    pages_with_tables = 0
    for page in sorted(FRONTEND.glob("*.html")):
        html = page.read_text(encoding="utf-8")
        matches = list(_TABLE.finditer(html))
        if not matches:
            continue
        pages_with_tables += 1
        for m in matches:
            before = html[: m.start()]
            i = before.rfind("<div")
            assert i != -1, f"{page.name}：`table.data` 前面没有 div 包装"
            tag = before[i : before.find(">", i) + 1]
            assert "table-scroll" in tag, (
                f"{page.name} 里有一张 table.data 包在了 {tag!r} 里，"
                "应该是 <div class=\"table-scroll\">。\n"
                "裸的 overflow 容器不会让表头粘住（它从不纵向滚动），"
                "CSS 里那句 position: sticky 就白写了。"
            )

    assert pages_with_tables > 0, "一个带表格的页面都没扫到 —— 是路径变了还是正则写错了？"
