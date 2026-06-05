"""Memory Manager - Session Memory, Task Memory, Cross-Task Context, Decay.

Features:
- Session Memory: 当前会话上下文和最近任务摘要
- Task Memory: 任务执行记录（通过 TraceEvents 实现）
- Cross-Task Context: 同会话内跨任务记忆传递
- Time Decay: 基于指数衰减的记忆置信度调整
- Auto-Expiry: 过期记忆自动清理
"""

from __future__ import annotations

import json
import math
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any
from uuid import uuid4

from flowcraft_core.storage.database import Database

logger = logging.getLogger(__name__)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MemoryEntry:
    memory_id: str = field(default_factory=lambda: _new_id("mem"))
    memory_type: str = "TASK"
    scope_id: str = ""
    title: str = ""
    content: str = ""
    source_type: str = "task"
    source_id: str | None = None
    sensitivity_level: str = "normal"
    confidence: float = 1.0
    expires_at: str | None = None
    created_at: str = field(default_factory=_now_utc)
    updated_at: str = field(default_factory=_now_utc)
    deleted_at: str | None = None


MEMORY_TYPES = frozenset({"WORKING", "SESSION", "TASK", "LONG_TERM", "KNOWLEDGE"})
SENSITIVITY_LEVELS = frozenset({"normal", "sensitive", "confidential"})

# Decay config
DEFAULT_DECAY_HALF_LIFE_HOURS = 24.0
DEFAULT_MIN_CONFIDENCE = 0.1
DEFAULT_MAX_MEMORIES_PER_SESSION = 200
DEFAULT_MEMORY_TTL_HOURS = 168  # 7 days


