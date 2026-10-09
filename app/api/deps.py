"""Web 层的依赖注入别名（`Annotated[..., Depends(...)]`）——边界：
    app/source_scope.py   什么是数据源、怎么过滤   ← 谁都能 import
    app/api/deps.py       怎么把它变成一个依赖     ← 只有 Web 层 import

★ `SourceFilter` 在这里重新导出一次（下面 `as` 那行）：端点文件两个都要用；
  **别的层**必须直接从 `app.source_scope` import，规矩由 tests/test_layering.py 守。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.source_scope import SourceFilter as SourceFilter  # ← 重新导出，见模块注释


def source_filter(source: SourceFilter = SourceFilter.all) -> SourceFilter:
    """把查询参数解析成 SourceFilter。单独包一层而不是直接 `Annotated[...]`：
    这样 `SourceDep` 是可被别的模块 import 的明确依赖，将来加权限/租户也有地方放。
    """
    return source


# 端点里直接写这两个别名，签名会短很多（def overview(db: DbDep, source: SourceDep)）。
DbDep = Annotated[Session, Depends(get_db)]
SourceDep = Annotated[SourceFilter, Depends(source_filter)]
