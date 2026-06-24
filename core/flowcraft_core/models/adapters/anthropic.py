"""Anthropic (Claude) Provider Adapter — Claude Opus 4 / Sonnet 4 / Haiku 3.5."""
from __future__ import annotations
import asyncio, json, logging, re, time
from typing import Any
import httpx
from flowcraft_core.models.adapters.base import ModelProfile, ProviderAdapter

logger = logging.getLogger(__name__)
ANTHROPIC_API_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"

CLAUDE_OPUS_4_PROFILE = ModelProfile(model_id="claude-opus-4-20250514", provider="anthropic", display_name="Claude Opus 4", base_url=ANTHROPIC_API_BASE, context_window=200000, cost_input_per_1k=0.015, cost_output_per_1k=0.075)
CLAUDE_SONNET_4_PROFILE = ModelProfile(model_id="claude-sonnet-4-20250514", provider="anthropic", display_name="Claude Sonnet 4", base_url=ANTHROPIC_API_BASE, context_window=200000, cost_input_per_1k=0.003, cost_output_per_1k=0.015)
CLAUDE_HAIKU_3_5_PROFILE = ModelProfile(model_id="claude-3-5-haiku-20241022", provider="anthropic", display_name="Claude Haiku 3.5", base_url=ANTHROPIC_API_BASE, context_window=200000, cost_input_per_1k=0.0008, cost_output_per_1k=0.004)
ANTHROPIC_PROFILES = {p.model_id: p for p in [CLAUDE_OPUS_4_PROFILE, CLAUDE_SONNET_4_PROFILE, CLAUDE_HAIKU_3_5_PROFILE]}

def is_anthropic_model(model_id: str) -> bool:
    return model_id in ANTHROPIC_PROFILES or model_id.startswith("claude-")

class AnthropicAdapter(ProviderAdapter):
    def __init__(self, profile: ModelProfile, api_key: str) -> None:
        self.profile = profile
        self._api_key = api_key
        self._max_retries = 3
        self._retry_delay = 2.0

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        system, user_msgs = self._split_system(messages)
        body = {"model": self.profile.model_id, "max_tokens": kwargs.get("max_tokens", 4096), "messages": user_msgs}
        if system: body["system"] = system
        if "temperature" in kwargs: body["temperature"] = kwargs["temperature"]
        result = await self._call("/messages", body)
        return "\n".join(b.get("text", "") for b in result.get("content", []) if b.get("type") == "text")

    async def structured_chat(self, messages: list[dict[str, str]], output_schema: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        system, user_msgs = self._split_system(messages)
        schema_json = json.dumps(output_schema, ensure_ascii=False)
        effective_system = (system or "") + f"\n\nYou MUST respond with valid JSON matching this schema. Output ONLY the JSON object, no other text.\nSchema: {schema_json}"
        body = {"model": self.profile.model_id, "max_tokens": kwargs.get("max_tokens", 4096), "messages": user_msgs, "system": effective_system}
        if "temperature" in kwargs: body["temperature"] = kwargs["temperature"]
        for attempt in range(3):
            result = await self._call("/messages", body)
            text = "\n".join(b.get("text", "") for b in result.get("content", []) if b.get("type") == "text")
            parsed = self._extract_json(text)
            if parsed is not None: return parsed
            body["system"] = effective_system + f"\n\nPrevious response was not valid JSON. Respond with ONLY the JSON object."
        raise ValueError(f"Anthropic structured output failed after 3 attempts")

    async def test_connection(self) -> dict[str, Any]:
        t0 = time.monotonic()
        try:
            await self._call("/messages", {"model": self.profile.model_id, "max_tokens": 10, "messages": [{"role": "user", "content": "Hi"}]})
            return {"status": "connected", "model": self.profile.model_id, "provider": "anthropic", "latency_ms": int((time.monotonic() - t0) * 1000)}
        except Exception as exc:
            return {"status": "error", "model": self.profile.model_id, "provider": "anthropic", "error": str(exc)[:200]}

    async def _call(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.profile.base_url}{path}"
        headers = {"x-api-key": self._api_key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
        last_error = None
        for attempt in range(self._max_retries):
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.post(url, json=body, headers=headers)
                    if resp.status_code == 200:
                        data = resp.json()
                        usage = data.get("usage", {})
                        cost = self.estimate_cost(usage.get("input_tokens", 0), usage.get("output_tokens", 0))
                        logger.info("model_call provider=anthropic model=%s prompt=%d completion=%d cost=$%.6f", body.get("model", ""), usage.get("input_tokens", 0), usage.get("output_tokens", 0), cost)
                        return data
                    if resp.status_code == 429:
                        await asyncio.sleep(self._retry_delay * (2 ** attempt))
                        continue
                    raise httpx.HTTPStatusError(f"Anthropic {resp.status_code}: {resp.text[:300]}", request=resp.request, response=resp)
            except httpx.HTTPStatusError: raise
            except Exception as exc:
                last_error = exc
                if attempt < self._max_retries - 1: await asyncio.sleep(self._retry_delay * (2 ** attempt))
        raise RuntimeError(f"Anthropic failed after {self._max_retries} attempts: {last_error}")

    @staticmethod
    def _split_system(messages: list[dict[str, str]]) -> tuple[str, list[dict[str, Any]]]:
        system_parts, user_msgs = [], []
        for m in messages:
            if m.get("role") == "system": system_parts.append(m.get("content", ""))
            elif m.get("role") in ("user", "assistant"): user_msgs.append({"role": m["role"], "content": m.get("content", "")})
        return "\n\n".join(system_parts), user_msgs

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any] | None:
        if not text: return None
        try: return json.loads(text.strip())
        except json.JSONDecodeError: pass
        m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
        if m:
            try: return json.loads(m.group(1).strip())
            except json.JSONDecodeError: pass
        s, e = text.find("{"), text.rfind("}")
        if s >= 0 and e > s:
            try: return json.loads(text[s:e + 1])
            except json.JSONDecodeError: pass
        return None
