"""数据源作用域 —— `?source=` 的单一事实来源。

================================================================================
为什么这个模块在 app/ 根下，而不在 app/api/ 下
================================================================================
它**本来**住在 `app/api/deps.py` 里。挪出来的原因是依赖方向：

    app/analysis/payload.py   from app.api.deps import SourceFilter, apply_source
    app/analysis/reporter.py  from app.api.deps import SourceFilter

也就是说**分析层 import 了 Web 层**。而 `app/api/images.py` 里明写着本项目的
一条规矩："Web 层依赖采集层，方向正好和本项目的架构相反"——一条自己都
没遵守的规矩，比没有规矩更糟。

★ 但真正的问题不是"违反了规矩"，是**当初把它放进 app/api/ 就是个分类错误**：

    `SourceFilter` 是个纯枚举，`apply_source()` 是个纯 SQLAlchemy 函数，
    两个都不 import FastAPI、不知道 HTTP 是什么、也不产生任何响应。
    它们只是"关于数据源这件事的一个约定"，被 Web 层、分析层、脚本层共用。

  它住在 `app/api/` 下纯属历史——因为最早用到它的是 stats 端点。
  "最早用它的地方"和"它属于哪里"是两件事，前者不该决定后者。

于是拆成两个文件，各归其位：

    app/source_scope.py   ← 本文件：什么是数据源、怎么过滤（与 Web 无关）
    app/api/deps.py       ← FastAPI 的 Depends 别名（是 Web 专有的，留在 Web 层）

规矩因此变成一条可以自动检查的话：**app/ 里的任何文件都不许 import scraper/。**
`tests/test_layering.py` 拿 AST 走一遍全目录来守它——不是写在注释里，
是跑测试时会红。

================================================================================
关于 `?source=` 这个设计本身
================================================================================
`SourceFilter` 是个 `str` 枚举而不是裸 `str` 加手工校验，三个理由：

  · FastAPI 自动做参数校验：`?source=bogus` 直接 422 并列出合法取值，
    不需要手写 if/raise
  · 自动进 OpenAPI 文档，`/docs` 上是一个下拉框而不是自由文本框
  · 继承 `str` 是为了和 SQLAlchemy 的字符串列比较时不用 `.value`
    （枚举成员的 `==` 对 str 是成立的，但为了显式起见下面还是写 `.value`）

默认值是 `all` 而不是 `mock`：默认 `all` 才是诚实的——"不指定就看全部"。
如果默认 `mock`，那么任何忘了带参数的调用方看到的都是 Mock 数据，
而它自己并不知道，又是一个静默错误。
"""

from __future__ import annotations

from enum import Enum
from typing import TypeVar

from sqlalchemy import Select

from app.db.models import Product


class SourceFilter(str, Enum):
    r"""`?source=` 的合法取值。

    三个值对应三种问法：
      all   —— "这个平台里现在有什么"（演示时的默认视角）
      mock  —— "把管道跑起来的那批数据长什么样"（验证分析逻辑）
      smzdm —— "真实采到的数据长什么样"（验证采集管道）

    面试可讲：**双数据源不是两个库，是一个字段 + 一个过滤参数。**
    好处是两批数据走的是完全相同的表结构、完全相同的查询路径、
    完全相同的图表代码——所以"Mock 数据验证过的分析逻辑"这句话是真的，
    而不是"我在两套代码上各验了一遍"。
    """

    all = "all"
    mock = "mock"
    # 真实采集源。现阶段库里还没有这个 source 的数据，
    # 所以 `?source=smzdm` 会返回空结果——这不是 bug，
    # 是阶段④ 之前应有的状态，前端有对应的空态提示。
    smzdm = "smzdm"


# 一个类型变量，用来表达"apply_source 不改变传入语句的类型"。
# select(Product) 进去还是 Select[Product] 出来，而不是退化成裸 Select。
_SelectT = TypeVar("_SelectT")


def apply_source(stmt: Select[_SelectT], source: SourceFilter) -> Select[_SelectT]:
    r"""给任意一条以 Product 为主表的 select() 语句加上数据源过滤。

    ★ 为什么必须收成一个函数，而不是每个端点里写一遍
      `if source != "all": stmt = stmt.where(...)`：

      这是"复制粘贴出来的 bug"的经典温床。7 个 stats 端点各写一份，就会出现：
        · 某一个端点漏了过滤 → Mock 和真实数据混在一起算，图上多出一批
          来路不明的商品
        · 某一个端点写成了 `source == "all"`（把条件写反）→ 结果刚好相反
        · 新增第四个数据源时改漏一处

      而这些错误的共同特征是**不报错**：接口正常返回 200，图表正常渲染，
      只是数字不对。等到面试现场被人问"这 1000 个商品里有多少是真实的"才暴露。

      收成一个函数之后，"漏过滤"从"7 分之 1 的概率"变成"改这一个函数才对"。

    ★ 只对 `all` 不加条件，而不是 `else: where(source == 'all')`——
      数据库里根本没有 source='all' 的行，写反了会返回空结果。
      空结果比报错更难查：图表一片空白，看起来像前端坏了。

    ★ 本函数的约定是：**查询里必须有 products 表**，且 `Product.source`
      这个条件在语义上就是调用方想要的那个过滤。目前有两种合法形状：

        ① 以 Product 为主表（stats 层的每一条语句都是这样）
        ② 主表是别的表，但 join 了 Product，且**每条结果恰好对应一个商品**
           ——比如告警规则列表（`AlertRule ⋈ Product`，一条规则一个商品）

      ★ 为什么 ② 也成立、而且不会多出或少掉行：
        加的是 WHERE（不是 JOIN 条件），所以它只**过滤**不**连接**；
        而一条规则只挂在唯一一个商品上，过滤掉不等价的商品
        就等于过滤掉那一条规则——不会因为 join 而把一行拆成多行。
        `tests/test_api_alerts.py` 里两条按数据源过滤的用例守着它。

      ★ 不成立的形状要说清楚：如果 join 的是"一对多"（比如从 Product
        去 join 它的所有价格点），那么过滤 `Product.source` 并不会
        把不想看的价格点去掉，只是把它们所属的商品整批留下或去掉。
        那种查询要过滤的是**商品**，不是价格点——条件该写在别处。
    """
    if source is SourceFilter.all:
        return stmt
    return stmt.where(Product.source == source.value)
