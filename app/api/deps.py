"""Web 层的依赖注入别名。

================================================================================
这个文件现在只剩"FastAPI 专有"的那一点东西
================================================================================
它**原本**还装着 `SourceFilter` 和 `apply_source()`。那两个被挪到了
`app/source_scope.py`，理由写在那个文件的顶部，一句话概括：

    Web 层、分析层、脚本层都要用"数据源"这个概念，
    而 `app/api/` 是 Web 层——把共用约定放在这里，就是在逼别的层 import Web 层。
    实际也确实发生了：`app/analysis/payload.py` 曾经 `from app.api.deps import ...`。

★ 留在这里的是**真正属于 Web 的东西**——`Annotated[..., Depends(...)]` 别名。
  这些别名里 `Depends` 是 FastAPI 的概念，别的层用不上也不该看见。
  于是边界很清楚：

    app/source_scope.py   什么是数据源、怎么过滤     ← 谁都能 import
    app/api/deps.py       怎么把它变成一个依赖        ← 只有 Web 层 import

★ `SourceFilter` 从这里**重新导出**了一次（下面那行 `as` 导入）。
  这是刻意的：`SourceDep` 的类型是 `SourceFilter`，而端点文件
  （`app/api/products.py` 之类）既需要 `SourceDep` 也需要 `SourceFilter`。
  不重导的话，每个端点文件都要写两行 import 去两个地方——
  那是一种为了纯粹而制造的噪声。
  ★ 但**别的层**（比如 `app/analysis/`）必须从 `app.source_scope` 直接 import，
    不能图省事从这里拿。规矩由 `tests/test_layering.py` 守着。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.source_scope import SourceFilter as SourceFilter  # ← 重新导出，见模块注释


def source_filter(source: SourceFilter = SourceFilter.all) -> SourceFilter:
    """把查询参数解析成 SourceFilter。

    单独包一层函数而不是直接 `Annotated[SourceFilter, Query()]`：
    这样 `SourceDep` 是一个明确的、可被别的模块 import 的依赖，
    将来要在过滤之外再做点什么（比如权限、租户）也有地方放。
    """
    return source


# 端点里直接写这两个别名，签名会短很多：
#     def overview(db: DbDep, source: SourceDep) -> OverviewOut:
DbDep = Annotated[Session, Depends(get_db)]
SourceDep = Annotated[SourceFilter, Depends(source_filter)]
