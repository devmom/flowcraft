"""VentSessionManager — manages the full lifecycle of a vent session.

State machine: IDLE -> DETECTED -> ENGAGED -> TEMPLATE_SHOWN -> USER_VENTED -> CLOSED

Handles:
    - Graded response (light/medium/heavy based on severity)
    - Template generation (with context pre-fill from traces)
    - Session persistence in SQLite
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from flowcraft_core.storage.database import Database

logger = logging.getLogger(__name__)


class VentSessionStatus(StrEnum):
    IDLE = "IDLE"
    DETECTED = "DETECTED"
    ENGAGED = "ENGAGED"
    TEMPLATE_SHOWN = "TEMPLATE_SHOWN"
    USER_VENTED = "USER_VENTED"
    CLOSED = "CLOSED"


@dataclass
class VentTemplate:
    """Context-aware vent template with auto-filled fields."""
    task_objective: str = ""
    actual_action: str = ""
    consequence: str = ""
    user_pain_point: str = ""
    user_suggestion: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "task_objective": self.task_objective,
            "actual_action": self.actual_action,
            "consequence": self.consequence,
            "user_pain_point": self.user_pain_point,
            "user_suggestion": self.user_suggestion,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VentTemplate":
        return cls(
            task_objective=data.get("task_objective", ""),
            actual_action=data.get("actual_action", ""),
            consequence=data.get("consequence", ""),
            user_pain_point=data.get("user_pain_point", ""),
            user_suggestion=data.get("user_suggestion", ""),
        )


@dataclass
class VentSession:
    """A single vent session record."""
    id: str
    task_id: str = ""
    session_id: str = ""
    severity: int = 0
    pain_points: list[str] = field(default_factory=list)
    template: VentTemplate | None = None
    selected_phrase_id: str = ""
    insight_generated: str = ""
    mapped_failure_type: str = ""
    status: VentSessionStatus = VentSessionStatus.IDLE
    created_at: str = ""
    closed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "severity": self.severity,
            "pain_points": self.pain_points,
            "template": self.template.to_dict() if self.template else None,
            "selected_phrase_id": self.selected_phrase_id,
            "insight_generated": self.insight_generated,
            "mapped_failure_type": self.mapped_failure_type,
            "status": self.status.value,
            "created_at": self.created_at,
            "closed_at": self.closed_at,
        }

    def to_api_response(
        self, top_phrases: list[dict[str, Any]] | None = None,
        phrases_grouped: dict[str, list[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        """Format session for API response."""
        resp = self.to_dict()
        if top_phrases is not None:
            resp["top_phrases"] = top_phrases
        if phrases_grouped is not None:
            resp["phrases_grouped"] = phrases_grouped
        resp["severity_level"] = (
            "light" if self.severity <= 1 else "medium" if self.severity <= 3 else "heavy"
        )
        return resp


class VentSessionManager:
    """Manages vent session lifecycle with SQLite persistence."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ── Session lifecycle ────────────────────────────────────

    def start_session(
        self,
        session_id: str,
        severity: int = 1,
        task_id: str = "",
        pain_points: list[str] | None = None,
    ) -> VentSession:
        """Create a new vent session."""
        vent_id = f"vent_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()

        self._db.insert_json("vent_sessions", {
            "id": vent_id,
            "task_id": task_id,
            "session_id": session_id,
            "severity": severity,
            "pain_points_json": json.dumps(pain_points or [], ensure_ascii=False),
            "template_filled_json": "{}",
            "selected_phrase_id": "",
            "insight_generated": "",
            "mapped_failure_type": "",
            "status": VentSessionStatus.DETECTED.value,
            "created_at": now,
            "closed_at": None,
        })

        logger.info("Vent session started: %s (severity=%d)", vent_id, severity)
        return VentSession(
            id=vent_id,
            task_id=task_id,
            session_id=session_id,
            severity=severity,
            pain_points=pain_points or [],
            status=VentSessionStatus.DETECTED,
            created_at=now,
        )

    def build_template(
        self,
        vent_session_id: str,
        task_objective: str = "",
        actual_action: str = "",
        consequence: str = "",
    ) -> VentTemplate:
        """Build and attach a context-aware template to the session."""
        tmpl = VentTemplate(
            task_objective=task_objective,
            actual_action=actual_action,
            consequence=consequence,
        )
        self._db.update(
            "vent_sessions", "id", vent_session_id,
            {
                "template_filled_json": json.dumps(tmpl.to_dict(), ensure_ascii=False),
                "status": VentSessionStatus.TEMPLATE_SHOWN.value,
            },
        )
        return tmpl

    @staticmethod
    def extract_context_from_traces(
        events: list[dict], db_task_row: dict | None = None,
    ) -> tuple[str, str, str]:
        """Extract context for vent template from EventRecorder trace events.

        Scans trace events to find:
            1. task_objective — from the task row or step.objective event
            2. actual_action — from the most recent tool.observation or step.failed
            3. consequence — the error message or tool result summary

        Returns (task_objective, actual_action, consequence).
        """
        task_objective = ""
        actual_action = ""
        consequence = ""

        # 1. Get task objective from DB row or trace events
        if db_task_row:
            task_objective = db_task_row.get("objective", "") or ""

        if not task_objective:
            for e in events:
                if e.get("event_type") in ("task.created", "intent.recognized"):
                    task_objective = e.get("message", "") or e.get("title", "")
                    if task_objective:
                        break

        # 2. Find the most recent failure or tool observation
        # Scan in reverse to get the latest relevant event
        for e in reversed(events):
            etype = e.get("event_type", "")

            if etype in ("tool.observation", "step.failed", "tool.error"):
                # Extract what the agent actually did
                title = e.get("title", "") or ""
                msg = e.get("message", "") or ""

                if not actual_action and title:
                    actual_action = title
                elif not actual_action and msg:
                    actual_action = msg[:200]

                # Extract consequence
                if not consequence:
                    # Try to parse payload for error/result
                    payload = e.get("payload", {})
                    if isinstance(payload, str):
                        try:
                            payload = json.loads(payload)
                        except (json.JSONDecodeError, TypeError):
                            payload = {}

                    if isinstance(payload, dict):
                        error = payload.get("error", "") or payload.get("message", "")
                        if error:
                            consequence = str(error)[:300]
                    if not consequence and msg:
                        consequence = msg[:300]

            if etype == "step.failed" and not actual_action:
                actual_action = e.get("message", "")[:200]

            if actual_action and consequence:
                break

        # 3. Fallback to any step-level event
        if not actual_action:
            for e in reversed(events):
                if e.get("event_type", "").startswith("step."):
                    actual_action = e.get("title", "") or e.get("message", "")[:200]
                    if actual_action:
                        break

        if not consequence:
            consequence = actual_action if actual_action else "（未获取到具体错误信息）"

        return task_objective, actual_action, consequence

    def submit_feedback(
        self,
        vent_session_id: str,
        user_pain_point: str = "",
        user_suggestion: str = "",
        selected_phrase_id: str = "",
    ) -> VentSession | None:
        """Submit user's vent feedback, completing the session."""
        session = self.get_session(vent_session_id)
        if not session:
            return None

        # Update template with user input
        if session.template is None:
            session.template = VentTemplate()
        session.template.user_pain_point = user_pain_point
        session.template.user_suggestion = user_suggestion

        now = datetime.now(timezone.utc).isoformat()

        self._db.update("vent_sessions", "id", vent_session_id, {
            "template_filled_json": json.dumps(session.template.to_dict(), ensure_ascii=False),
            "selected_phrase_id": selected_phrase_id,
            "status": VentSessionStatus.USER_VENTED.value,
        })

        session.selected_phrase_id = selected_phrase_id
        session.status = VentSessionStatus.USER_VENTED
        return session

    def close_session(
        self,
        vent_session_id: str,
        insight_generated: str = "",
        mapped_failure_type: str = "",
    ) -> VentSession | None:
        """Close the vent session with insight mapping."""
        session = self.get_session(vent_session_id)
        if not session:
            return None

        now = datetime.now(timezone.utc).isoformat()

        self._db.update("vent_sessions", "id", vent_session_id, {
            "insight_generated": insight_generated,
            "mapped_failure_type": mapped_failure_type,
            "status": VentSessionStatus.CLOSED.value,
            "closed_at": now,
        })

        session.insight_generated = insight_generated
        session.mapped_failure_type = mapped_failure_type
        session.status = VentSessionStatus.CLOSED
        session.closed_at = now
        return session

    # ── Queries ───────────────────────────────────────────────

    def get_session(self, vent_session_id: str) -> VentSession | None:
        """Get a vent session by ID."""
        row = self._db.fetch_one(
            "SELECT * FROM vent_sessions WHERE id = ?", (vent_session_id,))
        if not row:
            return None
        return self._row_to_session(dict(row))

    def list_sessions(
        self, session_id: str | None = None, limit: int = 20
    ) -> list[VentSession]:
        """List recent vent sessions."""
        if session_id:
            rows = self._db.fetch_all(
                "SELECT * FROM vent_sessions WHERE session_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (session_id, limit),
            )
        else:
            rows = self._db.fetch_all(
                "SELECT * FROM vent_sessions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        return [self._row_to_session(dict(r)) for r in rows]

    # ── Helpers ────────────────────────────────────────────────

    @staticmethod
    def _row_to_session(row: dict[str, Any]) -> VentSession:
        pain_points = []
        try:
            pain_points = json.loads(row.get("pain_points_json", "[]") or "[]")
        except (json.JSONDecodeError, TypeError):
            pass

        template = None
        try:
            tmpl_data = json.loads(row.get("template_filled_json", "{}") or "{}")
            if tmpl_data:
                template = VentTemplate.from_dict(tmpl_data)
        except (json.JSONDecodeError, TypeError):
            pass

        return VentSession(
            id=row.get("id", ""),
            task_id=row.get("task_id", ""),
            session_id=row.get("session_id", ""),
            severity=row.get("severity", 0) or 0,
            pain_points=pain_points,
            template=template,
            selected_phrase_id=row.get("selected_phrase_id", ""),
            insight_generated=row.get("insight_generated", ""),
            mapped_failure_type=row.get("mapped_failure_type", ""),
            status=VentSessionStatus(row.get("status", "IDLE")),
            created_at=row.get("created_at", ""),
            closed_at=row.get("closed_at", ""),
        )