class MemoryManager:
    """统一记忆管理入口。支持衰减、过期、跨任务检索。"""

    def __init__(self, db: Database) -> None:
        self._db = db
        self.decay_half_life_hours: float = DEFAULT_DECAY_HALF_LIFE_HOURS
        self.min_confidence: float = DEFAULT_MIN_CONFIDENCE
        self.memory_ttl_hours: float = DEFAULT_MEMORY_TTL_HOURS

    # ── Session Memory ──────────────────────────────────────

    def remember_session(
        self, session_id: str, title: str, content: str,
        ttl_hours: float | None = None,
    ) -> MemoryEntry:
        """写入一条会话记忆，可选 TTL 过期时间."""
        ttl = ttl_hours if ttl_hours is not None else self.memory_ttl_hours
        expires = (datetime.now(timezone.utc) + timedelta(hours=ttl)).isoformat()

        entry = MemoryEntry(
            memory_type="SESSION",
            scope_id=session_id,
            title=title,
            content=content,
            source_type="session",
            source_id=session_id,
            expires_at=expires,
        )
        self._db.insert_json("memories", {
            "id": entry.memory_id,
            "memory_type": entry.memory_type,
            "scope_id": entry.scope_id,
            "title": entry.title,
            "content": entry.content,
            "source_type": entry.source_type,
            "source_id": entry.source_id,
            "sensitivity_level": entry.sensitivity_level,
            "confidence": entry.confidence,
            "expires_at": entry.expires_at,
            "created_at": entry.created_at,
            "updated_at": entry.updated_at,
            "deleted_at": entry.deleted_at,
        })

        # 防止单会话记忆膨胀
        self._enforce_memory_cap(session_id)

        return entry

    def get_session_memories(
        self, session_id: str, max_count: int = 20, apply_decay: bool = True
    ) -> list[dict]:
        """获取会话记忆，自动过滤过期，可选时间衰减."""
        now = datetime.now(timezone.utc).isoformat()
        rows = self._db.fetch_all(
            "SELECT * FROM memories WHERE memory_type = 'SESSION' "
            "AND scope_id = ? AND deleted_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > ?) "
            "ORDER BY created_at DESC LIMIT ?",
            (session_id, now, max_count),
        )
        memories = [dict(row) for row in rows]
        if apply_decay:
            memories = self._apply_decay_to_memories(memories)
        return memories

    def get_session_memories_semantic(
        self, session_id: str, query: str, top_k: int = 10
    ) -> list[dict]:
        """语义检索同会话记忆（通过向量存储）。"""
        from flowcraft_core.memory.vector_store import get_vector_store, IndexedMemory
        vs = get_vector_store()
        results = vs.search(query, scope_id=session_id, memory_type="SESSION", top_k=top_k)
        return [
            {
                "id": m.memory_id, "title": m.title, "content": m.content,
                "created_at": m.created_at, "_score": m.effective_score,
            }
            for m in results
        ]

    # ── Cross-Task Context ──────────────────────────────────

    def get_cross_task_context(self, session_id: str, current_task_id: str) -> str:
        """获取同会话内之前任务的摘要，供新任务使用。

        查询最近完成的 3 个任务（排除当前任务），提取它们的标题、目标和关键步骤输出。
        """
        rows = self._db.fetch_all(
            "SELECT id, title, objective, status, created_at "
            "FROM tasks WHERE session_id = ? AND id != ? AND status IN ('COMPLETED', 'FAILED') "
            "ORDER BY created_at DESC LIMIT 3",
            (session_id, current_task_id),
        )
        if not rows:
            return ""

        parts = ["## 同会话前序任务摘要"]
        for i, row in enumerate(rows):
            task_dict = dict(row)
            tid = task_dict["id"]
            title = task_dict.get("title", "")[:80]
            objective = task_dict.get("objective", "")[:200]
            status = task_dict.get("status", "")
            status_icon = "✅" if status == "COMPLETED" else "❌"

            # 获取该任务的步骤输出摘要
            step_outputs = self._get_task_step_outputs(tid)

            parts.append(
                f"### 前序任务 {i + 1}: {status_icon} {title}\n"
                f"目标: {objective}\n"
                f"关键输出: {step_outputs}"
            )

        return "\n".join(parts)

    def _get_task_step_outputs(self, task_id: str) -> str:
        """获取任务的步骤输出摘要（从 memories 表中提取）。"""
        rows = self._db.fetch_all(
            "SELECT title, content FROM memories "
            "WHERE source_type = 'step' AND source_id LIKE ? AND deleted_at IS NULL "
            "ORDER BY created_at ASC LIMIT 10",
            (f"{task_id}%",),
        )
        if not rows:
            return "(无详细记录)"
        summaries = []
        for row in rows[:5]:
            d = dict(row)
            summaries.append(f"- {d['title']}: {d['content'][:150]}")
        return "\n".join(summaries)

    # ── Decay & Expiry ──────────────────────────────────────

    @staticmethod
    def calc_decay_factor(created_at: str, half_life_hours: float = DEFAULT_DECAY_HALF_LIFE_HOURS) -> float:
        """计算指数衰减因子。

        factor = 2^(-age_hours / half_life_hours)
        24小时半衰期 → 1天后置信度降至50%, 7天后降至 ~1%
        """
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600.0
            if age_hours <= 0:
                return 1.0
            return max(DEFAULT_MIN_CONFIDENCE, math.pow(2.0, -age_hours / half_life_hours))
        except (ValueError, TypeError):
            return 1.0

    def _apply_decay_to_memories(self, memories: list[dict]) -> list[dict]:
        """对记忆列表应用时间衰减，调整 confidence 字段."""
        for mem in memories:
            original_conf = mem.get("confidence", 1.0)
            decay = self.calc_decay_factor(
                mem.get("created_at", ""), self.decay_half_life_hours)
            mem["_original_confidence"] = original_conf
            mem["_decay_factor"] = decay
            mem["confidence"] = round(original_conf * decay, 4)
        return memories

    def purge_expired_memories(self) -> int:
        """软删除所有过期的记忆。返回清理数量。"""
        now = datetime.now(timezone.utc).isoformat()
        # 先统计
        rows = self._db.fetch_all(
            "SELECT id FROM memories WHERE deleted_at IS NULL "
            "AND expires_at IS NOT NULL AND expires_at <= ?",
            (now,),
        )
        count = len(rows)
        if count > 0:
            self._db.execute(
                "UPDATE memories SET deleted_at = ? "
                "WHERE deleted_at IS NULL AND expires_at IS NOT NULL AND expires_at <= ?",
                (_now_utc(), now),
            )
            logger.info("Purged %d expired memories", count)
        return count

    def _enforce_memory_cap(self, session_id: str) -> None:
        """限制每个会话的记忆数量，超出部分软删除最旧的."""
        rows = self._db.fetch_all(
            "SELECT id FROM memories WHERE memory_type = 'SESSION' "
            "AND scope_id = ? AND deleted_at IS NULL ORDER BY created_at ASC",
            (session_id,),
        )
        excess = len(rows) - DEFAULT_MAX_MEMORIES_PER_SESSION
        if excess > 0:
            for row in rows[:excess]:
                self._db.update("memories", "id", dict(row)["id"],
                               {"deleted_at": _now_utc()})
            logger.debug("Capped %d excess memories for session %s", excess, session_id[:20])

    # ── Init / Maintenance ──────────────────────────────────

    def startup_maintenance(self) -> dict[str, int]:
        """启动时执行记忆维护：清理过期 + 向量索引重建."""
        purged = self.purge_expired_memories()
        # 将现有记忆加载到向量存储
        indexed = self._rebuild_vector_index()
        return {"purged": purged, "indexed": indexed}

    def _rebuild_vector_index(self) -> int:
        """重建向量索引：将所有未删除记忆加载到 KeywordVectorStore."""
        from flowcraft_core.memory.vector_store import get_vector_store, IndexedMemory
        vs = get_vector_store()
        rows = self._db.fetch_all(
            "SELECT * FROM memories WHERE deleted_at IS NULL ORDER BY created_at ASC",
        )
        for row in rows:
            d = dict(row)
            im = IndexedMemory(
                memory_id=d["id"],
                memory_type=d.get("memory_type", "SESSION"),
                scope_id=d.get("scope_id", ""),
                title=d.get("title", ""),
                content=d.get("content", ""),
                created_at=d.get("created_at", ""),
                confidence=d.get("confidence", 1.0),
            )
            vs.index(im)
        logger.info("Rebuilt vector index with %d memories", len(rows))
        return len(rows)

    # ── Task Memory ─────────────────────────────────────────

    def get_task_context(self, task_id: str) -> dict[str, Any]:
        """获取任务的完整上下文（TaskBrief + Plan + Steps + Events）。"""
        context: dict[str, Any] = {"task_id": task_id}

        # TaskBrief
        brief_row = self._db.fetch_one(
            "SELECT * FROM task_briefs WHERE task_id = ?", (task_id,)
        )
        if brief_row:
            context["brief"] = json.loads(dict(brief_row).get("data_json", "{}"))

        # Latest plan and steps
        plan_row = self._db.fetch_one(
            "SELECT * FROM plans WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        )
        if plan_row:
            plan = dict(plan_row)
            plan["data"] = json.loads(plan.pop("data_json", "{}"))
            context["plan"] = plan
            steps_rows = self._db.fetch_all(
                "SELECT * FROM plan_steps WHERE plan_id = ? ORDER BY step_index ASC",
                (plan["id"],),
            )
            context["steps"] = [
                {**dict(row), "data": json.loads(dict(row).get("data_json", "{}"))}
                for row in steps_rows
            ]

        # Recent events
        events_rows = self._db.fetch_all(
            "SELECT * FROM trace_events WHERE task_id = ? ORDER BY created_at DESC LIMIT 20",
            (task_id,),
        )
        context["recent_events"] = [dict(row) for row in events_rows]

        return context

    # ── Generic CRUD ────────────────────────────────────────

    def write_memory(self, entry: MemoryEntry) -> MemoryEntry:
        self._db.insert_json("memories", {
            "id": entry.memory_id,
            "memory_type": entry.memory_type,
            "scope_id": entry.scope_id,
            "title": entry.title,
            "content": entry.content,
            "source_type": entry.source_type,
            "source_id": entry.source_id,
            "sensitivity_level": entry.sensitivity_level,
            "confidence": entry.confidence,
            "expires_at": entry.expires_at,
            "created_at": entry.created_at,
            "updated_at": entry.updated_at,
            "deleted_at": entry.deleted_at,
        })
        return entry

    def list_memories(
        self, memory_type: str | None = None, scope_id: str | None = None
    ) -> list[dict]:
        query = "SELECT * FROM memories WHERE deleted_at IS NULL"
        params: list[Any] = []
        if memory_type:
            query += " AND memory_type = ?"
            params.append(memory_type)
        if scope_id:
            query += " AND scope_id = ?"
            params.append(scope_id)
        query += " ORDER BY created_at DESC LIMIT 100"
        rows = self._db.fetch_all(query, tuple(params))
        return [dict(row) for row in rows]

    def soft_delete(self, memory_id: str) -> None:
        self._db.update("memories", "id", memory_id, {"deleted_at": _now_utc()})

    def clear_all(self) -> None:
        self._db.update("memories", None, None, {"deleted_at": _now_utc()})
