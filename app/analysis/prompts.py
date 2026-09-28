"""Prompt 模板 + 报告的 JSON schema。

================================================================================
★ 改了本文件的任何模板字符串，必须同步改 fingerprint.PROMPT_VERSION
================================================================================

这是整个缓存机制里唯一需要人肉维护的地方，所以放在最显眼的位置。
漏改的后果：改了 prompt，但指纹没变 → 命中旧缓存 → 拿到的还是旧报告，
而你会去怀疑热重载、怀疑没保存文件、怀疑浏览器缓存。

（为什么不把模板内容直接算进指纹，见 fingerprint.py 里的说明：
  那样改一个错别字就会让全部缓存作废，而调 prompt 的日常就是改几十个错别字。）

================================================================================
这个 prompt 在防什么
================================================================================

一份"AI 分析报告"最常见的三种失败，都能靠 prompt 里的硬规则挡掉大半：

**① 编造数字。** 模型很擅长写出"该品类均价约 200 元"这种句子，
   而数据里根本没有这个数。对策是强制要求每条结论都带 `evidence` 字段，
   且 evidence 里必须是**数据里出现过的数字**。

**② 拿样本的数字说整体。** 这是本项目特有的风险，因为
   prompt 里只放了 60 个商品（见 payload.py），而 summary 块里是全部 1000 个的
   统计量。模型完全可能把样本的均价说成整体均价——
   **而它是一个由采样方式制造出来的假结论**。
   对策有两层：payload 里的 `sample.note` 明说这是样本，
   system prompt 里再明令禁止。

**③ 对着空数据硬编。** 没有销量数据时（真实电商的常态），
   模型可能编出"销量最高的商品是……"。对策是把 has_sales
   明明白白地写在数据里，并要求它在数据不足时写进 data_limits。

================================================================================
为什么输出 JSON 而不是 Markdown
================================================================================

aiwenda 那边的输出是 Markdown（给人读的）。这里必须改成 JSON，
因为报告要被**缓存和复用**，而缓存的对象需要是结构化的：

  · 可以校验（缺字段当场报错，而不是渲染时才发现）
  · 可以测（断言"报告里有 3~5 个 section"，Markdown 做不到）
  · 可以稳定序列化（存进 content_json 列，读出来还是同一个结构）
  · 前端可以逐块渲染，而不是把一坨 HTML 塞进 innerHTML

代价是 token 多一些（花括号和引号），以及模型偶尔会不听话。
后者由 glm_client 的三级 JSON 提取 + 一次重试兜住。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# 分析维度。这些是**建议**的段落标题，不是强制的——
# 模型可以按数据情况合并或改名（schema 只约束"有 3~5 段"）。
SECTION_HINTS: tuple[str, ...] = (
    "价格结构",
    "品牌与店铺格局",
    "标题关键词反映的卖点",
    "销量与爆款（如果没有销量数据，直接说明并跳过）",
)

SYSTEM_PROMPT = """你是一名电商竞品分析顾问，为一家做数码配件的公司分析市场数据。

你会收到一份 JSON 数据，包含：
  · summary           —— **全部商品**的统计量（商品数、价格的 min/p25/中位数/p75/max/均值、
                          品牌分布、销量汇总）。这是唯一能代表整体的数据。
  · price_buckets     —— **全部商品**的价格直方图
  · shops             —— 店铺维度的商品数与均价
  · keywords          —— 商品标题的高频词及其出现次数
  · sample + top_products —— **一部分**商品（样本），以及它是怎么被选出来的
  · source            —— 数据来源标识（mock / smzdm / all）

请输出一个 JSON 对象，结构如下：

{
  "headline": "一句话结论，不超过 60 字",
  "sections": [
    {"title": "段落标题", "finding": "你的判断，2~4 句", "evidence": "支撑它的具体数字"}
  ],
  "opportunities": ["可执行的机会点，每条一句话"],
  "risks": ["风险或需要注意的地方"],
  "data_limits": ["这批数据支撑不了哪些结论"]
}

必须遵守的规则：

1. **只使用数据里出现过的数字。** 禁止估算、禁止外推、禁止凭常识补数字。
   如果你想说"价格集中在 100-150 元"，那个区间和商品数必须能在
   price_buckets 里找到。

