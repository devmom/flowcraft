"""Model provider adapters."""

from flowcraft_core.models.adapters.base import ModelProfile, ProviderAdapter, ModelCallRecord

__all__ = [
    "ModelProfile",
    "ProviderAdapter",
    "ModelCallRecord",
    # Agnes AI adapters are imported on demand to avoid circular deps:
    #   AgnesTextAdapter  — LLM chat (OpenAI-compatible)
    #   AgnesImageAdapter — image generation
    #   AgnesVideoAdapter — video generation (not yet available)
]
