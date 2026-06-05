"""Agnes AI Provider Adapters.

Agnes AI (https://agnes-ai.com/) is an AI Gateway by Sapiens AI offering:
- Text LLM: agnes-2.0-flash, agnes-1.5-flash (OpenAI-compatible /v1/chat/completions)
- Image Generation: agnes-image-2.0-flash, agnes-image-2.1-flash (/v1/images/generations)
- Video Generation: agnes-video-v2.0 (endpoint not yet available — 404 as of 2026-06-03)

IMPORTANT: Only text models are usable as LLM backends. Image/video models
must NOT be passed to /chat/completions — they use separate endpoints.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from flowcraft_core.models.adapters.base import ModelProfile, ProviderAdapter

logger = logging.getLogger(__name__)

# ── Agnes API Configuration ──────────────────────────────────
AGNES_BASE_URL = "https://apihub.agnes-ai.com/v1"

AGNES_2_FLASH_PROFILE = ModelProfile(
    model_id="agnes-2.0-flash",
    provider="agnes",
    display_name="Agnes 2.0 Flash",
    base_url=AGNES_BASE_URL,
    capabilities=["chat", "structured_chat"],
    context_window=128000,  # conservative estimate; actual limit TBD
    supports_structured_output=True,
    supports_streaming=True,  # assumed OpenAI-compatible
    cost_input_per_1k=0.0,    # free tier
    cost_output_per_1k=0.0,
)

AGNES_1_5_FLASH_PROFILE = ModelProfile(
    model_id="agnes-1.5-flash",
    provider="agnes",
    display_name="Agnes 1.5 Flash",
    base_url=AGNES_BASE_URL,
    capabilities=["chat", "structured_chat"],
    context_window=128000,
    supports_structured_output=True,
    supports_streaming=True,
    cost_input_per_1k=0.0,
    cost_output_per_1k=0.0,
)

# ── Non-LLM model IDs (must NOT be used with chat/completions) ──
AGNES_IMAGE_MODELS = {"agnes-image-2.0-flash", "agnes-image-2.1-flash"}
AGNES_VIDEO_MODELS = {"agnes-video-v2.0"}
AGNES_NON_LLM_MODELS = AGNES_IMAGE_MODELS | AGNES_VIDEO_MODELS

# All model IDs (used for model listing)
ALL_AGNES_MODELS = {
    "agnes-2.0-flash": AGNES_2_FLASH_PROFILE,
    "agnes-1.5-flash": AGNES_1_5_FLASH_PROFILE,
}


def is_agnes_llm(model_id: str) -> bool:
    """Check if a model ID is an Agnes text LLM (not image/video)."""
    return model_id in ALL_AGNES_MODELS


# ── Adapter: Agnes Text (LLM) ────────────────────────────────

class AgnesTextAdapter(ProviderAdapter):
    """Agnes text LLM adapter — wraps OpenAICompatibleAdapter for chat.

    Uses the OpenAI-compatible /v1/chat/completions endpoint.
    Image and video models are NOT supported here — use AgnesImageAdapter instead.
    """

    def __init__(
        self,
        profile: ModelProfile,
        api_key: str,
        timeout_seconds: int = 60,
        max_retries: int = 2,
    ) -> None:
        if profile.model_id in AGNES_NON_LLM_MODELS:
            raise ValueError(
                f"AgnesTextAdapter cannot use image/video model '{profile.model_id}'. "
                f"Use AgnesImageAdapter for image models."
            )
        self.profile = profile
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=15.0),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ── chat (LLM text) ──────────────────────────────────

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        t0 = time.monotonic()
        model_id = self.profile.model_id
        last_error: str | None = None
        from flowcraft_core.models.adapters.openai_compatible import OpenAICompatibleAdapter

        for retry in range(self._max_retries + 1):
            try:
                payload = self._build_payload(messages, kwargs)
                client = self._get_client()
                url = f"{self.profile.base_url}/chat/completions"
                api_t0 = time.monotonic()
                resp = await client.post(url, content=payload)
                body = resp.json()
                api_elapsed = time.monotonic() - api_t0

                if resp.status_code == 200:
                    content = OpenAICompatibleAdapter._extract_text(body)
                    duration_ms = int((time.monotonic() - t0) * 1000)
                    usage = body.get("usage", {})
                    logger.info(
                        "agnes.call model=%s status=200 prompt=%d completion=%d dur=%dms api_time=%.2fs",
                        model_id,
                        usage.get("prompt_tokens", 0),
                        usage.get("completion_tokens", 0),
                        duration_ms,
                        api_elapsed,
                    )
                    return content

                last_error = body.get("error", {}).get("message", f"HTTP {resp.status_code}")
                logger.warning("agnes.call error model=%s status=%d error=%s",
                              model_id, resp.status_code, last_error[:100])

            except (httpx.TimeoutException, httpx.RequestError, OSError) as exc:
                last_error = str(exc)
                logger.warning("agnes.call exception model=%s error=%s", model_id, type(exc).__name__)

            if retry < self._max_retries:
                import asyncio
                await asyncio.sleep(1.0 * (retry + 1))

        raise RuntimeError(f"Agnes model call failed after {self._max_retries + 1} attempts: {last_error}")

    async def structured_chat(
        self,
        messages: list[dict[str, str]],
        output_schema: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        from flowcraft_core.models.adapters.openai_compatible import OpenAICompatibleAdapter

        schema_instruction = (
            "\n\nIMPORTANT: Respond ONLY with valid JSON matching this schema. "
            "Do NOT wrap in markdown code fences.\n"
            f"Schema: {json.dumps(output_schema, ensure_ascii=False)}"
        )

        augmented = list(messages)
        if augmented and augmented[-1]["role"] == "user":
            augmented[-1] = {"role": "user", "content": augmented[-1]["content"] + schema_instruction}
        else:
            augmented.insert(0, {"role": "system", "content": "Output only valid JSON matching the described schema."})

        for attempt in range(3):
            raw = await self.chat(augmented, **kwargs)
            parsed = OpenAICompatibleAdapter._parse_json(raw)
            if parsed is not None:
                return parsed
            if attempt < 2:
                augmented.append({
                    "role": "user",
                    "content": "Your last response was not valid JSON. Output ONLY valid JSON (no markdown, no extra text).",
                })

        raise RuntimeError("Failed to get valid structured output after 3 attempts")

    async def test_connection(self) -> dict[str, Any]:
        t0 = time.monotonic()
        try:
            await self.chat([{"role": "user", "content": "Reply with exactly 'ok'."}])
            latency_ms = int((time.monotonic() - t0) * 1000)
            return {"status": "ok", "message": "Agnes connection successful", "latency_ms": latency_ms}
        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return {"status": "error", "message": str(exc), "latency_ms": latency_ms}

    def estimate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return 0.0  # Free tier

    # ── Internal ──────────────────────────────────────────

    def _build_payload(self, messages: list[dict], kwargs: dict[str, Any]) -> bytes:
        payload: dict[str, Any] = {
            "model": self.profile.model_id,
            "messages": messages,
            "temperature": kwargs.get("temperature", 0.7),
            "max_tokens": kwargs.get("max_tokens", 4096),
        }
        if "response_format" in kwargs:
            payload["response_format"] = kwargs["response_format"]
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")


# ── Adapter: Agnes Image Generation ──────────────────────────

class AgnesImageAdapter:
    """Agnes image generation adapter.

    Uses /v1/images/generations endpoint (OpenAI-compatible).
    NOT a ProviderAdapter — image generation is a separate capability,
    not usable as an LLM backend.

    Supported models: agnes-image-2.0-flash, agnes-image-2.1-flash
    """

    DEFAULT_MODEL = "agnes-image-2.1-flash"

    def __init__(self, api_key: str, model: str | None = None, timeout_seconds: int = 120):
        if model and model not in AGNES_IMAGE_MODELS:
            raise ValueError(f"Not an image model: {model}. Choose from {AGNES_IMAGE_MODELS}")
        self.model = model or self.DEFAULT_MODEL
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=15.0),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                },
            )
        return self._client

    async def generate(
        self,
        prompt: str,
        size: str = "1024x1024",
        n: int = 1,
    ) -> list[str]:
        """Generate images from a text prompt.

        Args:
            prompt: Text description of the image.
            size: One of "256x256", "512x512", "1024x1024".
            n: Number of images to generate (1-4).

        Returns:
            List of image URLs.
        """
        payload = json.dumps({
            "model": self.model,
            "prompt": prompt,
            "n": n,
            "size": size,
        }).encode("utf-8")

        client = self._get_client()
        url = f"{AGNES_BASE_URL}/images/generations"

        resp = await client.post(url, content=payload)
        if resp.status_code != 200:
            body = resp.json()
            error_msg = body.get("error", {}).get("message", f"HTTP {resp.status_code}")
            raise RuntimeError(f"Agnes image generation failed: {error_msg}")

        body = resp.json()
        urls = [item.get("url") for item in body.get("data", []) if item.get("url")]
        logger.info("agnes.image model=%s prompt=%s size=%s n=%d -> %d urls",
                    self.model, prompt[:60], size, n, len(urls))
        return urls

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None


# ── Adapter: Agnes Video Generation (placeholder) ─────────────

class AgnesVideoAdapter:
    """Agnes video generation adapter.

    Uses /v1/videos/generations endpoint (when available).
    As of 2026-06-03, the video generation endpoint returns 404 —
    the model exists but generation is not yet available.

    NOT a ProviderAdapter — video generation is separate from LLM.
    """

    DEFAULT_MODEL = "agnes-video-v2.0"

    def __init__(self, api_key: str, model: str | None = None, timeout_seconds: int = 300):
        if model and model not in AGNES_VIDEO_MODELS:
            raise ValueError(f"Not a video model: {model}. Choose from {AGNES_VIDEO_MODELS}")
        self.model = model or self.DEFAULT_MODEL
        self._api_key = api_key
        self._timeout = timeout_seconds

    async def generate(self, prompt: str, duration: int = 5) -> dict[str, Any]:
        """Generate video from text prompt (NOT YET AVAILABLE).

        As of 2026-06-03, this endpoint returns 404.
        """
        raise NotImplementedError(
            "Agnes video generation is not yet available. "
            "The /v1/videos/generations endpoint currently returns 404."
        )
