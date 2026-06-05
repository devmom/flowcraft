"""Task Checkpoint — save/resume long-running task execution.

Each step completion triggers an automatic checkpoint.
On service restart, in-progress tasks are restored from the latest checkpoint
instead of being marked FAILED.

Schema per checkpoint row (stored in checkpoints table):
    id              — checkpoint UUID
    task_id         — FK to tasks
    checkpoint_idx  — monotonically increasing index (0, 1, 2, ...)
    completed_steps_json  — list of completed step indices
    current_step_index    — the step that was about to execute
    observation_snapshot_json — last 3 ToolObservations
    context_summary       — compressed LLM context (< 500 chars)
    plan_snapshot_json    — serialized ExecutionPlan
    created_at
"""

from __future__ import annotations

import json as _json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from flowcraft_core.domain.schemas import ExecutionPlan, PlanStep, ToolObservation, now_utc
from flowcraft_core.storage.database import Database

logger = logging.getLogger(__name__)


@dataclass
class TaskCheckpoint:
    """A snapshot of task execution state that enables resume."""
    task_id: str
    checkpoint_idx: int = 0
    completed_step_indices: list[int] = field(default_factory=list)
    current_step_index: int = 0
    observation_snapshot: list[dict[str, Any]] = field(default_factory=list)
    context_summary: str = ""
    plan_snapshot: dict[str, Any] | None = None
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = f"ckpt_{uuid4().hex[:12]}"


class CheckpointManager:
    """Manages task checkpoints — save, load, restore, cleanup."""

    MAX_CHECKPOINTS_PER_TASK = 20  # Keep last N checkpoints, prune older ones

    def __init__(self, db: Database) -> None:
        self.db = db
        self._ensure_table()

    def _ensure_table(self) -> None:
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                checkpoint_idx INTEGER NOT NULL DEFAULT 0,
                completed_steps_json TEXT NOT NULL DEFAULT '[]',
                current_step_index INTEGER NOT NULL DEFAULT 0,
                observation_snapshot_json TEXT NOT NULL DEFAULT '[]',
                context_summary TEXT NOT NULL DEFAULT '',
                plan_snapshot_json TEXT,
                created_at TEXT NOT NULL
            )
        """)
        self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_checkpoints_task
            ON checkpoints(task_id, checkpoint_idx DESC)
        """)

    def save(self, task_id: str, completed_step_indices: list[int],
             current_step_index: int, observations: list[ToolObservation],
             context_summary: str = "", plan: ExecutionPlan | None = None) -> TaskCheckpoint:
        """Save a checkpoint for the given task."""

        # Get current max checkpoint index
        row = self.db.fetch_one(
            "SELECT COALESCE(MAX(checkpoint_idx), -1) + 1 AS next_idx FROM checkpoints WHERE task_id = ?",
            (task_id,))
        next_idx = dict(row)["next_idx"] if row else 0

        # Serialize observations (last 3, truncated)
        obs_snapshot = []
        for obs in observations[-3:]:
            obs_snapshot.append({
                "tool_name": obs.tool_intent_id,  # approximate
                "status": obs.status,
                "summary": obs.output_summary,
                "payload": _truncate_payload(obs.output_payload, 2000),
            })

        checkpoint = TaskCheckpoint(
            task_id=task_id,
            checkpoint_idx=next_idx,
            completed_step_indices=list(completed_step_indices),
            current_step_index=current_step_index,
            observation_snapshot=obs_snapshot,
            context_summary=context_summary[:500],
            plan_snapshot=plan.model_dump(mode="json") if plan else None,
        )

        self.db.execute(
            """INSERT INTO checkpoints
               (id, task_id, checkpoint_idx, completed_steps_json,
                current_step_index, observation_snapshot_json,
                context_summary, plan_snapshot_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (checkpoint.id, task_id, checkpoint.checkpoint_idx,
             _json.dumps(checkpoint.completed_step_indices),
             checkpoint.current_step_index,
             _json.dumps(checkpoint.observation_snapshot, ensure_ascii=False),
             checkpoint.context_summary,
             _json.dumps(checkpoint.plan_snapshot, ensure_ascii=False) if checkpoint.plan_snapshot else None,
             now_utc().isoformat()),
        )

        # Prune old checkpoints
        self.db.execute(
            """DELETE FROM checkpoints WHERE task_id = ? AND id NOT IN (
                   SELECT id FROM checkpoints WHERE task_id = ?
                   ORDER BY checkpoint_idx DESC LIMIT ?)""",
            (task_id, task_id, self.MAX_CHECKPOINTS_PER_TASK),
        )

        logger.debug("Checkpoint #%d saved for task %s", next_idx, task_id[:12])
        return checkpoint

    def load_latest(self, task_id: str) -> TaskCheckpoint | None:
        """Load the most recent checkpoint for a task."""
        row = self.db.fetch_one(
            "SELECT * FROM checkpoints WHERE task_id = ? ORDER BY checkpoint_idx DESC LIMIT 1",
            (task_id,))
        if not row:
            return None
        return self._row_to_checkpoint(dict(row))

    def load_by_index(self, task_id: str, idx: int) -> TaskCheckpoint | None:
        """Load a specific checkpoint by index."""
        row = self.db.fetch_one(
            "SELECT * FROM checkpoints WHERE task_id = ? AND checkpoint_idx = ?",
            (task_id, idx))
        if not row:
            return None
        return self._row_to_checkpoint(dict(row))

    def list_checkpoints(self, task_id: str) -> list[dict[str, Any]]:
        """List all checkpoints for a task (metadata only)."""
        rows = self.db.fetch_all(
            "SELECT id, checkpoint_idx, current_step_index, context_summary, created_at "
            "FROM checkpoints WHERE task_id = ? ORDER BY checkpoint_idx ASC",
            (task_id,))
        return [dict(r) for r in rows]

    def get_resumable_tasks(self) -> list[str]:
        """Find all tasks that have checkpoints and are in non-terminal states."""
        rows = self.db.fetch_all("""
            SELECT DISTINCT c.task_id FROM checkpoints c
            JOIN tasks t ON t.id = c.task_id
            WHERE t.status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')
            ORDER BY c.created_at DESC
        """)
        return [dict(r)["task_id"] for r in rows]

    def delete_for_task(self, task_id: str) -> None:
        """Delete all checkpoints for a task (e.g., when task completes)."""
        self.db.execute("DELETE FROM checkpoints WHERE task_id = ?", (task_id,))

    @staticmethod
    def _row_to_checkpoint(row: dict) -> TaskCheckpoint:
        return TaskCheckpoint(
            id=row["id"],
            task_id=row["task_id"],
            checkpoint_idx=row["checkpoint_idx"],
            completed_step_indices=_json.loads(row["completed_steps_json"]),
            current_step_index=row["current_step_index"],
            observation_snapshot=_json.loads(row["observation_snapshot_json"]),
            context_summary=row["context_summary"],
            plan_snapshot=_json.loads(row["plan_snapshot_json"]) if row.get("plan_snapshot_json") else None,
        )


def _truncate_payload(payload: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """Truncate large string values in payload for checkpoint storage."""
    result = {}
    for k, v in payload.items():
        if isinstance(v, str) and len(v) > max_chars:
            result[k] = v[:max_chars] + f"...[{len(v)} total chars]"
        elif isinstance(v, dict):
            result[k] = _truncate_payload(v, max_chars // 2)
        elif isinstance(v, list) and len(_json.dumps(v, default=str)) > max_chars:
            result[k] = v[:3]  # keep first 3 items
        else:
            result[k] = v
    return result
