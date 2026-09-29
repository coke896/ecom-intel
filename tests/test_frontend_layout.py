"""前端布局的静态不变量。

================================================================================
这个文件是"人眼确认"那一轮的唯一产物
================================================================================
前端没有构建步骤（有意的，见 style.css 顶部），所以也没有任何工具会在
改动 CSS 时替我们看一眼。于是就有了下面这件事：

  `table.data th` 上写着 `position: sticky; top: 58px`，
  本意是"页面滚动时表头别被 58px 高的顶栏盖住"。
  而这三张表都被包在 `<div style="overflow-x:auto">` 里 ——
  **CSS 规范规定：overflow-x/overflow-y 里只要有一个不是 visible，
  另一个就会被计算成 auto**。所以那个 div 无论有没有东西可滚，
  都是一个双向的滚动容器；而 sticky 是相对**最近的滚动容器**定位的。
  `top: 58px` 于是变成了"距那个 div 自己的顶边 58px"，
  表头被向下推开 58px、正好压住头两行数据。

  ★ 它不报错、不影响任何一条测试、HTML 和 CSS 都完全合法。
    在浏览器里打开也只是"表格顶部看起来被啃掉一块"。
    这是**唯一**能发现它的方式：有人真的去看了一眼。

================================================================================
这两条测试守的是"别让它回来"，不是"它是对的"
================================================================================
静态检查证明不了视觉效果 —— 它只能钉住"这次学到的那个约束还在"。
真正的确认仍然要靠人去看（README 的已知边界表里如实记着这一点）。

================================================================================
★ 解析 CSS 之前必须先去掉注释（和 test_frontend_scripts.py 同一个坑）
================================================================================
修这个 bug 时，我在 .table-scroll 的注释里**原样写了** `top: 58px`
这四个字符（为了说清楚它是怎么错的）。只写正则的话，
会把注释里那个"反例"当成真的声明读出来，然后报一个假失败 ——
和 test_frontend_scripts.py 里"注释里的 <script> 被当成脚本"是同一个形状。
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
    """取出选择器列表里**恰好等于** selector 的那条规则，解析成 dict。

    ★ 必须去注释后再解析，理由见模块顶部的说明。
    """
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


# ---------------------------------------------------------------------------
# 守门：先确认解析器真的读到了东西
# ---------------------------------------------------------------------------
def test_css_parser_actually_finds_the_rule():
    """★ 守的是**解析器本身**，不是那条规则。

    选择器写错 / 去注释写错的话，`_declarations` 会返回空 dict，
    下面的断言于是拿 `None` 去比 —— 那会失败，还算好事；
    但如果反过来写（比如断言"不含某个值"），一个空 dict 会**静默通过**。
    一个失效的检查比没有检查更糟，所以先钉住"读到了"。
    """
    decls = _declarations("table.data th")
    assert decls, "没解析到 `table.data th` 规则 —— 是选择器变了，还是去注释写错了？"
    assert decls.get("position") == "sticky", (
        f"`table.data th` 不再是 sticky 了（现在是 {decls.get('position')!r}）。"
        "如果不是有意的，说明这条规则被改坏了；如果是有意的，"
        "请一并删掉 .table-scroll 那段注释和本文件。"
    )


# ---------------------------------------------------------------------------
# 真正的不变量
# ---------------------------------------------------------------------------
def test_sticky_table_header_uses_zero_offset():
    """粘住的表头，top 必须是 0。

    理由（完整版见 style.css 里 .table-scroll 那段注释）：
    表头粘的是它所在的滚动容器，不是视口。滚动容器的顶边就是表头在
    正常流里的位置，所以任何非 0 的 top 都会把表头**往下推**、
    压住数据行 —— 既没有"粘住"的效果，又啃掉了表格顶部。

    ★ 这条**不能**证明视觉上没问题（那要人眼看）。
      它只钉住"别再写回顶栏高度那个数"。
    """
    decls = _declarations("table.data th")
    top = decls.get("top")
    assert top == "0", (
        f"`table.data th` 的 top 是 {top!r}，应该是 '0'。\n"
        "如果写的是顶栏高度（58px 之类）：那正是 2026-09 那次人眼确认"
        "抓到的 bug —— 表头会被顶下去压住数据行。"
    )


def test_every_data_table_sits_in_a_scroll_container():
    """每张 `table.data` 都要包在 `.table-scroll` 里。

    这保证了三张表的横向滚动行为一致，而且 max-height 真的生效 ——
    表头才可能"有东西可粘"。包在一个裸的 overflow div 里不会报错，
    但表头就永远不粘（滚动容器从不纵向滚动），CSS 里那句 sticky
    就成了一句不干活的承诺。

    ★ 页面上没有表格的文件（index / analysis）直接跳过，不是失败。
    """
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
