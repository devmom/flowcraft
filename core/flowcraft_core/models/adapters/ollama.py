"""Ollama local model adapter.

Ollama serves an OpenAI-compatible /v1/chat/completions endpoint.
Default URL: http://localhost:11434/v1
No API key required for local models.
"""
from __future__ import annotations

import logging
from typing import Any

from flowcraft_core.models.adapters.base import ModelProfile, ProviderAdapter
from flowcraft_core.models.adapters.openai_compatible import OpenAICompatibleAdapter

logger = logging.getLogger(__name__)

# Common Ollama model profiles
OLLAMA_PROFILES: dict[str, ModelProfile] = {
    "qwen3": ModelProfile(
        model_id="qwen3",
        provider="ollama",
        display_name="Qwen 3 (Ollama)",
        base_url="http://localhost:11434/v1",
        capabilities=["chat"],
        context_window=32768,
        supports_structured_output=False,
        supports_streaming=True,
    ),
    "llama4": ModelProfile(
        model_id="llama4",
        provider="ollama",
        display_name="Llama 4 (Ollama)",
        base_url="http://localhost:11434/v1",
        capabilities=["chat"],
        context_window=131072,
        supports_structured_output=False,
        supports_streaming=True,
    ),
    "deepseek-r1": ModelProfile(
        model_id="deepseek-r1",
        provider="ollama",
        display_name="DeepSeek R1 (Ollama)",
        base_url="http://localhost:11434/v1",
        capabilities=["chat"],
        context_window=131072,
        supports_structured_output=False,
        supports_streaming=True,
    ),
    "mistral": ModelProfile(
        model_id="mistral",
        provider="ollama",
        display_name="Mistral (Ollama)",
        base_url="http://localhost:11434/v1",
        capabilities=["chat"],
        context_window=32768,
        supports_structured_output=False,
        supports_streaming=True,
    ),
    "codestral": ModelProfile(
        model_id="codestral",
        provider="ollama",
        display_name="Codestral (Ollama)",
        base_url="http://localhost:11434/v1",
        capabilities=["chat"],
        context_window=32768,
        supports_structured_output=False,
        supports_streaming=True,
    ),
}


class OllamaAdapter(OpenAICompatibleAdapter):
    """Ollama 本地模型适配器.

    复用 OpenAICompatibleAdapter，因为 Ollama 支持 /v1/chat/completions 端点。
    区别：
        - 默认本地端点 http://localhost:11434/v1
        - 不需要 API Key
        - 结构化输出取决于具体模型能力
    """

    @classmethod
    def from_model_name(
        cls,
        model_name: str = "qwen3",
        base_url: str | None = None,
        api_key: str = "ollama",
        timeout_seconds: int = 120,
        max_retries: int = 2,
    ) -> "OllamaAdapter":
        """从模型名称创建适配器（自动匹配已知 profile）."""
        if model_name in OLLAMA_PROFILES:
            profile = OLLAMA_PROFILES[model_name]
        else:
            profile = ModelProfile(
                model_id=model_name,
                provider="ollama",
                display_name=f"{model_name} (Ollama)",
                base_url=base_url or "http://localhost:11434/v1",
                capabilities=["chat"],
                context_window=8192,
                supports_structured_output=False,
                supports_streaming=True,
            )
        if base_url:
            profile.base_url = base_url
        return cls(profile, api_key=api_key, timeout_seconds=timeout_seconds, max_retries=max_retries)

    @classmethod
    async def list_local_models(cls, base_url: str = "http://localhost:11434") -> list[dict]:
        """列出本地已安装的 Ollama 模型."""
        import asyncio
        import json
        import urllib.request

        def _list():
            url = base_url.rstrip("/") + "/api/tags"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("models", [])

        try:
            return await asyncio.to_thread(_list)
        except Exception as exc:
            logger.warning("Failed to list Ollama models: %s", exc)
            return []

    async def test_connection(self) -> dict[str, Any]:
        result = await super().test_connection()
        if result["status"] == "ok":
            result["provider"] = "ollama"
            result["model"] = self.profile.model_id
            result["type"] = "local"
        return result


def create_ollama_profile(model_name: str, base_url: str | None = None) -> ModelProfile:
    """动态创建 Ollama 模型 profile."""
    if model_name in OLLAMA_PROFILES:
        profile = OLLAMA_PROFILES[model_name]
        if base_url:
            profile.base_url = base_url
        return profile
    return ModelProfile(
        model_id=model_name,
        provider="ollama",
        display_name=f"{model_name} (Ollama)",
        base_url=base_url or "http://localhost:11434/v1",
        capabilities=["chat"],
        context_window=8192,
        supports_structured_output=False,
        supports_streaming=True,
    )
