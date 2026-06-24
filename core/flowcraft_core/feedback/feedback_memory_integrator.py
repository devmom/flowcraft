"""FeedbackMemoryIntegrator — persists vent feedback as cross-session memories.

Converts structured vent feedback into durable lessons stored in:
    - SQLite feedback_memories table (indexed)
    - VectorStore general_lessons / task_specific_lessons collections (Phase 3)

Phase 1: SQLite storage with basic retrieval
Phase 2: LLM-based lesson condensation
Phase 3: VectorStore integration
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from flowcraft_core.storage.database import Database

if TYPE_CHECKING:
    from flowcraft_core.models.gateway import ModelGateway

logger = logging.getLogger(__name__)

LLM_CONDENSE_TIMEOUT = 5.0  # seconds


@dataclass
class LessonMemory:
    """A lesson learned from user feedback."""
    id: str
    memory_type: str = "lesson_learned"
    summary: str = ""
    task_type: str = ""
    failure_type: str = ""
    tools_involved: list[str] = field(default_factory=list)
    pain_direction: str = "general"
    severity: int = 0
    occurrence_count: int = 1
    source_vent_session_id: str = ""
    vector_store_collection: str = "general_lessons"
    retrieval_weight: float = 1.0
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "memory_type": self.memory_type,
            "summary": self.summary,
            "task_type": self.task_type,
            "failure_type": self.failure_type,
            "tools_involved": self.tools_involved,
            "pain_direction": self.pain_direction,
            "severity": self.severity,
            "occurrence_count": self.occurrence_count,
            "retrieval_weight": self.retrieval_weight,
            "created_at": self.created_at,
        }


class FeedbackMemoryIntegrator:
    """Integrates vent feedback into the persistent memory system.

    Phase 1: Simple keyword-based summary
    Phase 2: LLM-based condensation for precise, actionable lesson summaries
    """

    def __init__(
        self, db: Database, model_gateway: "ModelGateway | None" = None,
    ) -> None:
        self._db = db
        self._model_gateway = model_gateway

    def set_model_gateway(self, gateway: "ModelGateway") -> None:
        """Inject ModelGateway for LLM-based condensation."""
        self._model_gateway = gateway

    # ── Phase 2: LLM-based lesson condensation ───────────────

    async def condense_lesson(
        self,
        user_complaint: str,
        failure_type: str,
        correction_hint: str,
        task_objective: str = "",
    ) -> str:
        """Use LLM to condense user feedback into an actionable rule.

        Returns a concise rule in the format:
        "在进行[操作类型]时，必须[预防措施]，否则会导致[后果]"

        Falls back to the correction_hint on timeout/error.
        """
        if not self._model_gateway or not self._model_gateway.is_live():
            return correction_hint or self._build_fallback_summary(
                user_complaint, failure_type,
            )

        try:
            messages = [
                {"role": "system", "content": (
                    "You are a knowledge engineer. Convert user complaints into "
                    "concise, actionable rules for an AI assistant to follow. "
                    "Rules must be:\n"
                    "- Specific and executable\n"
                    "- No judgment of the user\n"
                    "- In the format: 'When [performing X], must [precaution Y], "
                    "otherwise [consequence Z]'\n"
                    "- In the same language as the user's complaint\n"
                    "- Maximum 100 characters"
                )},
                {"role": "user", "content": (
                    f"User complaint: \"{user_complaint}\"\n"
                    f"Task: {task_objective or 'unknown'}\n"
                    f"Failure type: {failure_type}\n"
                    f"Suggested fix: {correction_hint}\n\n"
                    f"Condense this into ONE concise, actionable rule."
                )},
            ]

            result = await asyncio.wait_for(
                self._model_gateway.generate_text(
                    messages[-1]["content"],
                    system=messages[0]["content"],
                    max_tokens=200,
                ),
                timeout=LLM_CONDENSE_TIMEOUT,
            )

            summary = result.strip()
            if summary:
                logger.info("LLM condensed lesson: %s", summary[:80])
                return summary

        except asyncio.TimeoutError:
            logger.warning("LLM condensation timed out, using fallback")
        except Exception as exc:
            logger.warning("LLM condensation failed: %s, using fallback", exc)

        return correction_hint or self._build_fallback_summary(
            user_complaint, failure_type,
        )

    @staticmethod
    def _build_fallback_summary(complaint: str, failure_type: str) -> str:
        """Build a fallback summary when LLM is unavailable."""
        if failure_type == "TOOL_ERROR":
            return f"在执行工具操作前，必须验证前置条件和输入参数"
        elif failure_type == "MODEL_PARSE_ERROR":
            return f"在理解用户意图时，必须使用分步骤确认，避免误解"
        elif failure_type == "TIMEOUT":
            return f"在处理大任务时，必须先评估任务规模和资源需求"
        elif failure_type == "PERMISSION_DENIED":
            return f"在执行受限操作前，必须检查权限配置"
        else:
            return f"用户反馈: {complaint[:80]}"

    async def integrate_with_condensation(
        self,
        vent_session_id: str,
        failure_type: str,
        pain_direction: str = "general",
        task_type: str = "",
        severity: int = 1,
        tools_involved: list[str] | None = None,
        pain_point_text: str = "",
        correction_hint: str = "",
        task_objective: str = "",
    ) -> LessonMemory | None:
        """Full integration pipeline: LLM condensation + storage."""
        summary = await self.condense_lesson(
            user_complaint=pain_point_text,
            failure_type=failure_type,
            correction_hint=correction_hint,
            task_objective=task_objective,
        )

        now = datetime.now(timezone.utc).isoformat()
        memory_id = f"fm_{uuid.uuid4().hex[:12]}"

        existing = self._db.fetch_one(
            "SELECT id, occurrence_count, retrieval_weight FROM feedback_memories "
            "WHERE pain_direction = ? AND failure_type = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (pain_direction, failure_type),
        )

        occurrence_count = 1
        retrieval_weight = 1.0
        if existing:
            e = dict(existing)
            occurrence_count = (e.get("occurrence_count", 0) or 0) + 1
            retrieval_weight = min(5.0, 1.0 + (occurrence_count - 1) * 0.5)

        tools_json = json.dumps(tools_involved or [], ensure_ascii=False)
        collection = "general_lessons" if pain_direction == "general" else "task_specific_lessons"

        self._db.insert_json("feedback_memories", {
            "id": memory_id, "memory_type": "lesson_learned",
            "summary": summary, "task_type": task_type,
            "failure_type": failure_type, "tools_involved_json": tools_json,
            "pain_direction": pain_direction, "severity": severity,
            "occurrence_count": occurrence_count,
            "source_vent_session_id": vent_session_id,
            "vector_store_collection": collection,
            "retrieval_weight": retrieval_weight,
            "created_at": now, "last_retrieved_at": None,
        })

        logger.info(
            "Integrated condensed feedback memory %s (pain=%s, failure=%s, "
            "occurrence=%d, weight=%.1f)",
            memory_id, pain_direction, failure_type,
            occurrence_count, retrieval_weight,
        )

        return LessonMemory(
            id=memory_id, summary=summary, task_type=task_type,
            failure_type=failure_type, tools_involved=tools_involved or [],
            pain_direction=pain_direction, severity=severity,
            occurrence_count=occurrence_count,
            source_vent_session_id=vent_session_id,
            vector_store_collection=collection,
            retrieval_weight=retrieval_weight, created_at=now,
        )

    def update_summary(self, memory_id: str, summary: str) -> bool:
        """Update the summary of an existing lesson (e.g., after async condensation)."""
        row = self._db.fetch_one(
            "SELECT id FROM feedback_memories WHERE id = ?", (memory_id,))
        if not row:
            return False
        self._db.update("feedback_memories", "id", memory_id, {"summary": summary})
        return True

    def integrate(
        self,
        vent_session_id: str,
        failure_type: str,
        pain_direction: str = "general",
        task_type: str = "",
        severity: int = 1,
        tools_involved: list[str] | None = None,
        pain_point_text: str = "",
        correction_hint: str = "",
    ) -> LessonMemory | None:
        """Store feedback as a persistent lesson.

        Phase 1: Simple insertion with keyword-based summary.
        Phase 2-3: LLM-based condensation + VectorStore.
        """
        now = datetime.now(timezone.utc).isoformat()
        memory_id = f"fm_{uuid.uuid4().hex[:12]}"

        # Check for existing similar lessons to increment occurrence_count
        existing = self._db.fetch_one(
            "SELECT id, occurrence_count, retrieval_weight FROM feedback_memories "
            "WHERE pain_direction = ? AND failure_type = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (pain_direction, failure_type),
        )

        occurrence_count = 1
        retrieval_weight = 1.0
        if existing:
            e = dict(existing)
            occurrence_count = (e.get("occurrence_count", 0) or 0) + 1
            retrieval_weight = min(5.0, 1.0 + (occurrence_count - 1) * 0.5)

        # Build summary from available info (Phase 2: LLM condensation)
        if correction_hint:
            summary = correction_hint
        elif pain_point_text:
            summary = f"用户反馈: {pain_point_text} (类型: {failure_type})"
        else:
            summary = f"任务执行引发了用户不满 (类型: {failure_type}, 方向: {pain_direction})"

        tools_json = json.dumps(tools_involved or [], ensure_ascii=False)
        collection = "general_lessons" if pain_direction == "general" else "task_specific_lessons"

        self._db.insert_json("feedback_memories", {
            "id": memory_id,
            "memory_type": "lesson_learned",
            "summary": summary,
            "task_type": task_type,
            "failure_type": failure_type,
            "tools_involved_json": tools_json,
            "pain_direction": pain_direction,
            "severity": severity,
            "occurrence_count": occurrence_count,
            "source_vent_session_id": vent_session_id,
            "vector_store_collection": collection,
            "retrieval_weight": retrieval_weight,
            "created_at": now,
            "last_retrieved_at": None,
        })

        logger.info(
            "Integrated feedback memory %s (pain=%s, failure=%s, occurrence=%d, weight=%.1f)",
            memory_id, pain_direction, failure_type, occurrence_count, retrieval_weight,
        )

        return LessonMemory(
            id=memory_id,
            summary=summary,
            task_type=task_type,
            failure_type=failure_type,
            tools_involved=tools_involved or [],
            pain_direction=pain_direction,
            severity=severity,
            occurrence_count=occurrence_count,
            source_vent_session_id=vent_session_id,
            vector_store_collection=collection,
            retrieval_weight=retrieval_weight,
            created_at=now,
        )

    def retrieve_lessons(
        self,
        task_type: str = "",
        failure_type: str = "",
        pain_direction: str = "",
        limit: int = 5,
    ) -> list[LessonMemory]:
        """Retrieve relevant lessons for a given task context."""
        conditions = []
        params: list[Any] = []

        if task_type:
            conditions.append("(task_type = ? OR task_type = '' OR task_type IS NULL)")
            params.append(task_type)
        if failure_type:
            conditions.append("failure_type = ?")
            params.append(failure_type)
        if pain_direction:
            conditions.append("pain_direction = ?")
            params.append(pain_direction)

        where = " AND ".join(conditions) if conditions else "1=1"
        query = (
            f"SELECT * FROM feedback_memories WHERE {where} "
            "ORDER BY retrieval_weight DESC, occurrence_count DESC LIMIT ?"
        )
        params.append(limit)

        rows = self._db.fetch_all(query, tuple(params))
        return [self._row_to_lesson(dict(r)) for r in rows]

    def log_retrieval(self, memory_id: str, task_id: str, was_effective: bool | None = None) -> None:
        """Log that a lesson was retrieved for a task."""
        log_id = f"mrl_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        self._db.insert_json("memory_retrieval_log", {
            "id": log_id,
            "memory_id": memory_id,
            "task_id": task_id,
            "retrieved_at": now,
            "was_effective": was_effective,
        })
        # Update last_retrieved_at
        self._db.update("feedback_memories", "id", memory_id, {"last_retrieved_at": now})

    def adjust_weight(self, memory_id: str, was_effective: bool) -> None:
        """Adjust retrieval weight based on effectiveness."""
        row = self._db.fetch_one(
            "SELECT retrieval_weight FROM feedback_memories WHERE id = ?", (memory_id,))
        if not row:
            return
        current = float(row["retrieval_weight"] or 1.0)
        new_weight = current * 0.9 if was_effective else current * 1.5
        new_weight = max(0.1, min(10.0, new_weight))
        self._db.update("feedback_memories", "id", memory_id, {"retrieval_weight": new_weight})

    @staticmethod
    def _row_to_lesson(row: dict[str, Any]) -> LessonMemory:
        tools = []
        try:
            tools = json.loads(row.get("tools_involved_json", "[]") or "[]")
        except (json.JSONDecodeError, TypeError):
            pass
        return LessonMemory(
            id=row.get("id", ""),
            memory_type=row.get("memory_type", "lesson_learned"),
            summary=row.get("summary", ""),
            task_type=row.get("task_type", ""),
            failure_type=row.get("failure_type", ""),
            tools_involved=tools,
            pain_direction=row.get("pain_direction", "general"),
            severity=row.get("severity", 0) or 0,
            occurrence_count=row.get("occurrence_count", 0) or 0,
            source_vent_session_id=row.get("source_vent_session_id", ""),
            vector_store_collection=row.get("vector_store_collection", "general_lessons"),
            retrieval_weight=float(row.get("retrieval_weight", 1.0) or 1.0),
            created_at=row.get("created_at", ""),
        )
