"""Workflow system - templates and runs.

MVP 预留数据结构，不做图形化编辑器。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class WorkflowTemplate:
    """可复用工作流模板。

    后续阶段支持从任务保存为模板、编辑步骤、导入导出。
    """

    name: str
    workflow_id: str = field(default_factory=lambda: _new_id("wf"))
    description: str = ""
    author: str = "local-user"
    version: str = "1.0.0"
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    required_tools: list[str] = field(default_factory=list)
    required_permissions: list[str] = field(default_factory=list)
    risk_summary: str = "LOW"
    tags: list[str] = field(default_factory=list)
    use_count: int = 0
    status: str = "active"
    created_at: str = field(default_factory=_now_utc)
    updated_at: str = field(default_factory=_now_utc)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "name": self.name,
            "description": self.description,
            "author": self.author,
            "version": self.version,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "steps": self.steps,
            "required_tools": self.required_tools,
            "required_permissions": self.required_permissions,
            "risk_summary": self.risk_summary,
            "tags": self.tags,
            "use_count": self.use_count,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class WorkflowRun:
    """工作流运行实例。"""

    workflow_id: str
    workflow_run_id: str = field(default_factory=lambda: _new_id("wfrun"))
    task_id: str | None = None
    input_payload: dict[str, Any] = field(default_factory=dict)
    status: str = "running"
    created_at: str = field(default_factory=_now_utc)
    completed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_run_id": self.workflow_run_id,
            "workflow_id": self.workflow_id,
            "task_id": self.task_id,
            "input_payload": self.input_payload,
            "status": self.status,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }
