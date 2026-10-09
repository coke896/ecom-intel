"""告警检查 —— 状态机（armed / fired）。

★ 为什么必须有状态：最直觉的"价格 < 阈值 → 写一条 alerts"在价格**持续**低于
  阈值期间每检查一次就触发一次（5 分钟一轮 → 一天 288 条），而"跌破"只发生
  一次。armed+成立→触发；fired+成立→跳过；fired+不成立→重新武装（**不发
  通知**——"涨回阈值上方"不是可执行的信息）；armed+不成立→什么也不做。
  重新武装**必须存在**：跌→涨→再跌时第二次跌破是新事件，永久熄火会漏报。

★ price_drop_pct 的基准必须从 price_history 现算（MAX），**不能用
  products.price_max**（那是"本次爆料的价格区间上限"）：真实数据上它恰好等于
  当前价，于是跌幅恒为 0、告警永不触发，**而且不报错**。

★ 并发安全：状态转换是带条件的 UPDATE（`WHERE state='armed'`），谁先到谁赢。
  跑在 worker 轮询里是**部署决定**（要固定节奏），不是"Web 进程跑不了它"——
  那是采集的限制（见 app/main.py 第 2 条）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import Alert, AlertRule, PriceHistory, Product

# ★ logger 名用 `__name__` 而不手打：模块搬过家，手打的名字漏改后日志来源会说
#   错话，而"按 logger 名过滤"的配置不会报错、只会一条都收不到。
logger = logging.getLogger(__name__)


# 规则类型的合法取值。★ 写成元组而不是散落在 if/elif 里，是为了让"有哪些规则类型"
# 成为可被 import、可被测试、可被接口校验的**数据**。
RULE_PRICE_BELOW = "price_below"
RULE_PRICE_DROP_PCT = "price_drop_pct"
RULE_TYPES = (RULE_PRICE_BELOW, RULE_PRICE_DROP_PCT)


@dataclass(slots=True)
class AlertReport:
    """一轮告警检查的结果。

    ★ 数字必须来自**真实发生了什么**。`undecidable`（判不了）单独计数：它和
      "没触发"在界面上长得一样，含义却相反——没触发是规则在正常工作，判不了
      是规则**根本没工作**。
    """

    rules_checked: int = 0
    fired: int = 0
    rearmed: int = 0
    undecidable: int = 0
    # 每个判不了的规则给一句人能看懂的原因，写进日志。
    reasons: list[str] = field(default_factory=list)

    def as_message(self) -> str:
        """一行摘要（worker 日志和接口都用它）。"""
        if not self.rules_checked:
            return "没有启用中的告警规则"
        parts = [f"检查 {self.rules_checked} 条规则"]
        if self.fired:
            parts.append(f"触发 {self.fired}")
        if self.rearmed:
            parts.append(f"重新武装 {self.rearmed}")
        if self.undecidable:
            parts.append(f"★ {self.undecidable} 条判不了")
        if not (self.fired or self.rearmed or self.undecidable):
            parts.append("无变化")
        return "，".join(parts)


def _triggered(
    rule_type: str,
    threshold: float,
    price: float,
    baseline: float | None,
) -> bool | None:
    """判断规则是否成立。返回 None 表示【判不了】。

    ★ 纯函数不碰数据库：条件判断可以单独测，收集数据是调用方的事。
    """
    if rule_type == RULE_PRICE_BELOW:
        return price < threshold

    if rule_type == RULE_PRICE_DROP_PCT:
        # 没有基准价就判不了。★ 不能把"基准缺失"当成"跌幅 0"——
        # 那会让一条从没算过的规则看起来"检查过了、没到阈值"。
        if baseline is None or baseline <= 0:
            return None
        return (baseline - price) / baseline * 100.0 >= threshold

    # 不认识的类型（手工往库里写了一行，或将来加了新类型但这里忘了改）。
    # ★ 返回 None 而不是 False：False 的意思是"检查过了、条件不成立"，那是句假话。
    return None


def _reasons_for_skipping(
    rule_type: str,
    product_id: int,
    price: float | None,
    baseline: float | None,
) -> str:
    """给"判不了"的规则生成一句能定位问题的原因。"""
    if rule_type not in RULE_TYPES:
        return f"规则类型不认识：{rule_type!r}（已知：{'/'.join(RULE_TYPES)}）"
    if price is None:
        return f"商品 #{product_id} 没有当前价（源站页面上就没写价格）"
    if rule_type == RULE_PRICE_DROP_PCT and (baseline is None or baseline <= 0):
        return f"商品 #{product_id} 没有可用的历史价，算不出跌幅基准"
    return f"商品 #{product_id} 的原因不明（这是个 bug，不该发生）"


def _baselines(db: Session, product_ids: set[int]) -> dict[int, float]:
    """一次查出所有商品的历史最高价。★ 一次查完而不是循环里逐个查（N+1）；
    MAX(price) 而不是 MAX(price_max)：见模块顶部。
    """
    if not product_ids:
        return {}
    rows = db.execute(
        select(PriceHistory.product_id, func.max(PriceHistory.price))
        .where(PriceHistory.product_id.in_(product_ids))
        .group_by(PriceHistory.product_id)
    ).all()
    # ★ float() 转换让类型是确定的，不必让调用方去猜这个值会不会是 Decimal 或字符串。
    return {int(pid): float(mx) for pid, mx in rows if mx is not None}


def check_alerts(db: Session, *, now: datetime | None = None) -> AlertReport:
    """检查所有启用中的告警规则，推进状态机。返回本轮统计。

    ★ 原子性靠**带条件的 UPDATE**，而不是"先读 state 再判断要不要写"：
      "先查后改"在并发下会**双写**（两个进程同时读到 armed，同一事件写两条
      alerts）；`WHERE state='armed'` 有行返回就说明我抢到了这次触发——
      **WHERE 子句才是权威，上面读到的 `rule.state` 只是快路径**
      （见 docs/可行性评估.md 16.10）。
    ★ 一次 commit：状态转换和 Alert 行在同一事务里，不会出现"标了 fired 但没
      写记录"的半截状态；崩在中途整体回滚，下一轮重新判，不会漏报。
    """
    when = now or utcnow()

    # 一次查出「规则 + 商品」（价格和标题都要用）。★ join 而不是懒加载
    #   relationship：后者会在循环里触发 N+1（和 _baselines 同一条理由）。
    rows = db.execute(
        select(AlertRule, Product)
        .join(Product, Product.id == AlertRule.product_id)
        .where(AlertRule.enabled.is_(True))
        .order_by(AlertRule.id)
    ).all()

    report = AlertReport(rules_checked=len(rows))
    if not rows:
        return report

    drop_product_ids = {
        rule.product_id for rule, _ in rows if rule.rule_type == RULE_PRICE_DROP_PCT
    }
    baselines = _baselines(db, drop_product_ids)

    for rule, product in rows:
        baseline = baselines.get(rule.product_id)
        verdict = (
            None
            if product.price is None
            else _triggered(
                rule.rule_type, rule.threshold, product.price, baseline
            )
        )

        if verdict is None:
            report.undecidable += 1
            reason = _reasons_for_skipping(
                rule.rule_type, rule.product_id, product.price, baseline
            )
            report.reasons.append(f"规则 #{rule.id}：{reason}")
            logger.debug("规则 #%s 判不了：%s", rule.id, reason)
            continue

        if verdict:
            claimed = db.execute(
                update(AlertRule)
                .where(AlertRule.id == rule.id, AlertRule.state == "armed")
                .values(state="fired")
                .returning(AlertRule.id)
            ).scalar_one_or_none()
            if claimed is None:
                # 已经被别的进程触发了。不是错误，也不该记一条 alerts。
                logger.debug("规则 #%s 已被其它进程触发，跳过", rule.id)
                continue

            db.add(
                Alert(
                    rule_id=rule.id,
                    triggered_at=when,
                    price_at_trigger=product.price,
                    # ★ 规则快照必须在**触发这一刻**取：规则可被 PATCH 就地改，
                    #   事后 join 拿到的是"现在"的规则，历史记录会跟着变
                    #   （"价格低于 ¥50｜触发价 ¥89.9" 自相矛盾）。
                    rule_type_at_trigger=rule.rule_type,
                    threshold_at_trigger=rule.threshold,
                    # ★ 恒为 False：没有外部推送渠道（不发邮件/webhook），列表都是
                    #   【拉】的；留这列只为将来接推送不用改表结构。
                    notified=False,
                )
            )
            report.fired += 1
            logger.warning(
                "★ 告警触发：规则 #%s（%s %s）｜%s｜当前 %s / 阈值 %s",
                rule.id,
                rule.rule_type,
                rule.threshold,
                product.title,
                product.price,
                rule.threshold,
            )
            continue

        # 条件不成立。只有 fired 的规则需要重新武装。
        if rule.state == "fired":
            rearmed = db.execute(
                update(AlertRule)
                .where(AlertRule.id == rule.id, AlertRule.state == "fired")
                .values(state="armed")
                .returning(AlertRule.id)
            ).scalar_one_or_none()
            if rearmed is not None:
                report.rearmed += 1
                logger.info(
                    "规则 #%s 的价格已回到阈值上方，重新武装（不发通知）", rule.id
                )

    db.commit()

    # ★ 这一行不能省：上面的批量 UPDATE **不会**把新值同步到会话里已有的对象上，
    #   不 expire 的话调用方（尤其是测试）随后读 `rule.state` 会拿到旧值（见 16.10）。
    db.expire_all()
    return report
