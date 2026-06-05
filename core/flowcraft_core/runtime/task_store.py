from __future__ import annotations

import json
from datetime import datetime, timezone

from flowcraft_core.domain.schemas import ExecutionPlan, Task, TaskBrief, now_utc
from flowcraft_core.storage.database import Database


class TaskStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def _ensure_session(self, session_id: str, task_title: str) -> None:
        """Create or update the session record for this task's session.

        The sessions table tracks session-level metadata (creation time, last activity).
        Without this, the frontend sidebar shows no/incorrect session timestamps.
        """
        now = datetime.now(timezone.utc).isoformat()
        existing = self.db.fetch_one(
            "SELECT id FROM sessions WHERE id = ?", (session_id,))
        if existing:
            self.db.update("sessions", "id", session_id, {
                "title": task_title[:80],
                "updated_at": now,
                "last_task_id": None,  # updated below
            })
        else:
            self.db.insert_json("sessions", {
                "id": session_id,
                "title": task_title[:80],
                "created_at": now,
                "updated_at": now,
                "last_task_id": None,
            })

    def save_task(self, task: Task) -> None:
        # Ensure session record exists for correct sidebar display
        self._ensure_session(task.session_id, task.title)

        self.db.insert_json(
            "tasks",
            {
                "id": task.task_id,
                "session_id": task.session_id,
                "user_id": task.user_id,
                "title": task.title,
                "objective": task.objective,
                "task_type": task.task_type,
                "status": task.status.value,
                "risk_level": task.risk_level.value,
                "constraints_json": task.constraints,
                "success_criteria_json": task.success_criteria,
                "current_plan_id": task.current_plan_id,
                "failed_reason": task.failed_reason,
                "created_at": task.created_at.isoformat(),
                "updated_at": task.updated_at.isoformat(),
                "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            },
        )

        # Update the session with the task that was just created
        self.db.update("sessions", "id", task.session_id, {
            "last_task_id": task.task_id,
            "updated_at": task.created_at.isoformat(),
        })

    def update_task(self, task: Task) -> None:
        self.db.update(
            "tasks",
            "id",
            task.task_id,
            {
                "title": task.title,
                "objective": task.objective,
                "task_type": task.task_type,
                "status": task.status.value,
                "risk_level": task.risk_level.value,
                "constraints_json": task.constraints,
                "success_criteria_json": task.success_criteria,
                "current_plan_id": task.current_plan_id,
                "failed_reason": task.failed_reason,
                "updated_at": task.updated_at.isoformat(),
                "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            },
        )

    def save_brief(self, brief: TaskBrief) -> None:
        self.db.insert_json(
            "task_briefs",
            {
                "task_id": brief.task_id,
                "data_json": brief.model_dump(mode="json"),
                "created_at": now_utc().isoformat(),
            },
        )

    def save_plan(self, plan: ExecutionPlan) -> None:
        self.db.insert_json(
            "plans",
            {
                "id": plan.plan_id,
                "task_id": plan.task_id,
                "mode": plan.mode.value,
                "goal": plan.goal,
                "data_json": plan.model_dump(mode="json"),
                "status": plan.status,
                "version": plan.version,
                "created_at": plan.created_at.isoformat(),
            },
        )
        for step in plan.steps:
            self.db.insert_json(
                "plan_steps",
                {
                    "id": step.step_id,
                    "plan_id": plan.plan_id,
                    "task_id": plan.task_id,
                    "step_index": step.index,
                    "title": step.title,
                    "objective": step.objective,
                    "action_type": step.action_type,
                    "risk_level": step.risk_level.value,
                    "approval_required": 1 if step.approval_required else 0,
                    "status": step.status.value,
                    "data_json": step.model_dump(mode="json"),
                    "created_at": plan.created_at.isoformat(),
                    "updated_at": plan.created_at.isoformat(),
                },
            )

    def get_task_row(self, task_id: str) -> dict | None:
        row = self.db.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if not row:
            return None
        item = dict(row)
        item["constraints"] = json.loads(item.pop("constraints_json"))
        item["success_criteria"] = json.loads(item.pop("success_criteria_json"))
        return item
