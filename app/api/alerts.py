"""告警接口 —— 建规则、看记录、手动查一次。

    POST   /api/alerts/rules      建一条规则
    GET    /api/alerts/rules      规则列表
    PATCH  /api/alerts/rules/{id} 改规则（阈值 / 规则类型 / 启用停用）
    DELETE /api/alerts/rules/{id} 删规则（连同它的触发记录）
    GET    /api/alerts            触发记录
    POST   /api/alerts/check      立刻查一轮（不等 worker 的 5 分钟）

================================================================================
★ 这个文件里没有告警逻辑，一行都没有
================================================================================
判断"价格够不够低"、推进 armed/fired 状态机、写 alerts 行的，全是
`scraper/check_alerts.py`。这里只做三件事：把请求翻译成数据库操作、
把结果翻译成响应模型、把非法输入挡在门外。

理由和 `app/api/tasks.py` 一样：**同一套规则写两遍就会分叉**
（这个项目已经因此栽过三次，见 docs/可行性评估.md 第十五节）。
尤其不能让接口自己实现一遍状态机——那等于让"触发一次"这件事有两个实现，
而两个实现对于"什么算同一个事件"的理解迟早会不一样。

================================================================================
四个必须在接口层拍板的决定
================================================================================

【1】`rule_type` 的合法值来自 `check_alerts.RULE_TYPES`，不在这里另写一份

    代价是 OpenAPI 上它是一个自由文本框而不是下拉框（FastAPI 的参数校验
    需要 Enum 类型才能给出取值列表）。
    这里选了"正确"而不是"文档好看"：**能自动生成下拉框的那个写法，
    必然要维护第二份合法值清单**，而那份清单会分叉——
    加一种新规则类型时改了 RULE_TYPES 忘了改枚举，接口就会挡下合法的类型。
    分叉的代价（新类型永远建不出来）比文档少个下拉框大得多。

【2】同一个商品 + 同一种规则 + 同一个阈值 → 拒绝，不建第二条
    ★ 这条检查**建成和改成都走同一个函数**（`_assert_not_duplicate`）：
      只挡"建成"的话，改一个阈值就能绕过去，防线的形状是漏的。

    和 tasks.py 里"同一个源不重复入队"是同一条理由：**两条一模一样的规则
    会对同一个价格事件各写一条 alerts、各推一次通知**。
    状态机只能防住"一条规则重复触发"，防不住"两条规则同时触发"。
    挡在后端而不是前端，也是同一条理由（前端的防抖挡不住 curl 和第二个标签页）。

【3】改规则 = 重新武装（`state` 回到 `armed`）

    这一条不显眼，但少了它会出现一个**静默的空洞**：

        规则"低于 100 提醒我" → 价格跌到 99 → 触发，state=fired
        → 用户觉得阈值太低，改成"低于 200 提醒我"
        → 当前价 99 < 200，条件成立，但 state 还是 fired → **跳过**
        → 用户以为改完就会响，实际它永远不会响（除非价格先涨回 200 以上）

    改阈值/规则类型，等于换了一个判断条件；改启用状态，等于重新开始看守。
    两种情况下"之前触发过"这个事实都不再适用，所以一律回到 armed。
    ★ 代价是改完可能立刻收到一次通知（因为条件当下就成立）——
      这是对的：用户刚刚亲手要求了"这个条件下提醒我"。

【4】删规则时显式删掉它的 alerts，而**不是**用 ON DELETE CASCADE

    看似应该让数据库级联删，但本项目**没有迁移工具**（建表用 create_all，
    它不会改动已经存在的表）。于是给模型加上 `ondelete="CASCADE"`
    只对**新建的库**生效，而 `data/ecom.db` 和别人的库不会变——
    同一份代码在两台机器上行为不同，正是这个项目反复记录的那类问题。

    所以：显式删，写在代码里，对哪个库都一样。
    也顺便在响应里说清楚——要保留触发历史就**停用**，别删。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.api.deps import DbDep, SourceDep, apply_source
from app.db.models import Alert, AlertRule, PriceHistory, Product
from scraper.check_alerts import RULE_PRICE_DROP_PCT, RULE_TYPES, check_alerts

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

# 规则总数上限。
#
# ★ 这不是"怕数据库撑不住"——几百行规则对 SQLite 毫无压力。
#   限制它的原因是**检查是每 5 分钟一轮的全量扫描**：规则数直接决定
#   那个周期性动作的代价。给它一个上限，等于给"后台开销"设了一个已知的边界，
#   而不是"用户加多少就多少"。
#   真实的告警系统都有这个上限（否则一次配置失误就能让检查跑不完）。
MAX_RULES = 100


# ===========================================================================
# 请求 / 响应模型
# ===========================================================================


class RuleCreate(BaseModel):
    """建规则的请求体。"""

    product_id: int = Field(description="给哪个商品配告警")
    rule_type: str = Field(
        description=f"规则类型，合法取值：{'、'.join(RULE_TYPES)}"
        "（★ 依赖 check_alerts.RULE_TYPES，接口不另存一份清单）"
    )
    threshold: float = Field(
        gt=0,
        description=(
            "阈值。price_below 是价格（元）；price_drop_pct 是跌幅百分比（如 20 表示跌 20%）。"
            "★ 必须大于 0：阈值 0 意味着'价格跌破 0 元'，那永远不会发生——"
            "而它会安静地待在列表里，看起来像一条正常工作的规则。"
        ),
    )


class RuleUpdate(BaseModel):
    """改规则。★ 三个字段都是可选的，只改传进来的那些。"""

    threshold: float | None = Field(default=None, gt=0)
    rule_type: str | None = None
    enabled: bool | None = None


class AlertRuleOut(BaseModel):
    """一条规则。"""

    id: int
    product_id: int
    product_title: str
    product_source: str
    """商品的数据血缘（mock / smzdm）。

    ★ 界面上要显示它。同一个列表里可能既有 Mock 商品的规则、
      又有真实商品的规则，而"这条规则监控的是演示数据"是使用者
      必须一眼看见的事——看不到的话，他会对着一条永远不会触发的
      Mock 规则等下去。"""

    rule_type: str
    threshold: float
    enabled: bool
    state: str
    """armed / fired。

    ★ 界面必须显示它，因为它承载了状态机的全部可观测状态：
      "已经通知过了，正在等价格涨回去"只能从这里看出来。
      不显示的话，用户看到一条"启用中"且价格低于阈值的规则却没有新通知，
      会以为是坏了——其实这正是设计。"""

    fired_count: int = 0
    """这条规则历史上触发过几次（alerts 的行数）。"""
    created_at: datetime

    warning: str | None = None
    """★ 建/改规则时如果发现它**在可预见的将来不可能触发**，这里给一句人话。

    目前只有一种情况会填它：给一个历史点数不足的商品配跌幅规则（原因见
    `_baseline_warning`）。

    ★ 为什么是"警告"而不是"拒绝"：这样的规则**将来会变得有用**——
      商品被再次爆料时就会多出第二个价格点。拦下来是错的，
      但让它安安静静地不响也是错的（用户会以为告警系统坏了）。
      所以照建，但把话说在前面。

    ★ 为什么放在响应里而不是只在前端判断：前端要算它就得自己查一遍
      price_history，那是第二份实现，而且 curl 的人看不到。
    """


class RuleListOut(BaseModel):
    items: list[AlertRuleOut] = Field(default_factory=list)
    total: int = 0
    max_rules: int = MAX_RULES


class AlertOut(BaseModel):
    """一条触发记录。"""

    id: int
    rule_id: int
    product_id: int
    product_title: str
    rule_type: str
    threshold: float
    triggered_at: datetime
    price_at_trigger: float | None

    notified: bool = False
    """★ **当前恒为 False**，因为本项目没有外部推送渠道。

    日志和这个列表都是【拉】的——用户打开页面才看得到。
    字段留着是为了将来接推送时不用改表结构。
    ★ 界面不要把 False 渲染成"推送失败"：现在根本没有东西可失败。
    """


class AlertListOut(BaseModel):
    items: list[AlertOut] = Field(default_factory=list)
    total: int = 0


class CheckResultOut(BaseModel):
    """手动查一轮的结果。"""

    rules_checked: int = 0
    fired: int = 0
    rearmed: int = 0
    undecidable: int = 0
    reasons: list[str] = Field(default_factory=list)
    message: str = ""


# ===========================================================================
# 内部工具
# ===========================================================================


def _validate_rule_type(rule_type: str) -> str:
    """校验规则类型，非法就 422 并列出合法取值。

    ★ 非法输入要**说清楚有哪些合法值**。只说"不支持的类型"的话，
      调用方（包括前端开发者和 curl 的人）得去翻源码才知道该填什么。
    """
    if rule_type not in RULE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"规则类型 {rule_type!r} 不认识。"
                f"合法取值：{'、'.join(RULE_TYPES)}"
            ),
        )
    return rule_type


def _validate_threshold(rule_type: str, threshold: float) -> float:
    """跌幅阈值不能超过 100%。

    ★ 跌幅是 (历史最高 - 当前) / 历史最高 × 100，它的上界是 100
      （当前价为 0 时）。填 150 表示"要跌掉 1.5 倍"——永远不可能成立。
      挡下来比让它静静地不触发好：后者看起来像"规则在工作，只是还没到"。
    """
    if rule_type == RULE_PRICE_DROP_PCT and threshold > 100:
        raise HTTPException(
            status_code=422,
            detail=(
                f"跌幅阈值 {threshold} 超过 100%：价格最多只能跌到自己变成 0，"
                "所以跌幅不可能大于 100。这条规则永远不会触发——"
                "如果你要的是别的意思，请改 rule_type。"
            ),
        )
    return threshold


def _fired_counts(db: Session) -> dict[int, int]:
    """每条规则触发过几次。

    ★ 一次 group by 查完，不在循环里逐个 count：
      逐个数是 N+1 查询的经典形态（同 scraper/check_alerts.py 里的理由）。
    """
    rows = db.execute(
        select(Alert.rule_id, func.count()).group_by(Alert.rule_id)
    ).all()
    return {int(rid): int(n) for rid, n in rows}


def _baseline_warning(db: Session, product_id: int, rule_type: str) -> str | None:
    """给"现在配了也不会响"的规则一句提醒（不拦，只提醒）。

    ★ 这一条来自实测数据，不是假想的问题：

        库里 100 个真实商品（source='smzdm'）中，
        98 个只有【1 个】价格点，2 个没有价格点，**0 个有 2 个以上**。
        （原因见 docs/可行性评估.md 16.5：抓来的是爆料，同一商品重复爆料才会
         产生第二个价格点，而那次采集里还没发生。）

      于是给它们配跌幅规则会得到两种安静的失败：
        · 1 个点 → 基准 = 那唯一的价 = 当前价 → 跌幅恒为 0，永远不触发
        · 0 个点 → 每轮都记一条"判不了"（这一条至少在报告里看得见）

    ★ 为什么**不拒绝**这种规则：
      它不是错的，只是**还没到时候**——商品被再次爆料时会多出第二个价格点，
      那时这条规则立刻就有意义了。拦下来等于禁止用户提前配好。
      要防的是"用户以为它在看着"，所以照建、但把话说在前面。
    """
    if rule_type != RULE_PRICE_DROP_PCT:
        return None

    points = (
        db.scalar(
            select(func.count())
            .select_from(PriceHistory)
            .where(PriceHistory.product_id == product_id)
        )
        or 0
    )
    if points == 0:
        return (
            "这个商品还没有任何价格历史点，跌幅规则现在【判不了】——"
            "每轮检查都会在报告里记一条'判不了'。"
            "等它被采集到一次价格后这条规则才会真正开始工作。"
        )
    if points == 1:
        return (
            "这个商品目前只有 1 个价格点，跌幅的基准就是当前价，"
            "所以跌幅恒为 0——这条规则【在它出现第二个价格点之前不会触发】。"
            "★ 第二个点要靠这个商品被再次爆料（见 docs/可行性评估.md 16.5）。"
        )
    return None


def _rule_out(
    rule: AlertRule,
    product: Product,
    fired_count: int,
    *,
    warning: str | None = None,
) -> AlertRuleOut:
    return AlertRuleOut(
        id=rule.id,
        product_id=rule.product_id,
        product_title=product.title,
        product_source=product.source,
        rule_type=rule.rule_type,
        threshold=rule.threshold,
        enabled=rule.enabled,
        state=rule.state,
        fired_count=fired_count,
        created_at=rule.created_at,
        warning=warning,
    )


def _get_rule(db: Session, rule_id: int) -> AlertRule:
    rule = db.get(AlertRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"没有 id={rule_id} 的告警规则")
    return rule


def _assert_not_duplicate(
    db: Session,
    *,
    product_id: int,
    rule_type: str,
    threshold: float,
    exclude_id: int | None = None,
) -> None:
    """有完全相同的规则就 409。

    ★ 建成和改成都必须走这里。
      只挡"建成"的话，防线可以被绕过：
        规则 A（商品 5，低于 100）、规则 B（商品 5，低于 150）
        → 把 A 的阈值改成 150 → **两条一一样的规则**，谁也没挡住。
      这正是"同一条规则写两遍就会分叉"的另一个面相——
      不是两处实现不一致，而是**一处有、一处没有**。

    ★ 比较是**精确的浮点相等**，不是"差不多"。
      引入容差就要回答"多接近算一样"，那是个需要解释的魔法数字；
      而两条阈值相近的规则本来就是用户自己配的，不算错误——
      真正要防的是"连点两次提交建出两条一模一样的"。
    """
    query = select(AlertRule).where(
        AlertRule.product_id == product_id,
        AlertRule.rule_type == rule_type,
        AlertRule.threshold == threshold,
    )
    if exclude_id is not None:
        query = query.where(AlertRule.id != exclude_id)

    duplicate = db.execute(query).scalar_one_or_none()
    if duplicate is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"已经有一条一模一样的规则（#{duplicate.id}）了："
                "同一个商品、同一种类型、同一个阈值。"
                "★ 两条相同规则会对同一个价格事件各写一条记录——"
                "状态机只能防住一条规则重复触发，防不住两条规则同时触发。"
            ),
        )


# ===========================================================================
# 端点：规则
# ===========================================================================


@router.post("/rules", response_model=AlertRuleOut)
def create_rule(db: DbDep, payload: RuleCreate) -> AlertRuleOut:
    """建一条告警规则。

    ★ 新规则一律从 `armed` 开始，所以**如果条件当下就成立，下一轮检查会立刻触发**。
      这是对的：用户说"低于 200 提醒我"，而它现在就是 180——
      他需要知道这件事，而不是"等它先涨回去再跌下来"。
    """
    _validate_rule_type(payload.rule_type)
    _validate_threshold(payload.rule_type, payload.threshold)

    product = db.get(Product, payload.product_id)
    if product is None:
        raise HTTPException(
            status_code=404, detail=f"没有 id={payload.product_id} 的商品"
        )

    # ★ 规则总数上限。见 MAX_RULES 的说明。
    total = db.scalar(select(func.count()).select_from(AlertRule)) or 0
    if total >= MAX_RULES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"告警规则已达上限 {MAX_RULES} 条。"
                "★ 检查是每 5 分钟一轮的全量扫描，规则数直接决定那个动作的代价——"
                "请先删掉一些不需要的，而不是把上限调高。"
            ),
        )

    # ★ 完全相同的规则不重复建。理由见文件顶部【2】。
    _assert_not_duplicate(
        db,
        product_id=payload.product_id,
        rule_type=payload.rule_type,
        threshold=payload.threshold,
    )

    rule = AlertRule(
        product_id=payload.product_id,
        rule_type=payload.rule_type,
        threshold=payload.threshold,
        enabled=True,
        state="armed",
    )
    db.add(rule)
    db.commit()

    return _rule_out(
        rule,
        product,
        fired_count=0,
        warning=_baseline_warning(db, rule.product_id, rule.rule_type),
    )


@router.get("/rules", response_model=RuleListOut)
def list_rules(
    db: DbDep,
    source: SourceDep,
    limit: int = Query(200, ge=1, le=500, description="最多返回几条"),
) -> RuleListOut:
    """规则列表，最新的在前。

    ★ 一次 join 出商品标题，不做懒加载（N+1）。
    ★ 支持 `?source=`：这条查询的主表是 alert_rules，但它**join 了 Product
      且一条规则恰好对应一个商品**，所以过滤 `Product.source` 正好是
      "只看这个数据源的规则"。这是 apply_source 的第二种合法形状，
      写在 deps.py 的约定里（那里也写了它不适用的形状）。
    """
    stmt = (
        select(AlertRule, Product)
        .join(Product, Product.id == AlertRule.product_id)
        .order_by(AlertRule.id.desc())
        .limit(limit)
    )
    rows = db.execute(apply_source(stmt, source)).all()
    counts = _fired_counts(db)

    return RuleListOut(
        items=[_rule_out(rule, product, counts.get(rule.id, 0)) for rule, product in rows],
        total=len(rows),
    )


@router.patch("/rules/{rule_id}", response_model=AlertRuleOut)
def update_rule(
    db: DbDep,
    payload: RuleUpdate,
    rule_id: int = Path(description="规则 id"),
) -> AlertRuleOut:
    """改一条规则。**任何改动都会把状态重置回 `armed`**（理由见文件顶部【3】）。"""
    rule = _get_rule(db, rule_id)

    new_type = payload.rule_type if payload.rule_type is not None else rule.rule_type
    if payload.rule_type is not None:
        _validate_rule_type(payload.rule_type)
    new_threshold = (
        payload.threshold if payload.threshold is not None else rule.threshold
    )
    _validate_threshold(new_type, new_threshold)

    # ★ 改完不能和别的规则撞成一对。
    #   在**赋值之前**检查：此刻会话里还没有未提交的改动，
    #   查询看到的就一定是库里的真实状态（不依赖 autoflush 的设置）。
    if new_type != rule.rule_type or new_threshold != rule.threshold:
        _assert_not_duplicate(
            db,
            product_id=rule.product_id,
            rule_type=new_type,
            threshold=new_threshold,
            exclude_id=rule.id,
        )

    changed = False
    if payload.rule_type is not None and payload.rule_type != rule.rule_type:
        rule.rule_type = payload.rule_type
        changed = True
    if payload.threshold is not None and payload.threshold != rule.threshold:
        rule.threshold = payload.threshold
        changed = True
    if payload.enabled is not None and payload.enabled != rule.enabled:
        rule.enabled = payload.enabled
        changed = True

    # ★ 重置状态放在"有改动"的分支里，而不是无条件执行：
    #   一个空 PATCH（或者传了和当前值相同的值）不该改变状态机的状态。
    #   "保存了一下但什么都没改，于是重新武装了"是个很难查的怪事。
    if changed:
        rule.state = "armed"

    db.commit()
    product = db.get(Product, rule.product_id)
    assert product is not None  # 外键保证；真丢了说明数据被手工改坏了
    return _rule_out(
        rule,
        product,
        _fired_counts(db).get(rule.id, 0),
        # ★ 无条件算，不是只在"这次改动使它变成跌幅规则"时算：
        #   要提示的是"这条规则现在响不了"，和它是哪次改出来的无关。
        #   （改成跌幅规则和只改跌幅阈值，两条路径都要提示。）
        warning=_baseline_warning(db, rule.product_id, rule.rule_type),
    )


@router.delete("/rules/{rule_id}")
def delete_rule(db: DbDep, rule_id: int = Path(description="规则 id")) -> dict[str, object]:
    """删一条规则，连同它的触发记录。

    ★ 要保留历史就**停用**（PATCH enabled=false），别删。
      见文件顶部【4】：这里显式删而不是靠 ON DELETE CASCADE，
      因为本项目没有迁移工具，级联只对新建的库生效。
    """
    rule = _get_rule(db, rule_id)

    deleted_alerts = db.execute(
        delete(Alert).where(Alert.rule_id == rule.id)
    ).rowcount
    db.delete(rule)
    db.commit()

    return {
        "deleted_rule_id": rule_id,
        "deleted_alerts": int(deleted_alerts or 0),
        "notice": (
            "规则和它的触发记录都已删除。"
            "下次想保留历史请改用 PATCH 停用，而不是删除。"
        ),
    }


# ===========================================================================
# 端点：触发记录
# ===========================================================================


@router.get("", response_model=AlertListOut)
def list_alerts(
    db: DbDep,
    source: SourceDep,
    limit: int = Query(50, ge=1, le=500, description="最多返回几条"),
) -> AlertListOut:
    """触发记录，最新的在前。★ 同样支持 `?source=`（理由见 list_rules）。"""
    stmt = (
        select(Alert, AlertRule, Product)
        .join(AlertRule, AlertRule.id == Alert.rule_id)
        .join(Product, Product.id == AlertRule.product_id)
        .order_by(Alert.triggered_at.desc(), Alert.id.desc())
        .limit(limit)
    )
    rows = db.execute(apply_source(stmt, source)).all()

    return AlertListOut(
        items=[
            AlertOut(
                id=alert.id,
                rule_id=alert.rule_id,
                product_id=rule.product_id,
                product_title=product.title,
                rule_type=rule.rule_type,
                threshold=rule.threshold,
                triggered_at=alert.triggered_at,
                price_at_trigger=alert.price_at_trigger,
                notified=alert.notified,
            )
            for alert, rule, product in rows
        ],
        total=len(rows),
    )


# ===========================================================================
# 端点：手动查一轮
# ===========================================================================


@router.post("/check", response_model=CheckResultOut)
def run_check(db: DbDep) -> CheckResultOut:
    """立刻查一轮，不等 worker 的 5 分钟。

    ★ 为什么这件事**可以**在 Web 进程里做，而采集不行：

      采集不行是因为 Playwright 在 Windows 的 uvicorn 事件循环里必崩
      （见 scraper/worker.py 的模块注释），而且长任务不能绑在请求上。
      而告警检查是一次纯数据库的读写，几毫秒就完了，没有这两个问题。

    ★ 那"告警跑在 worker 里"的意义是什么？
      是**周期性**这件事不该由 Web 层承担，而不是"只有 worker 能碰它"。
      两个进程同时检查也是安全的：状态转换是带条件的 UPDATE
      （`WHERE state='armed'`），谁先到谁赢，后到的拿到 0 行、安静退出。
      正因为如此，手动触发一次是安全的——这恰好也是那条原子性设计
      给出来的一个额外好处。

    ★ **不接受 `?source=`**，和上面两个列表端点不一样，这是有意的：
      它是一次**动作**，不是一次查询。让它跟着界面上的数据源开关走，
      就会出现"我在 mock 视图下点了检查，结果我那条真实商品的规则
      没被查到"——而且不会有任何提示。检查永远是全量的，
      返回的报告也是全量的；要看某个数据源的情况，去看列表。
    """
    report = check_alerts(db)
    return CheckResultOut(
        rules_checked=report.rules_checked,
        fired=report.fired,
        rearmed=report.rearmed,
        undecidable=report.undecidable,
        reasons=report.reasons,
        message=report.as_message(),
    )
