"""GLM 调用封装：重试、JSON 解析、错误翻译。

走 openai SDK 指向智谱的 OpenAI 兼容端点（https://open.bigmodel.cn/api/paas/v4）：
改三个环境变量就能切别的兼容服务，不被单一供应商锁死；代价是官方 SDK 的
错误码和参数校验更准。这是取舍，不是"更好的选择"。

两层重试管两件不同的事：SDK 的 `max_retries` 管网络层（429 限流 / 5xx / 超时），
本项目显式打开；本模块自己重试一次，管"HTTP 200 但内容不是合法 JSON / 不合
schema"——temperature=0.3 下后者是偶发的，重采样大概率就好；只试一次，是因为
连续两次都错说明问题在 prompt 或 schema 上，该去改 prompt 而不是继续等。

本模块不碰数据库、不认识业务字段，只负责"把 JSON 发给模型、拿回合法 JSON"。
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
_KEY_PLACEHOLDER_MARKS = (
    "在此填入", "your-api-key", "your_api_key", "your api key",
    "your_key_here", "sk-xxx", "changeme", "xxx",
    "example", "请替换", "replace", "todo",
)


class AnalysisError(RuntimeError):
    """分析链路失败。

    单独一个异常类型，是为了让 API 层能把它翻译成一句**能照着做**的话，
    而不是把 openai 的 traceback 直接甩给前端。
    """


def key_configured(api_key: str | None = None) -> bool:
    """GLM key 是否已配置可用。

    ★ 只返回布尔值，**绝不回显 key 本身**——本函数被 /api/health 调用，
      而健康检查结果经常被随手贴进 issue 或截图，吐 key 就等于泄露。

    三种失败模式：空（.env 没填）；非 ASCII（HTTP 头只允许 ASCII，从网页复制
    key 常带进不可见的中文标点或全角空格，报错却指向请求头、不会说是 key 的
    问题）；占位符（.env.example 里的示例值被当真填了进去）。

    ★ 带 `api_key` 参数是为了检查"实际会用到的那把 key"（可覆盖 settings），
      否则显式传了真 key 也会被拿去跟 settings 比而报"未配置"。
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

    ★ 保留原文是为了**排查**：内容不对时第一个要回答的问题是"是模型写歪了，
      还是我们解析歪了"。重新调一次 API 得到的是**另一次采样**，不能复现问题。
    """


class AnalysisClient(Protocol):
    """分析客户端的接口。

    ★ 用 Protocol 而不是基类：测试里的假客户端不需要继承任何东西，
      写一个只有 `analyze` 方法的普通类就行。
    """

    def analyze(self, payload: dict[str, Any]) -> GlmResult: ...


# ---- JSON 提取 ----

# 模型经常把 JSON 包在 markdown 代码块里（```json ... ```），
# 即使 prompt 里明确说了"只输出 JSON"。
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any]:
    """从模型输出里抠出 JSON 对象。

    ★ 三级降级：① 直接解析；② 剥掉 markdown 代码块；③ 截取第一个 `{` 到
      最后一个 `}`。容忍格式偏差是成本判断（失败一次的代价是重调一次 API），
      但 schema 校验一步都不能松——缺字段是内容问题，不是格式问题（见 _validate）。
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

    ★ 和"能不能解析成 JSON"是两件事，必须分开：合在一起写的话，"模型返回了
      `{}`"会被当成解析成功，一路走到渲染报告时才 KeyError——那时离现场很远。
    ★ 校验失败会被上层当成"可重试的错误"：缺字段常常是这次采样跑偏了。
    """
    # 局部导入：pydantic 只在需要组装这家模型时用到，
    # 不必让"只想 import AnalysisError"的调用方也背上它。
    from app.analysis.prompts import ReportContent

    try:
        return ReportContent.model_validate(content).model_dump()
    except Exception as exc:  # pydantic.ValidationError
        raise AnalysisError(f"模型返回的 JSON 不符合报告结构：{exc}") from exc


# ---- 客户端 ----


class GlmClient:
    """走 OpenAI 兼容接口的 GLM 客户端（同步）。

    ★ 同步而不是 AsyncOpenAI：本项目调用链全同步（Session、payload、缓存读写），
      把中间的网络调用改成 async 不换来任何东西——FastAPI 本来就会把 `def`
      端点丢进线程池执行，并发能力一点不少。真正需要 async 的是"一个进程里
      并发几百个请求"，而本项目一次分析只发一个请求（刻意的设计取舍）。
      代价：一次调用最坏占用一个线程池线程 120 秒，将来要做多人同时点分析，
      这里是第一个要看的地方。
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

        ★ 惰性而不是在 `__init__` 里建：`GlmClient()` 在没配 key 时也应该能
          构造出来（测试要能断言"没配 key 时会报清楚的错"）。
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
        # 函数内 import，避免 prompts ↔ glm_client 在模块加载期互相拉扯。
        from app.analysis.prompts import build_messages

        messages = build_messages(payload)
        client = self._get_client()

        last_error: Exception | None = None
        # 两次尝试：第二次专门针对"格式/结构不对"；网络层的失败由 SDK 的
        # max_retries 单独负责，不走这个循环。
        for attempt in (1, 2):
            try:
                response = client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    # ★ 让模型倾向于输出 JSON 对象。它只是"提示"，不是保证——
                    #   不同兼容端点的支持程度不一样，所以 _extract_json 的
                    #   三级降级仍然必须有。把 response_format 当保险丝，不是开关。
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
