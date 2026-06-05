"""Provider Adapter base class."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ModelProfile:
    """模型配置档案."""
    model_id: str
    provider: str
    display_name: str
    base_url: str = ""
    api_key_ref: str = ""  # reference to SecretStore key
    capabilities: list[str] = field(default_factory=lambda: ["chat", "structured_chat"])
    context_window: int = 128000
    supports_structured_output: bool = True
    supports_streaming: bool = True
    cost_input_per_1k: float = 0.0
    cost_output_per_1k: float = 0.0
    enabled: bool = True


@dataclass
class ModelCallRecord:
    """模型调用记录，写入 model_calls 表."""
    call_id: str
    task_id: str | None
    step_id: str | None
    provider: str
    model_name: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int | None = None
    status: str = "completed"
    error_message: str | None = None
    cost_estimate: float | None = None


class ProviderAdapter(ABC):
    """模型供应商适配器抽象基类."""

    profile: ModelProfile

    @abstractmethod
    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        """普通对话，返回文本."""
        ...

    @abstractmethod
    async def structured_chat(
        self, messages: list[dict[str, str]], output_schema: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        """结构化对话，返回 JSON/dict."""
        ...

    @abstractmethod
    async def test_connection(self) -> dict[str, Any]:
        """测试连接，返回 {status, message, latency_ms}."""
        ...

    def estimate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        """成本估算（美元）."""
        cost = (
            prompt_tokens / 1000 * self.profile.cost_input_per_1k
            + completion_tokens / 1000 * self.profile.cost_output_per_1k
        )
        return round(cost, 6)
