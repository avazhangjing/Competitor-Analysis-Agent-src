import asyncio
import json
import logging
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI

from ..config import get_settings

logger = logging.getLogger(__name__)

LLM_RETRY_DELAYS = [2, 4]
# 单次 LLM 调用超时（防止服务端挂起拖垮整个分析）
LLM_CALL_TIMEOUT_SECONDS = 150
# 含重试在内的整体超时预算
LLM_OVERALL_TIMEOUT_SECONDS = 300

_llm_instance: ChatOpenAI | None = None


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


@dataclass
class LLMResult:
    """LLM 调用结果，包含响应内容和 token 用量。"""
    content: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    elapsed_ms: int = 0


# token 累加器：按任务（会话）隔离，避免并发分析互相污染统计
# 每个 asyncio.Task 持有独立 context，同一图节点内 gather 的子协程共享同一 context
_token_accumulator_var: ContextVar[TokenUsage] = ContextVar("token_accumulator", default=TokenUsage())


def reset_token_tracker() -> None:
    _token_accumulator_var.set(TokenUsage())


def get_accumulated_tokens() -> TokenUsage:
    return _token_accumulator_var.get()


def get_llm() -> ChatOpenAI | None:
    global _llm_instance
    settings = get_settings()
    if not settings.llm_api_key:
        logger.warning("LLM API key not configured")
        return None
    if _llm_instance is None:
        _llm_instance = ChatOpenAI(
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            temperature=0.2,
            timeout=LLM_CALL_TIMEOUT_SECONDS,
        )
    return _llm_instance


def _extract_token_usage(response: Any) -> TokenUsage:
    """从 LangChain AIMessage 中提取 token 用量。"""
    # 优先从 usage_metadata 获取
    usage_meta = getattr(response, "usage_metadata", None)
    if usage_meta and isinstance(usage_meta, dict):
        return TokenUsage(
            input_tokens=usage_meta.get("input_tokens", 0),
            output_tokens=usage_meta.get("output_tokens", 0),
            total_tokens=usage_meta.get("total_tokens", 0),
        )
    # 其次从 response_metadata 获取
    resp_meta = getattr(response, "response_metadata", None)
    if resp_meta and isinstance(resp_meta, dict):
        token_usage = resp_meta.get("token_usage", {})
        if token_usage:
            return TokenUsage(
                input_tokens=token_usage.get("prompt_tokens", 0),
                output_tokens=token_usage.get("completion_tokens", 0),
                total_tokens=token_usage.get("total_tokens", 0),
            )
    return TokenUsage()


async def ainvoke_with_retry(llm: ChatOpenAI, messages: list[BaseMessage]) -> Any:
    """调用 LLM，失败时最多重试 len(LLM_RETRY_DELAYS) 次。

    带两级超时保护：
    - 单次调用超时 LLM_CALL_TIMEOUT_SECONDS（wait_for 取消挂起的请求）；
    - 整体预算 LLM_OVERALL_TIMEOUT_SECONDS（含重试），超预算直接放弃。
    """
    last_exc: Exception | None = None
    deadline = time.monotonic() + LLM_OVERALL_TIMEOUT_SECONDS
    for attempt in range(len(LLM_RETRY_DELAYS) + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        timeout = min(LLM_CALL_TIMEOUT_SECONDS, remaining)
        try:
            response = await asyncio.wait_for(llm.ainvoke(messages), timeout=timeout)
            acc = _token_accumulator_var.get()
            _token_accumulator_var.set(acc + _extract_token_usage(response))
            return response
        except asyncio.TimeoutError:
            last_exc = TimeoutError(f"LLM 调用超时（>{int(timeout)}s）")
            logger.warning("LLM call timed out after %ds (attempt %d/%d)",
                           int(timeout), attempt + 1, len(LLM_RETRY_DELAYS) + 1)
        except Exception as exc:
            last_exc = exc
            logger.warning("LLM call failed (attempt %d/%d): %s",
                           attempt + 1, len(LLM_RETRY_DELAYS) + 1, exc)
        if attempt < len(LLM_RETRY_DELAYS):
            delay = min(LLM_RETRY_DELAYS[attempt], max(0, deadline - time.monotonic()))
            await asyncio.sleep(delay)
    if last_exc is None:
        last_exc = TimeoutError(f"LLM 调用整体超时（>{LLM_OVERALL_TIMEOUT_SECONDS}s）")
    raise last_exc


async def ainvoke_tracked(llm: ChatOpenAI, messages: list[BaseMessage]) -> LLMResult:
    """调用 LLM 并追踪 token 用量和耗时。"""
    start = time.perf_counter()
    response = await ainvoke_with_retry(llm, messages)
    elapsed_ms = round((time.perf_counter() - start) * 1000)
    usage = _extract_token_usage(response)
    content = response.content if hasattr(response, "content") else str(response)
    return LLMResult(content=content, usage=usage, elapsed_ms=elapsed_ms)


def parse_json_object(text: str) -> dict[str, Any]:
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    payload = fenced.group(1) if fenced else text
    start = payload.find("{")
    if start == -1:
        logger.warning("No JSON object found in LLM response (first 200 chars): %s", payload[:200])
        return {}
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(payload)):
        ch = payload[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(payload[start : i + 1])
                except json.JSONDecodeError:
                    logger.warning("Failed to parse JSON from LLM response (first 200 chars): %s", payload[:200])
                    return {}
    logger.warning("Unbalanced JSON braces in LLM response (first 200 chars): %s", payload[:200])
    return {}
