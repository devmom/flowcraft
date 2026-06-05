"""Task Timeline Replay - Visual step-by-step execution playback.

Features:
    - Replay task execution with timestamps
    - Step-by-step navigation (prev/next/pause)
    - Tool call visualization
    - Decision point highlighting
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from flowcraft_core.storage.database import Database
from flowcraft_core.observability.events import EventRecorder

logger = logging.getLogger(__name__)


class TaskReplay:
    """Replay task execution as a structured timeline."""

    def __init__(self, db: Database, events: EventRecorder) -> None:
        self.db = db
        self.events = events

    def get_timeline(self, task_id: str) -> dict:
        """Get complete replayable timeline for a task."""
        task_row = self.db.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if not task_row:
            return {"error": "Task not found"}

        task = dict(task_row)
        events = self.events.list_for_task(task_id)

        # Build timeline structure
        phases = []
        current_phase: dict | None = None

        for ev in events:
            et = ev.get("event_type", "")

            # Phase transitions
            if et in ("task.created",):
                current_phase = {"phase": "creation", "title": "Task Created",
                                 "events": [], "start": ev.get("created_at")}
                phases.append(current_phase)
            elif et in ("intent.recognized",):
                current_phase = {"phase": "intent", "title": "Intent Analysis",
                                 "events": [], "start": ev.get("created_at")}
                phases.append(current_phase)
            elif et in ("plan.created",):
                current_phase = {"phase": "planning", "title": "Planning",
                                 "events": [], "start": ev.get("created_at")}
                phases.append(current_phase)
            elif et in ("execution.started",):
                current_phase = {"phase": "execution", "title": "Execution",
                                 "events": [], "start": ev.get("created_at")}
                phases.append(current_phase)
            elif et in ("step.started",):
                current_phase = {"phase": f"step_{ev.get('payload', {}).get('step_index', '?')}",
                                 "title": ev.get("title", "Step"),
                                 "events": [], "start": ev.get("created_at")}
                phases.append(current_phase)
            elif et in ("task.completed", "task.failed", "task.cancelled"):
                current_phase = {"phase": "completion", "title": ev.get("title", "Complete"),
                                 "events": [], "start": ev.get("created_at")}
                phases.append(current_phase)

            # Add event to current phase
            if current_phase:
                current_phase["events"].append({
                    "type": et,
                    "title": ev.get("title", ""),
                    "message": ev.get("message", ""),
                    "time": ev.get("created_at", ""),
                    "severity": ev.get("severity", "INFO"),
                    "payload": ev.get("payload", {}),
                })

        # Statistics
        tool_calls = sum(1 for e in events if e.get("event_type", "").startswith("tool."))
        errors = sum(1 for e in events if e.get("severity") == "ERROR")
        warnings = sum(1 for e in events if e.get("severity") == "WARN")
        duration = ""
        if task.get("created_at") and task.get("updated_at"):
            try:
                start = datetime.fromisoformat(task["created_at"])
                end = datetime.fromisoformat(task["updated_at"])
                delta = end - start
                duration = f"{delta.total_seconds():.1f}s"
            except Exception:
                pass

        return {
            "task_id": task_id,
            "title": task.get("title", ""),
            "objective": task.get("objective", ""),
            "status": task.get("status", ""),
            "risk_level": task.get("risk_level", ""),
            "created_at": task.get("created_at", ""),
            "completed_at": task.get("completed_at"),
            "failed_reason": task.get("failed_reason"),
            "duration": duration,
            "phases": phases,
            "total_events": len(events),
            "stats": {
                "tool_calls": tool_calls,
                "errors": errors,
                "warnings": warnings,
                "phases": len(phases),
            },
        }

    def get_summary(self, task_id: str) -> dict:
        """Get a compact task summary."""
        timeline = self.get_timeline(task_id)
        if "error" in timeline:
            return timeline

        # Extract key outputs
        outputs = []
        for phase in timeline.get("phases", []):
            for ev in phase.get("events", []):
                if ev["type"] in ("step.answer", "tool.completed"):
                    payload = ev.get("payload", {})
                    output = payload.get("output", ev.get("message", ""))
                    if output and len(output) > 30:
                        outputs.append({"phase": phase["title"], "output": output[:500]})

        return {
            "task_id": task_id,
            "title": timeline["title"],
            "status": timeline["status"],
            "duration": timeline["duration"],
            "stats": timeline["stats"],
            "key_outputs": outputs[:5],
        }

