from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from flowcraft_core.domain.enums import RiskLevel
from flowcraft_core.domain.schemas import ToolIntent, ToolObservation, now_utc


class ToolDefinition(BaseModel):
    tool_name: str
    display_name: str
    description: str
    category: str
    risk_level: RiskLevel
    permissions: list[str] = Field(default_factory=list)
    requires_approval_by_default: bool = False
    timeout_seconds: int = 30
    max_retries: int = 0
    supports_dry_run: bool = False
    supports_rollback: bool = False
    examples: list[dict] = Field(default_factory=list)  # Few-shot examples for LLM tool selection


class Tool(ABC):
    definition: ToolDefinition

    @abstractmethod
    async def execute(self, intent: ToolIntent) -> ToolObservation:
        raise NotImplementedError


def is_path_allowed(path: Path, allowed_paths: list[Path]) -> bool:
    resolved = path.resolve()
    for allowed in allowed_paths:
        try:
            resolved.relative_to(allowed.resolve())
            return True
        except ValueError:
            continue
    return False


def observation_from_output(
    intent: ToolIntent,
    status: str,
    summary: str,
    payload: dict[str, Any] | None = None,
    error: str | None = None,
) -> ToolObservation:
    now = now_utc()
    return ToolObservation(
        tool_intent_id=intent.tool_intent_id,
        task_id=intent.task_id,
        step_id=intent.step_id,
        status=status,
        output_summary=summary,
        output_payload=payload or {},
        error_message=error,
        started_at=now,
        finished_at=now,
    )

