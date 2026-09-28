"""告警检查 —— 三态状态机（armed / fired）。

================================================================================
为什么必须有状态：没有状态的告警会自己把自己淹掉
================================================================================
最直觉的写法是"价格 < 阈值 → 写一条 alerts"。它在**价格持续低于阈值**期间
每检查一次就触发一次：

    5 分钟查一次 → 一天 288 次 → 288 条记录、288 条通知

而"这个商品跌破 200 元"这件事**只发生了一次**——第一次跌破才是事件，
后面每一次检查看到的都只是"它还在下面"。没有状态的告警把**一个状态**当成了
**282 个事件**，用户收到的通知会从"值得看一眼"变成"垃圾"，于是没人再看。

状态机（`alert_rules.state`）：

    armed + 条件成立   → 触发：写 alerts、state ← fired
    fired + 条件成立   → 跳过（同一个事件，不重复记录）
    fired + 条件不成立 → 重新武装：state ← armed（★ 不发通知）
    armed + 条件不成立 → 什么也不做

★ 为什么重新武装【不发通知】：
  "价格涨回阈值上方了"不是一个值得推送的事件。发了的话，一次价格波动
  会产生两条通知（跌破一条、涨回一条），而涨回那一条对用户没有任何
  可执行的信息——他既不需要买，也不需要卖。
  代价是"已经重新武装"这件事只能从 `state` 字段看出来，界面上要显示它。

★ 为什么重新武装【必须存在】（不能"触发一次就永久熄火"）：
     跌到 199 → 触发 → 涨回 250 → 又跌到 180
  第二次跌破是一个**新的**价格事件，用户希望被通知。
  永久熄火的话第二条就漏了，而且漏得悄无声息——用户以为"规则还在看着"。

================================================================================
两种规则，以及 price_drop_pct 的基准从哪来
================================================================================
    price_below      当前价 < threshold
    price_drop_pct   相对【历史最高价】的跌幅 ≥ threshold（单位：百分比）

★ price_drop_pct 的基准必须从 price_history 现算（MAX 聚合），
  **不能用 products.price_max**。

  那一列的语义是"**本次爆料的价格区间上限**"（见 app/db/models.py 的说明：
  `券后199-299` → 199/299），不是"这只商品历史上最贵卖过多少"。

  在 Mock 数据上两者数值接近（相差不大），会让人以为用错了也没关系；
  而在真实数据上 `price_max` 恰好等于当前价（每条爆料都是一个单值区间），
  于是"跌幅"永远是 0、告警永远不触发——**而且它不报错**。
  这与 models.py 里"历史最高/最低价一律从 price_history 现算"是同一条规矩。

================================================================================
★ 为什么本文件在 scraper/ 下，但它其实不采集
================================================================================
它读数据库、写数据库，和"采集"没关系。放在这里是因为**它和采集共用
同一个进程和同一个轮询节奏**（`python -m scraper.worker` 每 5 分钟叫它一次）。

它绝不能放进 `app/`（Web 层）的原因：Web 层是多 worker 的（uvicorn 可以起多个），
而告警的"检查—触发—推送"是一个**有状态的动作**，跑在多个进程里会互相踩。
放在采集进程里，它天然是单点的。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import Alert, AlertRule, PriceHistory, Product

logger = logging.getLogger("scraper.alerts")


# 规则类型的合法取值。★ 写成元组而不是散落在 if/elif 里，
# 是为了让"有哪些规则类型"成为一个可被 import、可被测试、可被接口校验的**数据**。
# 加一种新类型时，漏改某处的可能性从"看运气"变成"看这个元组"。
RULE_PRICE_BELOW = "price_below"
RULE_PRICE_DROP_PCT = "price_drop_pct"
RULE_TYPES = (RULE_PRICE_BELOW, RULE_PRICE_DROP_PCT)


@dataclass(slots=True)
class AlertReport:
    """一轮告警检查的结果。

    ★ 和 CrawlReport 一样，数字必须来自**真实发生了什么**，
      而不是"循环跑了几次"。区别在于：这里有一个既不是成功
      也不是失败的状态——`undecidable`（判不了）。

      把它单独计数，是因为它和"没触发"在界面上长得一样（都没写 alerts），
      但含义完全相反：
        没触发   → 规则在正常工作，价格还没到
        判不了   → 规则**根本没工作**（商品没有价格 / 没有基准价 / 规则类型不认识）
      合成一个的话，"我的告警怎么一直没响"会被答成"价格还没到"，
      而真相是那条规则从来没有被真正执行过。
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

    ★ 纯函数，不碰数据库。这样"条件判断"可以被单独测，
      不必为每一种阈值构造一份数据——收集数据是调用方的事。
    """
    if rule_type == RULE_PRICE_BELOW:
        return price < threshold

    if rule_type == RULE_PRICE_DROP_PCT:
        # 没有基准价就判不了。★ 不能把"基准缺失"当成"跌幅 0"——
        # 那会让一条从没算过的规则看起来"检查过了、没到阈值"。
        if baseline is None or baseline <= 0:
            return None
        return (baseline - price) / baseline * 100.0 >= threshold

    # 不认识的类型（比如有人手工往库里写了一行，或者将来加了新类型
    # 但这里忘了改）。★ 返回 None 而不是 False：
    # 返回 False 的意思是"检查过了，条件不成立"，而这是句假话。
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
    """一次查出所有商品的历史最高价。

    ★ 为什么要一次查完，而不是在循环里逐个查：
      逐个查是 N+1 查询的经典形态。规则数少的时候看不出问题，
      而"少"是会变的——真实部署里告警规则是用户一条条加的。

    ★ MAX(price) 而不是 MAX(price_max)：见模块顶部——
      price_max 是"本次爆料的区间上限"，不是历史最高价。
    """
    if not product_ids:
        return {}
    rows = db.execute(
        select(PriceHistory.product_id, func.max(PriceHistory.price))
        .where(PriceHistory.product_id.in_(product_ids))
        .group_by(PriceHistory.product_id)
    ).all()
    # ★ float() 转换：SQLite 的 MAX 在空表/全 NULL 时返回 None，
    #   而这里 group_by 之后不会出现 None 行——但显式转换让类型是确定的，
    #   不必让调用方去猜"这个值会不会是 Decimal 或字符串"。
    return {int(pid): float(mx) for pid, mx in rows if mx is not None}


def check_alerts(db: Session, *, now: datetime | None = None) -> AlertReport:
    """检查所有启用中的告警规则，推进状态机。返回本轮统计。

    ★ 原子性的实现方式：**状态转换本身是带条件的 UPDATE**，
      而不是"先读 state 再判断要不要写"。

          UPDATE alert_rules SET state='fired' WHERE id=? AND state='armed'
          → 有行返回 = 我抢到了这次触发

      这和 `claim_next_task()` 是同一条理由（见 scraper/worker.py）：
      "先查后改"在并发下会**双写**——两个进程同时读到 state='armed'，
      于是同一个事件写了两条 alerts、推了两条通知。
      带条件的 UPDATE 把判断和修改变成了一个原子动作，谁先到谁赢，
      后到的 rowcount=0，直接跳过。

      ★ 也就是说：**WHERE 子句才是权威，上面读到的 `rule.state` 只是快路径。**
        这个区别很实在——它就是本项目踩过的那个坑的正向用法
        （见 docs/可行性评估.md 16.10：批量 UPDATE 不会同步会话里的对象，
         所以"读到的值"和"库里的值"可能不一致）。

    ★ 一次 commit：状态转换和 Alert 行在**同一个事务**里，
      所以不会出现"标成了 fired 但没写记录"或反过来的半截状态。
      崩在中途则整体回滚——规则仍是 armed，下一轮重新判，不会漏报。
    """
    when = now or utcnow()

    # 一次查出「规则 + 商品」，因为价格和标题都要用。
    # ★ join 而不是给 AlertRule 加一条 lazy relationship：
    #   懒加载会在循环里触发 N+1 查询（和 _baselines 里那条理由一样）。
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
                    # ★ 规则快照。必须在**触发这一刻**取，不能等以后去 join 规则表——
                    #   规则是可以改的（PATCH 就地改 threshold / rule_type，
                    #   不会动 alerts 表），事后 join 拿到的是"现在"的规则，
                    #   于是历史记录会跟着变，甚至会变成自相矛盾的一句话
                    #   （"价格低于 ¥50 ｜ 触发价 ¥89.9"，而 89.9 < 50 是假）。
                    #   实测过，见 models.Alert 上那段注释。
                    rule_type_at_trigger=rule.rule_type,
                    threshold_at_trigger=rule.threshold,
                    # ★ 恒为 False：本项目**没有外部推送渠道**
                    #   （不发邮件、不发 webhook）。日志和界面上的列表
                    #   都是【拉】的，不是【推】的——用户打开页面才看得到。
                    #   留这一列是为了将来接推送时不用改表结构，
                    #   但它现在恒为 False，这件事必须说出来：
                    #   不说的话，看数据的人会以为"通知推送失败了"。
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

    # ★ 这一行不能省，理由和 worker.py 里那两处一样（见 16.10）：
    #   上面用的是批量 UPDATE，它**不会**把新值同步到会话里已有的对象上。
    #   不 expire 的话，调用方（尤其是测试）随后读 `rule.state` 会拿到旧值——
    #   "库改了、读的人不知道"，而这次改的恰好是状态机的状态。
    db.expire_all()
    return report
