from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def dump(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, list):
        return [dump(item) for item in value]
    if isinstance(value, dict):
        return {key: dump(item) for key, item in value.items()}
    if isinstance(value, datetime):
        return value.isoformat()
    return value


class SimpleModel:
    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        return {key: dump(value) for key, value in asdict(self).items()}

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


@dataclass
class AgentRequest(SimpleModel):
    session_id: str
    raw_input: str
    request_id: str = field(default_factory=lambda: new_id("req"))
    user_id: str = "local-user"
    attachments: list[dict[str, Any]] = field(default_factory=list)
    source: str = "desktop"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=now_utc)


@dataclass
class Task(SimpleModel):
    session_id: str
    title: str
    objective: str
    task_id: str = field(default_factory=lambda: new_id("task"))
    user_id: str = "local-user"
    task_type: str = "UNKNOWN"
    status: str = "CREATED"
    risk_level: str = "LOW"
    constraints: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    current_plan_id: str | None = None
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)
    completed_at: datetime | None = None
    failed_reason: str | None = None


@dataclass
class TaskBrief(SimpleModel):
    task_id: str
    objective: str
    task_type: str
    target_objects: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    requires_local_files: bool = False
    requires_network: bool = False
    requires_tools: bool = False
    risk_level: str = "LOW"
    clarification_required: bool = False
    clarification_questions: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    expected_output_format: str = "text"


@dataclass
class PlanStep(SimpleModel):
    index: int
    title: str
    objective: str
    action_type: str
    expected_output: str
    step_id: str = field(default_factory=lambda: new_id("step"))
    plan_id: str | None = None
    required_context: dict[str, Any] = field(default_factory=dict)
    required_tools: list[str] = field(default_factory=list)
    risk_level: str = "LOW"
    approval_required: bool = False
    status: str = "PENDING"
    retry_count: int = 0
    max_retries: int = 2
    completion_check: dict[str, Any] = field(default_factory=dict)
    failure_strategy: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionPlan(SimpleModel):
    task_id: str
    mode: str
    goal: str
    steps: list[PlanStep]
    plan_id: str = field(default_factory=lambda: new_id("plan"))
    assumptions: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    risk_points: list[str] = field(default_factory=list)
    approval_points: list[str] = field(default_factory=list)
    fallback_strategy: dict[str, Any] = field(default_factory=dict)
    stop_conditions: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=now_utc)
    version: int = 1
    status: str = "ACTIVE"


@dataclass
class TraceEvent(SimpleModel):
    event_type: str
    title: str
    message: str
    event_id: str = field(default_factory=lambda: new_id("event"))
    task_id: str | None = None
    session_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=now_utc)
    severity: str = "INFO"

