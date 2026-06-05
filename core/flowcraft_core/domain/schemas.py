from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from .enums import ApprovalStatus, PlanMode, PolicyDecisionValue, RiskLevel, StepStatus, TaskStatus


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class AgentRequest(BaseModel):
    request_id: str = Field(default_factory=lambda: new_id("req"))
    session_id: str
    user_id: str = "local-user"
    raw_input: str
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    source: str = "desktop"
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_utc)


class Task(BaseModel):
    task_id: str = Field(default_factory=lambda: new_id("task"))
    session_id: str
    user_id: str = "local-user"
    title: str
    objective: str
    task_type: str = "UNKNOWN"
    status: TaskStatus = TaskStatus.CREATED
    risk_level: RiskLevel = RiskLevel.LOW
    constraints: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    current_plan_id: str | None = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)
    completed_at: datetime | None = None
    failed_reason: str | None = None


class TaskBrief(BaseModel):
    task_id: str
    objective: str
    task_type: str
    target_objects: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    requires_local_files: bool = False
    requires_network: bool = False
    requires_tools: bool = False
    risk_level: RiskLevel = RiskLevel.LOW
    clarification_required: bool = False
    clarification_questions: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    expected_output_format: str = "text"


class PlanStep(BaseModel):
    step_id: str = Field(default_factory=lambda: new_id("step"))
    plan_id: str | None = None
    index: int
    title: str
    objective: str
    action_type: str
    required_context: dict[str, Any] = Field(default_factory=dict)
    required_tools: list[str] = Field(default_factory=list)
    depends_on: list[int] = Field(default_factory=list)  # DAG: 依赖的步骤 index 列表
    expected_output: str
    risk_level: RiskLevel = RiskLevel.LOW
    approval_required: bool = False
    status: StepStatus = StepStatus.PENDING
    retry_count: int = 0
    max_retries: int = 2
    completion_check: dict[str, Any] = Field(default_factory=dict)
    failure_strategy: dict[str, Any] = Field(default_factory=dict)


class ExecutionPlan(BaseModel):
    plan_id: str = Field(default_factory=lambda: new_id("plan"))
    task_id: str
    mode: PlanMode
    goal: str
    assumptions: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    steps: list[PlanStep]
    risk_points: list[str] = Field(default_factory=list)
    approval_points: list[str] = Field(default_factory=list)
    fallback_strategy: dict[str, Any] = Field(default_factory=dict)
    stop_conditions: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=now_utc)
    version: int = 1
    status: str = "ACTIVE"


class ToolIntent(BaseModel):
    tool_intent_id: str = Field(default_factory=lambda: new_id("tool_intent"))
    task_id: str
    step_id: str
    tool_name: str
    purpose: str
    input_summary: str
    input_payload: dict[str, Any]
    expected_result: str
    risk_level: RiskLevel = RiskLevel.LOW
    requires_approval: bool = False
    created_at: datetime = Field(default_factory=now_utc)


class ToolObservation(BaseModel):
    observation_id: str = Field(default_factory=lambda: new_id("obs"))
    tool_intent_id: str
    task_id: str
    step_id: str
    status: str
    output_summary: str
    output_payload_ref: str | None = None
    output_payload: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None
    started_at: datetime = Field(default_factory=now_utc)
    finished_at: datetime = Field(default_factory=now_utc)
    duration_ms: int = 0
    audit_id: str | None = None


class PolicyDecision(BaseModel):
    decision_id: str = Field(default_factory=lambda: new_id("decision"))
    task_id: str
    step_id: str | None = None
    target_type: str
    target_id: str
    decision: PolicyDecisionValue
    reason: str
    matched_rules: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    created_at: datetime = Field(default_factory=now_utc)


class ApprovalRequest(BaseModel):
    approval_id: str = Field(default_factory=lambda: new_id("approval"))
    task_id: str
    step_id: str | None = None
    action_title: str
    action_description: str
    risk_level: RiskLevel
    impact_preview: list[str] = Field(default_factory=list)
    requested_by: str = "flowcraft"
    status: ApprovalStatus = ApprovalStatus.PENDING
    user_decision: str | None = None
    created_at: datetime = Field(default_factory=now_utc)
    resolved_at: datetime | None = None


class TraceEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: new_id("event"))
    task_id: str | None = None
    session_id: str | None = None
    event_type: str
    title: str
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_utc)
    severity: str = "INFO"

