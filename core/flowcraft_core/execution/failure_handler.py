"""Failure Handler — 结构化失败分类与智能回退策略.

失败类型:
    MODEL_ERROR          — 模型调用失败 (网络/配额/超时)
    MODEL_PARSE_ERROR    — 模型输出解析失败 (JSON不合法)
    TOOL_ERROR           — 工具执行失败
    PERMISSION_DENIED    — 权限被拒绝
    USER_REJECTED        — 用户拒绝审批
    INSUFFICIENT_INFO    — 信息不足无法继续
    POLICY_BLOCKED       — 策略拦截
    TIMEOUT              — 超时
    STEP_LIMIT           — 步骤轮数超限
    UNKNOWN              — 未知错误
"""

from __future__ import annotations

import asyncio
import enum
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


class FailureType(enum.StrEnum):
    MODEL_ERROR = "MODEL_ERROR"
    MODEL_PARSE_ERROR = "MODEL_PARSE_ERROR"
    TOOL_ERROR = "TOOL_ERROR"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    USER_REJECTED = "USER_REJECTED"
    INSUFFICIENT_INFO = "INSUFFICIENT_INFO"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    TIMEOUT = "TIMEOUT"
    STEP_LIMIT = "STEP_LIMIT"
    USER_FRUSTRATION = "USER_FRUSTRATION"
    UNKNOWN = "UNKNOWN"


# 回退策略
RETRY_POLICY: dict[FailureType, dict] = {
    FailureType.MODEL_ERROR:       {"max_retries": 1, "base_delay": 2.0, "backoff": 2.0, "terminal": False},
    FailureType.MODEL_PARSE_ERROR: {"max_retries": 2, "base_delay": 0.5, "backoff": 1.0, "terminal": False},
    FailureType.TOOL_ERROR:        {"max_retries": 2, "base_delay": 1.0, "backoff": 1.5, "terminal": False},
    FailureType.PERMISSION_DENIED: {"max_retries": 0, "base_delay": 0,   "backoff": 0,   "terminal": True},
    FailureType.USER_REJECTED:     {"max_retries": 0, "base_delay": 0,   "backoff": 0,   "terminal": True},
    FailureType.INSUFFICIENT_INFO: {"max_retries": 1, "base_delay": 1.0, "backoff": 0,   "terminal": False},
    FailureType.POLICY_BLOCKED:    {"max_retries": 0, "base_delay": 0,   "backoff": 0,   "terminal": True},
    FailureType.TIMEOUT:           {"max_retries": 1, "base_delay": 3.0, "backoff": 0,   "terminal": False},
    FailureType.STEP_LIMIT:        {"max_retries": 0, "base_delay": 0,   "backoff": 0,   "terminal": True},
    FailureType.USER_FRUSTRATION: {"max_retries": 0, "base_delay": 0,   "backoff": 0,   "terminal": False},
    FailureType.UNKNOWN:           {"max_retries": 1, "base_delay": 1.0, "backoff": 0,   "terminal": True},
}


@dataclass
class FailureInfo:
    """结构化的失败信息."""
    failure_type: FailureType
    message: str
    original_exception: Exception | None = None
    context: dict[str, Any] = field(default_factory=dict)
    retry_count: int = 0
    can_retry: bool = True
    can_fallback_model: bool = False
    is_terminal: bool = False

    @property
    def user_message(self) -> str:
        messages = {
            FailureType.MODEL_ERROR:       "模型服务暂时不可用，已尝试重试",
            FailureType.MODEL_PARSE_ERROR: "模型返回格式异常，已尝试修复",
            FailureType.TOOL_ERROR:        f"工具执行失败：{self.message}",
            FailureType.PERMISSION_DENIED: f"权限不足：{self.message}",
            FailureType.USER_REJECTED:     "操作已被您拒绝",
            FailureType.INSUFFICIENT_INFO: f"信息不足：{self.message}",
            FailureType.POLICY_BLOCKED:    f"安全策略已拦截此操作：{self.message}",
            FailureType.TIMEOUT:           "操作超时",
            FailureType.STEP_LIMIT:        "步骤执行轮数超限，任务终止",
            FailureType.USER_FRUSTRATION: "用户对执行结果表达了不满，已记录反馈",
            FailureType.UNKNOWN:           f"未知错误：{self.message}",
        }
        return messages.get(self.failure_type, self.message)


def classify_exception(exc: Exception, context: str = "") -> FailureInfo:
    msg = str(exc).lower()
    if "timeout" in msg or "timed out" in msg:
        return FailureInfo(FailureType.TIMEOUT, str(exc), exc, {"context": context})
    if "permission" in msg or "access denied" in msg or "forbidden" in msg:
        return FailureInfo(FailureType.PERMISSION_DENIED, str(exc), exc, {"context": context})
    if "json" in msg or "parse" in msg or "decode" in msg or "structured output" in msg:
        return FailureInfo(FailureType.MODEL_PARSE_ERROR, str(exc), exc, {"context": context})
    if any(kw in msg for kw in ("api", "rate limit", "quota", "unauthorized", "model", "token")):
        return FailureInfo(FailureType.MODEL_ERROR, str(exc), exc,
                          {"context": context, "can_fallback_model": True})
    if any(kw in msg for kw in ("connection", "network", "refused", "unreachable")):
        return FailureInfo(FailureType.MODEL_ERROR, str(exc), exc,
                          {"context": context, "can_fallback_model": True})
    if "最大工具调用轮数" in str(exc) or "max" in msg:
        return FailureInfo(FailureType.STEP_LIMIT, str(exc), exc, {"context": context})
    return FailureInfo(FailureType.UNKNOWN, str(exc), exc, {"context": context})


async def retry_with_backoff(
    fn: Callable[..., Awaitable[Any]],
    *args: Any,
    failure_info: FailureInfo,
    on_retry: Callable[[FailureInfo], None] | None = None,
    **kwargs: Any,
) -> Any:
    policy = RETRY_POLICY.get(failure_info.failure_type, RETRY_POLICY[FailureType.UNKNOWN])
    max_retries = min(policy["max_retries"], 3)

    last_error = failure_info.original_exception

    for attempt in range(max_retries + 1):
        if attempt > 0:
            delay = policy["base_delay"] * (policy["backoff"] ** (attempt - 1))
            logger.info("Retry %d/%d for %s, delay=%.1fs",
                        attempt, max_retries, failure_info.failure_type.value, delay)
            await asyncio.sleep(delay)

        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            last_error = exc
            new_info = classify_exception(exc, failure_info.context.get("context", ""))
            failure_info.retry_count = attempt + 1
            failure_info.message = str(exc)
            failure_info.original_exception = exc

            if new_info.failure_type in (
                FailureType.PERMISSION_DENIED,
                FailureType.USER_REJECTED,
                FailureType.POLICY_BLOCKED,
            ):
                failure_info.is_terminal = True
                break

            if on_retry:
                on_retry(failure_info)

    if policy["terminal"]:
        failure_info.is_terminal = True
    failure_info.original_exception = last_error
    failure_info.can_retry = False
    raise StepFailedError(failure_info)


class StepFailedError(Exception):
    def __init__(self, failure: FailureInfo | str) -> None:
        if isinstance(failure, FailureInfo):
            self.failure_info = failure
            super().__init__(failure.user_message)
        else:
            self.failure_info = classify_exception(Exception(failure))
            super().__init__(failure)