2. **区分整体和样本。** summary / price_buckets / shops / keywords 描述整体，
   可以用来说"整体如何"。top_products 只是样本，
   **绝不能用它里面的均值或占比去代表整体**——
   它是按 popularity 挑出来的，本身就偏向头部。
   样本的用途是"举例子"，不是"算比例"。

3. **没有的证据就不要有结论。** 如果 summary.has_sales 是 false，
   说明这批数据没有销量字段。此时：
     · 不要写任何关于销量、爆款、畅销程度的判断
     · 在 data_limits 里明确写出"数据缺少销量，无法判断畅销程度"
   同理，brand_missing_count 高时不要谈品牌格局。

4. **宁可说"看不出来"，也不要凑一段。** 一个诚实的
   "这批数据不足以判断 X"比一段编出来的分析有价值得多。
   data_limits 里至少写一条。

5. **sections 写 3~5 段。** 建议覆盖：{sections}
   依据数据情况可以合并、改名或跳过——跳过的要说明原因。

6. **只输出 JSON，不要有任何其他文字。** 不要用 markdown 代码块包裹，
   不要写"好的，以下是分析"这类开场白。

语气要求：像一个懂行的人在给同事讲数据，直接说结论和依据。
不要用"我们可以看到""综上所述""值得注意的是"这类填充语。
不要用感叹号。""".replace("{sections}", "、".join(SECTION_HINTS))


class ReportSection(BaseModel):
    """报告里的一段。"""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=40)
    finding: str = Field(min_length=1)
    # ★ evidence 允许为空字符串，但**不允许缺失**。
    #
    #   这个区别是有意的：把 evidence 设成必填，等于每次都逼模型
    #   给出一条依据（哪怕是空泛的"见 price_buckets"）；
    #   但如果设成必填且非空，模型在确实没有数字可引时
    #   会**编一个数字**来填满这个字段——那是比留空糟糕得多的结果。
    #   所以：字段必须在（结构上提醒它这是要求），内容可以为空（给它退路）。
    evidence: str = ""


class ReportContent(BaseModel):
    """报告的完整结构。

    ★ 校验策略：核心字段严格，边缘字段宽松。

      · headline / sections 严格 —— 它们是报告的骨架，缺了就没法渲染
      · opportunities / risks / data_limits 给默认空列表 ——
        强迫模型在"确实没有风险"时也要硬凑三条，只会得到三条废话
      · `extra="ignore"` —— 模型多返回一个字段不该让整份报告失败。
        它会自己加 `"conclusion"` 或 `"summary"` 这类字段，
        这是模型的常见行为，不是错误。丢掉就好。

    ★ 为什么每段都要有 title：渲染时前端按 title 折叠/展开。
      没有 title 就只能给每段编号，而编号在用户眼里没有信息量。
    """

    model_config = ConfigDict(extra="ignore")

    headline: str = Field(min_length=1, max_length=200)
    # 3~5 段是 prompt 里要求的，这里把上下限也写进 schema。
    # ★ 注意 min_length=1：**不能要求 min_length=3**。
    #   真实数据稀少时（比如只有一个商品），模型只写得出 1 段，
    #   这时卡在 3 会让整次分析失败——而"数据少所以只有一段"
    #   是完全合理的结果。下限留给 prompt 去引导，schema 只管"别是空的"。
    sections: list[ReportSection] = Field(min_length=1, max_length=8)
    opportunities: list[str] = Field(default_factory=list, max_length=8)
    risks: list[str] = Field(default_factory=list, max_length=8)
    data_limits: list[str] = Field(default_factory=list, max_length=8)


def build_user_prompt(payload: dict[str, Any]) -> str:
    """把 payload 序列化成用户消息。

    ★ 紧凑 JSON（`separators`）而不是缩进 JSON：
      缩进会让每个字段多出几个空格和一个换行，60 个商品下来就是
      一两千个 token 的纯空白。而这些 token 对模型理解结构**没有帮助**——
      它是 JSON，结构已经由括号表达了。
      （人读的部分由 prompt 的说明和字段名承担，不靠缩进。）

    ★ `ensure_ascii=False`：中文原样输出。
      设成 True 的话每个中文字会变成 `\\uXXXX` 六个字符，
      不只是变长——模型看到转义序列后的理解也更差。
    """
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def build_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    """组装完整的消息列表。"""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(payload)},
    ]
