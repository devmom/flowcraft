"""OpenAI-compatible Provider Adapter.

Supports: DeepSeek, OpenAI, Azure OpenAI, vLLM, LM Studio, and any /v1/chat/completions API.

HTTP transport: httpx (async, cancellable). Falls back to urllib if httpx is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx

from flowcraft_core.logging_config import get_trace_logger
from flowcraft_core.models.adapters.base import ModelProfile, ProviderAdapter

logger = logging.getLogger(__name__)
trace = get_trace_logger("models.adapter")


class OpenAICompatibleAdapter(ProviderAdapter):
    """OpenAI-compatible API adapter.

    Works with: DeepSeek V4 Pro/Flash (api.deepseek.com), OpenAI, vLLM, LM Studio, etc.
    Default for FlowCraft: DeepSeek V4 Pro (deepseek-v4-pro).

    Uses httpx.AsyncClient for truly cancellable HTTP requests.
    """

    def __init__(
        self,
        profile: ModelProfile,
        api_key: str | None = None,
        timeout_seconds: int = 60,
        max_retries: int = 2,
    ) -> None:
        self.profile = profile
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """Lazy-init httpx client (must be called within an async context)."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=15.0),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key or ''}",
                },
            )
        return self._client

    async def close(self) -> None:
        """Close the httpx client (call on shutdown)."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ── Public API ──────────────────────────────────────────

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        """普通对话，返回文本."""
        t0 = time.monotonic()
        model_id = self.profile.model_id
        last_error: str | None = None

        # Estimate prompt size
        prompt_len = sum(len(str(m.get("content", ""))) for m in messages)

        trace.info(None, "adapter.chat",
                  f"HTTP chat: model={model_id} msgs={len(messages)} prompt_len={prompt_len}",
                  extra={"model": model_id, "messages": len(messages), "prompt_len": prompt_len})

        for retry in range(self._max_retries + 1):
            try:
                payload = self._build_payload(messages, kwargs)
                api_t0 = time.monotonic()
                body, status = await self._api_call(payload)
                api_elapsed = time.monotonic() - api_t0
                if status == 200:
                    content = self._extract_text(body)
                    duration_ms = int((time.monotonic() - t0) * 1000)
                    usage = body.get("usage", {})
                    self._log_call(
                        "completed",
                        usage.get("prompt_tokens", 0),
                        usage.get("completion_tokens", 0),
                        duration_ms,
                    )
                    trace.info(None, "adapter.chat",
                              f"HTTP ok ({api_elapsed:.2f}s): model={model_id} status={status} tokens_in={usage.get('prompt_tokens', 0)} tokens_out={usage.get('completion_tokens', 0)}",
                              extra={"model": model_id, "api_elapsed": api_elapsed, "status": status,
                                     "tokens_in": usage.get("prompt_tokens", 0),
                                     "tokens_out": usage.get("completion_tokens", 0)})
                    return content
                last_error = body.get("error", {}).get("message", f"HTTP {status}")
                trace.warn(None, "adapter.chat",
                          f"HTTP error ({api_elapsed:.2f}s): model={model_id} status={status} error={last_error[:100]}",
                          extra={"model": model_id, "status": status, "error": last_error[:100]})
            except Exception as exc:
                last_error = str(exc)
                trace.error(None, "adapter.chat",
                           f"HTTP exception: model={model_id} error={type(exc).__name__}: {exc}",
                           extra={"model": model_id, "error": str(exc)[:200]})

            if retry < self._max_retries:
                await asyncio.sleep(1.0 * (retry + 1))

        self._log_call("failed", error_message=last_error)
        raise RuntimeError(f"Model call failed after {self._max_retries + 1} attempts: {last_error}")

    async def structured_chat(
        self,
        messages: list[dict[str, str]],
        output_schema: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        """结构化输出。最多 3 次尝试（直接 + 修复 + 简化）."""
        schema_instruction = (
            "\n\nIMPORTANT: Respond ONLY with valid JSON matching this schema. "
            "Do NOT wrap in markdown code fences. Do NOT include any text outside the JSON.\n"
            f"Schema: {json.dumps(output_schema, ensure_ascii=False)}"
        )

        augmented = list(messages)
        if augmented and augmented[-1]["role"] == "user":
            augmented[-1] = {"role": "user", "content": augmented[-1]["content"] + schema_instruction}
        else:
            augmented.insert(0, {"role": "system", "content": "Output only valid JSON matching the described schema."})

        for attempt in range(3):
            raw = await self.chat(augmented, **kwargs)
            parsed = self._parse_json(raw)
            if parsed is not None:
                return parsed
            if attempt < 2:
                augmented.append({
                    "role": "user",
                    "content": "Your last response was not valid JSON. Please output ONLY valid JSON (no markdown fences, no extra text).",
                })

        raise RuntimeError("Failed to get valid structured output after 3 attempts")

    async def test_connection(self) -> dict[str, Any]:
        """测试模型连接."""
        t0 = time.monotonic()
        try:
            await self.chat([{"role": "user", "content": "Reply with exactly the word 'ok'."}])
            latency_ms = int((time.monotonic() - t0) * 1000)
            return {"status": "ok", "message": "Connection successful", "latency_ms": latency_ms}
        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return {"status": "error", "message": str(exc), "latency_ms": latency_ms}

    # ── Internal ────────────────────────────────────────────

    def _build_payload(self, messages: list[dict], kwargs: dict[str, Any]) -> bytes:
        payload: dict[str, Any] = {
            "model": self.profile.model_id,
            "messages": messages,
            "temperature": kwargs.get("temperature", 0.7),
            "max_tokens": kwargs.get("max_tokens", 4096),
        }
        # thinking is only supported by reasoner models (deepseek-v4-pro, deepseek-chat)
        # deepseek-v4-flash and other non-reasoner models will error if this is included
        model = self.profile.model_id
        if model in ("deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"):
            thinking = kwargs.get("thinking", {"type": "disabled"})
            payload["thinking"] = thinking
        if "response_format" in kwargs:
            payload["response_format"] = kwargs["response_format"]
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    @staticmethod
    def _normalize_messages(messages: list[dict]) -> list[dict]:
        """将 system 角色消息转换为 user 角色，以兼容不支持 system 的 API。

        策略：
        1. 收集所有 system 消息内容
        2. 将这些内容前置到第一条 user 消息中
        3. 如果没有任何 user 消息，创建一个 user 消息来承载 system 内容
        4. 移除所有 system 消息
        """
        system_messages = [m for m in messages if m.get("role") == "system"]
        if not system_messages:
            return messages

        # 收集所有 system 消息内容
        system_content = "\n\n".join(
            f"[System Instruction]\n{m['content']}" for m in system_messages
        )

        # 过滤掉 system 消息，保留 user 和 assistant
        filtered = [m for m in messages if m.get("role") != "system"]

        # 将 system 内容合并到第一条 user 消息
        if filtered and filtered[0].get("role") == "user":
            filtered[0] = {
                "role": "user",
                "content": system_content + "\n\n---\n\n" + filtered[0]["content"],
            }
        else:
            # 如果没有 user 消息，创建一个
            filtered.insert(0, {"role": "user", "content": system_content})

        return filtered

    async def _api_call(self, payload: bytes) -> tuple[dict, int]:
        """HTTP POST via httpx (async, cancellable).

        Returns (body_dict, http_status).
        On network/timeout errors, returns ({"error": {"message": ...}}, 0).
        """
        url = self.profile.base_url.rstrip("/") + "/chat/completions"
        model_id = self.profile.model_id

        trace.debug(None, "adapter.api_call",
                   f"HTTP POST (httpx): url={url} model={model_id} payload_size={len(payload)}bytes timeout={self._timeout}s",
                   extra={"url": url, "model": model_id, "payload_size": len(payload), "timeout": self._timeout})

        client = self._get_client()
        try:
            response = await client.post(
                url,
                content=payload,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._api_key or ''}"},
            )
            body = response.json()
            return body, response.status_code
        except httpx.TimeoutException as exc:
            trace.warn(None, "adapter.api_call",
                      f"HTTP timeout: url={url} model={model_id}",
                      extra={"url": url, "error": str(exc)[:200]})
            return {"error": {"message": f"Request timed out after {self._timeout}s: {exc}"}}, 0
        except httpx.HTTPStatusError as exc:
            try:
                body = exc.response.json()
            except Exception:
                body = {"error": {"message": str(exc)}}
            return body, exc.response.status_code
        except (httpx.RequestError, httpx.NetworkError, OSError) as exc:
            trace.error(None, "adapter.api_call",
                       f"HTTP network error: url={url} model={model_id} error={type(exc).__name__}: {exc}",
                       extra={"url": url, "error": str(exc)[:200]})
            return {"error": {"message": str(exc)}}, 0
        except Exception as exc:
            trace.error(None, "adapter.api_call",
                       f"HTTP unexpected error: url={url} model={model_id} error={type(exc).__name__}: {exc}",
                       extra={"url": url, "error": str(exc)[:200]})
            return {"error": {"message": str(exc)}}, 0

    @staticmethod
    def _extract_text(body: dict) -> str:
        choices = body.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content", "")
        return ""

    @staticmethod
    def _parse_json(raw: str) -> dict | None:
        """从模型输出中提取 JSON，支持 code fence 包裹和无包裹."""
        text = raw.strip()
        # Direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Remove code fences
        if text.startswith("```"):
            lines = text.split("\n")
            inner = "\n".join(lines[1:-1]) if len(lines) > 2 else text
            try:
                return json.loads(inner.strip())
            except json.JSONDecodeError:
                pass
        # Find JSON object via regex
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        return None

    def _log_call(
        self,
        status: str = "completed",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        duration_ms: int | None = None,
        error_message: str | None = None,
    ) -> None:
        """记录模型调用到日志（后续接入 model_calls 表）."""
        cost = self.estimate_cost(prompt_tokens, completion_tokens)
        logger.info(
            "model_call provider=%s model=%s status=%s prompt=%d completion=%d dur=%d cost=%.6f error=%s",
            self.profile.provider,
            self.profile.model_id,
            status,
            prompt_tokens,
            completion_tokens,
            duration_ms or 0,
            cost,
            error_message or "",
        )
