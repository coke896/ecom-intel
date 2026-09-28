"""GLM 调用封装：重试、JSON 解析、错误翻译。

================================================================================
为什么用 openai SDK 而不是智谱官方 SDK
================================================================================

智谱现在主推 `zai-sdk`（`zhipuai` 已停滞一年多）。本项目选 `openai` SDK
指向智谱的 OpenAI 兼容端点（`https://open.bigmodel.cn/api/paas/v4`）：

  · 同一套代码改三个环境变量就能切 Ollama / SiliconFlow / 任何兼容服务——
    这是"不被单一供应商锁死"，对求职项目来说是能讲的一点
  · 复用 aiwenda 已经验证过的那条路径，坑（temperature 范围、模型名、
    key 不加前缀）都已经踩过

代价是官方 SDK 的错误码和参数校验更准确。这是取舍，不是"更好的选择"。

================================================================================
两层重试，管的是两件不同的事
================================================================================

**第一层：SDK 自带的重试（`max_retries`）。**
  管的是网络层：429 限流、5xx、连接超时。SDK 内部做指数退避。
  ★ aiwenda 那边这一项是 0（默认），本项目显式打开。
    原因是智谱的免费模型有并发限制，"偶发一次 429 就让整份报告失败"
    在演示现场是很难堪的——而加这一行参数的代价是零。

**第二层：本模块自己的一次重试。**
  管的是"模型返回的东西不是合法 JSON / 不符合 schema"。
  SDK 的重试**完全管不到**这一类：HTTP 200、内容就是一段散文。

  为什么值得重试一次：`temperature=0.3` 意味着同一个 prompt 每次采样
  结果不同。JSON 格式错在温度不为 0 时**确实可能是偶发的**，
  重新采样一次有很大概率就好了。

  为什么**只**重试一次：连续两次都拿不到合法 JSON，
  说明问题在 prompt 或 schema 上，第三次采样大概率还是错的。
  这时候正确的动作是去改 prompt，而不是继续等——
  把所有失败都变成"多试几次"，只会让真正的 prompt 缺陷藏得更久。

================================================================================
本模块不做的事
================================================================================

不碰数据库、不读 settings 之外的配置、不认识 payload 里有什么业务字段。
它只负责"把一段 JSON 发给模型，拿回一段合法的 JSON"。
身份、缓存、落库都是 `reporter.py` 的事。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

from app.config import settings

logger = logging.getLogger(__name__)

# 占位符 / 常见示例 Key（检测到即视为未配置）。
# ★ 沿用 aiwenda 的清单，那几条是真被填进去过的。
_KEY_PLACEHOLDER_MARKS = (
    "在此填入", "your-api-key", "your_api_key", "your api key",
    "your_key_here", "sk-xxx", "changeme", "xxx",
    "example", "请替换", "replace", "todo",
)


class AnalysisError(RuntimeError):
    """分析链路失败。

    单独一个异常类型，是为了让 API 层能把它翻译成一句**能照着做**的话，
    而不是把 openai 的 traceback 直接甩给前端。
    （aiwenda 的经验：把 SDK 的原始报错暴露给用户，
      用户看到的第一个念头是"这程序坏了"，而不是"我该去配 key"。）
    """


def key_configured(api_key: str | None = None) -> bool:
    """GLM key 是否已配置可用。

    ★ 只返回布尔值，**绝不回显 key 本身**。
      这个函数被 `/api/health` 调用，而健康检查接口经常被随手贴进
      issue 或截图里——一旦它把 key 吐出来，就等于泄露了。

    ★ 三个要检查的失败模式：
      · 空 —— `.env` 没填
      · 非 ASCII —— HTTP 头只允许 ASCII 字符，
        从网页上复制 key 时经常会带进不可见的中文标点或全角空格，
        症状是 401 或 `UnicodeEncodeError`，但报错信息指向的是请求头，
        不会告诉你"key 里混了个全角空格"
      · 占位符 —— 有人把 .env.example 里的示例值直接当真的填进去了

    ★ `api_key` 参数存在的理由：`GlmClient` 的构造函数允许显式传一个 key
      覆盖配置，那么**检查的对象必须是"实际会用到的那把 key"**。
      不带这个参数的话，`GlmClient(api_key="真key")` 会被拿去跟 settings 比，
      而 settings 里没配 → 明明传了 key 却报"未配置"。
      这种"检查和实际用的不是同一个东西"是错误处理里最常见的漏洞。
    """
    key = ((settings.glm_api_key if api_key is None else api_key) or "").strip()
    if not key:
        return False
    try:
        key.encode("ascii")
    except UnicodeEncodeError:
        return False
    lowered = key.lower()
    return not any(mark in lowered for mark in _KEY_PLACEHOLDER_MARKS)


@dataclass(frozen=True)
class GlmResult:
    """一次成功的调用结果。"""

    content: dict[str, Any]
    """已经解析并通过 schema 校验的 JSON。"""

    tokens_used: int | None
    """总 token 数。拿不到时为 None（不是 0——0 会被读成"没消耗"）。"""

    raw_text: str
    """模型的原始输出。

    ★ 保留它是为了**排查**：报告内容不对时，第一个要回答的问题是
      "是模型写歪了，还是我们解析歪了"。
      没有原文就只能靠猜，而重新调一次 API 得到的是**另一次采样**，
      根本不能用来复现问题。
    """


class AnalysisClient(Protocol):
    """分析客户端的接口。

    ★ 用 Protocol 而不是基类，是为了让测试里的假客户端
      **不需要继承任何东西**——写一个只有 `analyze` 方法的普通类就行。
      这样测试文件不会因为重构 `GlmClient` 的继承关系而莫名其妙地红掉。
    """

    def analyze(self, payload: dict[str, Any]) -> GlmResult: ...


# ---------------------------------------------------------------------------
# JSON 提取
# ---------------------------------------------------------------------------

# 模型经常把 JSON 包在 markdown 代码块里（```json ... ```），
# 即使 prompt 里明确说了"只输出 JSON"。
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any]:
    """从模型输出里抠出 JSON 对象。

    ★ 三级降级，而不是"直接 json.loads"：

      ① 直接解析 —— 模型听话时的正常路径
      ② 剥掉 markdown 代码块再解析 —— 最常见的不听话形式
      ③ 截取第一个 `{` 到最后一个 `}` —— 前后带解释性文字时

    ★ 为什么要容忍到第三级：这不是"纵容模型乱来"，而是一个成本判断。
      失败一次的代价是重新调一次 API（几秒 + token），
      而这三行解析代码的成本是零。把常见的格式偏差挡在这里，
      比让用户看到"分析失败"要好——**但 schema 校验一步都不能松**
      （见 _validate），因为缺字段是内容问题，不是格式问题。
    """
    text = (text or "").strip()
    if not text:
        raise AnalysisError("模型返回了空内容。")

    candidates: list[str] = [text]

    fenced = _FENCE_RE.search(text)
    if fenced:
        candidates.append(fenced.group(1).strip())

    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])

    last_error: Exception | None = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(parsed, dict):
            return parsed
        last_error = AnalysisError(f"顶层是 {type(parsed).__name__}，需要 JSON 对象。")

    raise AnalysisError(
        f"模型的输出不是合法 JSON（已尝试直接解析、剥代码块、截取花括号三种方式）。"
        f"最后一步的错误：{last_error}\n"
        f"原始输出前 300 字：{text[:300]}"
    )


def _validate(content: dict[str, Any]) -> dict[str, Any]:
    """用 Pydantic 校验报告结构。

    ★ 这一步和"能不能解析成 JSON"是两件事，必须分开：
      解析只管括号配平，校验才管"该有的字段在不在"。
      合在一起写的话，"模型返回了 `{}`"会被当成解析成功，
      然后一路走到落库，最后在渲染报告时 KeyError——
      那时候已经离现场很远了。

    ★ 校验失败会被上层当成"可重试的错误"（见 analyze 里的重试）。
      因为缺字段常常是这一次采样跑偏了，重采样一次往往就有。
    """
    # 局部导入：pydantic 只在需要组装这家模型时用到，
    # 放在模块顶层会让"只想 import AnalysisError"的调用方也背上它。
    from app.analysis.prompts import ReportContent

    try:
        return ReportContent.model_validate(content).model_dump()
    except Exception as exc:  # pydantic.ValidationError
        raise AnalysisError(f"模型返回的 JSON 不符合报告结构：{exc}") from exc


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class GlmClient:
    """走 OpenAI 兼容接口的 GLM 客户端（同步）。

    ★ 为什么是同步的，而不是像 aiwenda 那样用 AsyncOpenAI：

      本项目的调用链是**全同步**的——SQLAlchemy 的 Session 是同步的、
      payload 装配是同步的、缓存读写是同步的。
      如果把中间的网络调用改成 async，就会在链条中间制造一个
      async/sync 边界，而且**换不来任何东西**：
      FastAPI 本来就会把 `def`（非 async）端点丢进线程池执行，
      并发能力一点没少。

      真正需要 async 的场景是"一个进程里并发几百个请求"，
      而本项目一次分析只发一个请求（这是刻意的设计取舍，
      见 docs/可行性评估.md 里"批量分析而非逐商品调用"那条）。

      代价：一次调用最坏占用一个线程池线程 120 秒。
      FastAPI 默认 40 个线程——演示场景下完全够用，
      但如果将来要做成多人同时点分析，这里是第一个要看的地方。
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        max_retries: int = 3,
    ) -> None:
        self.model = model or settings.glm_model
        self.temperature = settings.glm_temperature if temperature is None else temperature
        self.max_tokens = max_tokens or settings.glm_max_tokens
        self._timeout = settings.glm_timeout if timeout is None else timeout
        self._api_key = api_key if api_key is not None else settings.glm_api_key
        self._base_url = base_url or settings.glm_base_url
        self._max_retries = max_retries
        self._client = None  # 惰性创建：没配 key 时连构造都不该发生

    def _get_client(self):
        """惰性构造 SDK 客户端。

        ★ 惰性而不是在 `__init__` 里建：`GlmClient()` 在没配 key 时
          也应该能构造出来（测试里要能断言"没配 key 时会报清楚的错"）。
          在构造函数里就抛异常的话，这个断言写不出来。
        """
        if self._client is None:
            if not key_configured(self._api_key):
                raise AnalysisError(
                    "GLM_API_KEY 未配置或包含非法字符。请在项目根目录的 .env 里"
                    "填入智谱开放平台（open.bigmodel.cn，注册免费）的 API Key，"
                    "然后重启服务。\n"
                    "（.env.example 里有格式说明；修改 .env 后必须重启，"
                    "因为配置是在进程启动时读进内存的。）"
                )
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=self._timeout,
                # ★ 网络层重试：429 / 5xx / 超时。SDK 内部做指数退避。
                max_retries=self._max_retries,
            )
        return self._client

    def analyze(self, payload: dict[str, Any]) -> GlmResult:
        """把 payload 发给模型，拿回通过校验的报告 JSON。"""
        # 这里才 import，避免 prompts ↔ glm_client 在模块加载期互相拉扯
        # （prompts 不 import glm_client，但这个顺序仍然是脆的，
        #   把 import 收在函数里可以让将来加重试策略时不踩这个坑）。
        from app.analysis.prompts import build_messages

        messages = build_messages(payload)
        client = self._get_client()

        last_error: Exception | None = None
        # 两次尝试：第一次正常，第二次针对"格式/结构不对"。
        # 网络层的失败由 SDK 的 max_retries 单独负责，不走这个循环。
        for attempt in (1, 2):
            try:
                response = client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    # ★ 让模型倾向于输出 JSON 对象。
                    #   注意这只是"提示"，不是保证——不同兼容端点的支持程度
                    #   不一样，所以 _extract_json 的三级降级仍然必须有。
                    #   把 response_format 当成保险丝，不是当成开关。
                    response_format={"type": "json_object"},
                )
            except Exception as exc:  # noqa: BLE001 —— 统一翻译成 AnalysisError
                raise AnalysisError(f"调用 GLM 失败：{type(exc).__name__}: {exc}") from exc

            text = response.choices[0].message.content or ""
            try:
                content = _validate(_extract_json(text))
            except AnalysisError as exc:
                last_error = exc
                if attempt == 1:
                    logger.warning("GLM 第一次返回的内容不可用，重试一次：%s", exc)
                    continue
                raise

            usage = getattr(response, "usage", None)
            tokens = getattr(usage, "total_tokens", None) if usage else None

            return GlmResult(content=content, tokens_used=tokens, raw_text=text)

        # 循环内两次都失败时已经在上面 raise 了，正常走不到这里
        raise AnalysisError(f"GLM 连续两次返回的内容都不可用：{last_error}")
