"""Subtask Decomposition — break complex tasks into independently executable subtasks.

Workflow:
    1. After intent recognition, TaskSpawner analyzes if decomposition is beneficial
    2. LLM generates a SubTaskList (independent or dependent subtasks)
    3. Each subtask is executed as an independent Task with its own timeout
    4. Parent task waits for children, then synthesizes final output

When to decompose:
    - Task mentions "all", "every", "each", "批量", "全部", "每个" with file operations
    - Task has clear independent sub-goals
    - Task would likely exceed a single task's timeout
"""

from __future__ import annotations

import json as _json
import logging
from dataclasses import dataclass, field
from typing import Any

from flowcraft_core.domain.enums import RiskLevel, TaskStatus
from flowcraft_core.domain.schemas import Task, TaskBrief, now_utc
from flowcraft_core.models.gateway import ModelGateway

logger = logging.getLogger(__name__)

# Keywords that suggest a task can be decomposed
DECOMPOSITION_HINTS = [
    "所有", "全部", "每个", "每一", "批量",
    "all", "every", "each", "batch",
    "逐个", "分别", "依次", "遍历",
]

MAX_SUBTASKS = 12  # Prevent unbounded decomposition


@dataclass
class SubTaskDef:
    """Definition of a single subtask."""
    title: str
    objective: str
    task_type: str = "FILE_TASK"
    risk_level: str = "LOW"
    depends_on: list[int] = field(default_factory=list)  # indices of prerequisite subtasks
    expected_output: str = ""


@dataclass
class SubTaskDecomposition:
    """Result of task decomposition analysis."""
    decomposable: bool
    reason: str
    subtasks: list[SubTaskDef] = field(default_factory=list)


class TaskSpawner:
    """Analyzes tasks and spawns subtasks for parallel/sequential execution."""

    def __init__(self, model_gateway: ModelGateway) -> None:
        self.model_gateway = model_gateway

    def should_decompose(self, task: Task, brief: TaskBrief) -> bool:
        """Quick heuristic check: does this task likely benefit from decomposition?"""
        objective_lower = task.objective.lower()

        # Skip decomposition for simple QA
        if brief.task_type == "QA":
            return False

        # Check hint keywords
        hint_count = sum(1 for hint in DECOMPOSITION_HINTS if hint in objective_lower)
        if hint_count >= 1:
            return True

        # Check if objective mentions multiple files/items
        if any(kw in objective_lower for kw in ["files", "文件", "items", "项目",
                                                  "reports", "报告", "documents"]):
            return True

        return False

    async def analyze(self, task: Task, brief: TaskBrief) -> SubTaskDecomposition:
        """Use LLM to decide if and how to decompose a task.

        Returns SubTaskDecomposition with decomposable=False if no split needed.
        """
        # Heuristic pre-check
        if not self.should_decompose(task, brief):
            return SubTaskDecomposition(decomposable=False, reason="Task too simple for decomposition")

        # LLM-based decomposition
        if not self.model_gateway.is_live():
            return SubTaskDecomposition(decomposable=False, reason="Model not available")

        try:
            prompt = f"""## Task Analysis
Objective: {task.objective}
Type: {brief.task_type}
Risk: {brief.risk_level}

## Instructions
Analyze whether this task should be decomposed into independent subtasks.
Decompose if:
1. The task involves processing multiple separate items (files, URLs, data sources)
2. Items can be processed independently
3. Processing each item takes significant time

If decomposable, output subtasks. Each subtask must:
- Have a clear, standalone objective
- Be independently executable
- Specify dependencies on other subtasks (by index) if needed
- Final aggregation subtask should depend on all data-processing subtasks

Respond in JSON following the SubTaskDecomposition schema.
If NOT decomposable, set decomposable=false and subtasks=[]."""

            result = await self.model_gateway._adapter.structured_chat(
                [{"role": "system",
                  "content": "You are a task decomposition planner. Decide if a task should be split into subtasks."},
                 {"role": "user", "content": prompt}],
                {
                    "type": "object",
                    "properties": {
                        "decomposable": {"type": "boolean"},
                        "reason": {"type": "string"},
                        "subtasks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "objective": {"type": "string"},
                                    "task_type": {"type": "string", "enum": ["QA", "FILE_TASK", "BROWSER_TASK", "LOCAL_OPERATION", "DOCUMENT_SUMMARY"]},
                                    "risk_level": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"]},
                                    "depends_on": {"type": "array", "items": {"type": "integer"}},
                                    "expected_output": {"type": "string"},
                                },
                                "required": ["title", "objective", "task_type"],
                            },
                        },
                    },
                    "required": ["decomposable", "reason", "subtasks"],
                },
                temperature=0.2, max_tokens=2048,
            )

            subtasks = [
                SubTaskDef(
                    title=s.get("title", f"Subtask {i + 1}"),
                    objective=s.get("objective", ""),
                    task_type=s.get("task_type", "FILE_TASK"),
                    risk_level=s.get("risk_level", "LOW"),
                    depends_on=s.get("depends_on", []),
                    expected_output=s.get("expected_output", ""),
                )
                for i, s in enumerate(result.get("subtasks", [])[:MAX_SUBTASKS])
            ]

            return SubTaskDecomposition(
                decomposable=result.get("decomposable", False) and len(subtasks) > 0,
                reason=result.get("reason", ""),
                subtasks=subtasks,
            )

        except Exception as exc:
            logger.warning("Subtask analysis failed: %s", exc)
            return SubTaskDecomposition(decomposable=False, reason=f"Analysis error: {exc}")

    def create_child_tasks(self, parent: Task, decomposition: SubTaskDecomposition) -> list[Task]:
        """Create Task objects for each subtask in the decomposition."""
        children = []
        for i, sub in enumerate(decomposition.subtasks):
            child = Task(
                session_id=parent.session_id,
                user_id=parent.user_id,
                title=f"[{i + 1}/{len(decomposition.subtasks)}] {sub.title}",
                objective=sub.objective,
                task_type=sub.task_type,
                risk_level=RiskLevel(sub.risk_level),
                status=TaskStatus.CREATED,
            )
            # Store parent reference in metadata (via constraints or custom field)
            child.constraints = {
                "parent_task_id": parent.task_id,
                "subtask_index": i,
                "depends_on": sub.depends_on,
            }
            children.append(child)
        return children

    def aggregate_results(self, parent: Task, children: list[Task],
                          child_outputs: list[str]) -> str:
        """Aggregate subtask results into a single final output."""
        if not child_outputs:
            return f"Task '{parent.objective}' completed with no subtask outputs."

        parts = [f"# Task: {parent.objective}\n"]
        parts.append(f"Completed {len(children)} subtasks:\n")

        for i, (child, output) in enumerate(zip(children, child_outputs)):
            status_icon = "✅" if child.status == TaskStatus.COMPLETED else "❌"
            parts.append(f"## {status_icon} Subtask {i + 1}: {child.title}")
            parts.append(output[:2000])
            parts.append("")

        return "\n".join(parts)
