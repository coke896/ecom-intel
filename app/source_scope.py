r"""数据源作用域 —— `?source=` 的单一事实来源。

住在 app/ 而不是 app/api/：Web、分析、脚本三层都要用它，放 Web 层下等于逼别的
层 import Web 层（实际发生过：analysis/payload.py 曾 import app.api.deps）。规矩
因此可以自动检查：**app/ 里的文件不许 import scraper/**，由 tests/test_layering.py
拿 AST 守。

用 `str` 枚举而不是裸 `str` 加手工校验：FastAPI 自动校验（`?source=bogus` 直接
422 并列出合法取值）并进 OpenAPI 文档。默认 all 而不是 mock——"不指定就看全部"
才是诚实的，默认 mock 会让忘带参数的调用方看到假数据而自己不知道。
"""

from __future__ import annotations

from enum import Enum
from typing import TypeVar

from sqlalchemy import Select

from app.db.models import Product


class SourceFilter(str, Enum):
    r"""`?source=` 的合法取值：all=平台现状 / mock=管道测试数据 / smzdm=真实采集。

    ★ 双数据源不是两个库，是一个字段 + 一个过滤参数：两批数据走完全相同的表
      结构、查询路径和图表代码。
    """

    all = "all"
    mock = "mock"
    # 真实采集源：返回空不是 bug，前端有对应的空态提示。
    smzdm = "smzdm"


# 表达"apply_source 不改变传入语句的类型"：select(Product) 进去还是 Select[Product] 出来。
_SelectT = TypeVar("_SelectT")


def apply_source(stmt: Select[_SelectT], source: SourceFilter) -> Select[_SelectT]:
    r"""给任意一条以 Product 为主表的 select() 语句加上数据源过滤。

    ★ 为什么收成一个函数：7 个 stats 端点各写一遍 `if source != "all"` 会出现
      "某个端点漏了过滤"——这类错误**不报错**：接口 200、图表正常，只是数字不对。
    ★ 只对 `all` 不加条件：库里没有 source='all' 的行，写反了返回空结果，而空
      结果比报错更难查（图表一片空白）。
    ★ 约定：查询里必须有 products 表，加的是 WHERE。合法形状：① 主表是 Product；
      ② 主表是别的表但 join 了 Product 且**每条结果恰好对应一个商品**（如
      `AlertRule ⋈ Product`）。join 是"一对多"时不成立。
    """
    if source is SourceFilter.all:
        return stmt
    return stmt.where(Product.source == source.value)
