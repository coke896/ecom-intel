"""Prompt 模板 + 报告的 JSON schema。

★ 改了本文件的任何模板字符串，必须同步改 fingerprint.PROMPT_VERSION——
漏改的后果是"改了 prompt 却命中旧缓存"，而人会去怀疑热重载、怀疑没保存文件。
（不把模板本身算进指纹：那样改一个错别字就作废全部缓存。）

prompt 的硬规则针对三种常见失败：编造数据里没有的数字；拿 60 个样本的数字说
整体（summary 才是全体统计量）；没有销量/品牌数据时硬编——对策是强制 evidence
字段、payload 的 sample.note 与 system prompt 双层约束、data_limits 兜底。
输出 JSON 而非 Markdown：报告要缓存复用，结构化才能校验、测试、稳定序列化。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# 分析维度。这些是**建议**的段落标题，不是强制的——schema 只约束"有 3~5 段"。
SECTION_HINTS: tuple[str, ...] = (
    "价格结构",
    "品牌与店铺格局",
    "标题关键词反映的卖点",
    "销量与爆款（如果没有销量数据，直接说明并跳过）",
)

SYSTEM_PROMPT = """你是一名电商竞品分析顾问，为一家做数码配件的公司分析市场数据。

你会收到一份 JSON 数据，包含：
  · summary           —— **全部商品**的统计量（商品数、价格的 min/p25/中位数/p75/max/均值、
                          品牌分布、销量汇总，以及各字段缺失的商品数
                          price_missing_count / brand_missing_count）。
                          这是唯一能代表整体的数据。
  · price_buckets     —— 价格的直方图。它覆盖的是**有价格**的那些商品：
                          没有价格的商品进不了任何区间，数量见 price_missing_count
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
   price_missing_count 是**因为没有价格、所以不在 price_buckets 里**的商品数——
   凡是拿 price_buckets 算占比，分母用"有价格的商品数"，不要用 product_count。

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
    # ★ evidence 允许为空字符串，但**不允许缺失**：字段必填是结构上提醒它
    #   这是要求；若设成必填且非空，模型在确实没有数字可引时会**编一个数字**
    #   来填满字段——那比留空糟糕得多。所以字段必须在、内容可以空。
    evidence: str = ""


class ReportContent(BaseModel):
    """报告的完整结构。

    ★ 校验策略：核心字段（headline / sections）严格，它们是骨架；边缘字段
      （opportunities / risks / data_limits）给默认空列表，强迫模型硬凑只会得到废话；
      `extra="ignore"` 容忍模型自行加 "conclusion" 这类字段（常见行为，不是错误）。
    ★ 每段必须有 title：前端按 title 折叠/展开，编号对用户没有信息量。
    """

    model_config = ConfigDict(extra="ignore")

    headline: str = Field(min_length=1, max_length=200)
    # 3~5 段是 prompt 里要求的，这里把上下限也写进 schema。
    # ★ 但不能要求 min_length=3：数据稀少时（比如只有一个商品）模型只写得出 1 段，
    #   卡在 3 会让整次分析失败——下限留给 prompt 引导，schema 只管"别是空的"。
    sections: list[ReportSection] = Field(min_length=1, max_length=8)
    opportunities: list[str] = Field(default_factory=list, max_length=8)
    risks: list[str] = Field(default_factory=list, max_length=8)
    data_limits: list[str] = Field(default_factory=list, max_length=8)


def build_user_prompt(payload: dict[str, Any]) -> str:
    """把 payload 序列化成用户消息。

    ★ 紧凑 JSON（separators）而不是缩进 JSON：缩进对模型理解结构没有帮助，
      60 个商品下来就是一两千个 token 的纯空白。
    ★ `ensure_ascii=False`：中文原样输出，转成转义序列不只变长、理解也更差。
    """
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def build_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    """组装完整的消息列表。"""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(payload)},
    ]
