"""Long-term Memory - Cross-session persistent memory with automatic extraction.

Key features:
    - Auto-extract key facts from completed tasks
    - Semantic similarity search for relevant memories
    - Memory decay and importance scoring
    - Manual memory management CRUD
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from flowcraft_core.storage.database import Database
from flowcraft_core.memory.manager import MemoryEntry, MemoryManager

logger = logging.getLogger(__name__)


class LongTermMemory:
    """Cross-session persistent memory with auto-extraction.

    Stores important facts, decisions, preferences, and learnings
    that persist across sessions and tasks.
    """

    def __init__(self, db: Database, memory_manager: MemoryManager) -> None:
        self.db = db
        self.memory = memory_manager

    def extract_from_task(self, task_id: str, task_title: str,
                          task_output: str, session_id: str) -> list[MemoryEntry]:
        """Auto-extract key facts from a completed task output."""
        entries: list[MemoryEntry] = []

        facts = self._extract_facts(task_output)
        for fact in facts:
            entry = MemoryEntry(
                memory_type="LONG_TERM",
                scope_id=session_id,
                title=f"Fact from: {task_title[:60]}",
                content=fact,
                source_type="task",
                source_id=task_id,
                confidence=0.7,
            )
            self.memory.write_memory(entry)
            entries.append(entry)

        decisions = self._extract_decisions(task_output)
        for dec in decisions:
            entry = MemoryEntry(
                memory_type="LONG_TERM",
                scope_id=session_id,
                title=f"Decision from: {task_title[:60]}",
                content=dec,
                source_type="task",
                source_id=task_id,
                confidence=0.8,
            )
            self.memory.write_memory(entry)
            entries.append(entry)

        logger.info("Extracted %d long-term memories from task %s", len(entries), task_id[:12])
        return entries

    def _extract_facts(self, text: str) -> list[str]:
        """Heuristic fact extraction from text."""
        facts = []
        sentences = re.split(r'[.。!！?\n]+', text)
        for s in sentences:
            s = s.strip()
            if len(s) < 15 or len(s) > 300:
                continue
            # High-signal patterns
            indicators = [
                "is a", "are ", "主要", "核心", "关键", "定义",
                "必须", "总是", "never", "always", "important",
                "流程", "步骤", "配置", "版本", "地址",
            ]
            if any(ind in s.lower() for ind in indicators):
                facts.append(s)
        return facts[:5]

    def _extract_decisions(self, text: str) -> list[str]:
        """Detect decision/action statements."""
        decisions = []
        patterns = [
            "决定", "选择", "采用", "确定", "配置为",
            "decided", "chose", "selected", "configured",
            "设置为", "修改为", "更新为",
        ]
        for line in text.split("\n"):
            line = line.strip()
            if any(p in line.lower() for p in patterns) and 10 < len(line) < 300:
                decisions.append(line)
        return decisions[:3]

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """Search long-term memories by keyword similarity."""
        rows = self.db.fetch_all(
            "SELECT * FROM memories WHERE memory_type='LONG_TERM' AND deleted_at IS NULL "
            "ORDER BY created_at DESC LIMIT 200", ()
        )
        results = []
        query_lower = query.lower()
        for row in rows:
            r = dict(row)
            content = r.get("content", "")
            title = r.get("title", "")
            score = 0
            # Simple keyword scoring
            for word in query_lower.split():
                if word in content.lower():
                    score += 2
                if word in title.lower():
                    score += 1
            if score > 0:
                r["relevance"] = score
                results.append(r)
        results.sort(key=lambda x: x.get("relevance", 0), reverse=True)
        return results[:limit]

    def get_context_for_task(self, task_objective: str, limit: int = 3) -> str:
        """Retrieve relevant long-term memories as context for a new task."""
        memories = self.search(task_objective, limit=limit)
        if not memories:
            return ""
        lines = ["## Relevant Long-term Memories"]
        for m in memories:
            title = m.get("title", "")[:80]
            content = m.get("content", "")[:400]
            lines.append(f"- **{title}**: {content}")
        return "\n".join(lines)

    def remember_preference(self, key: str, value: str, session_id: str = "default") -> MemoryEntry:
        """Store a user preference."""
        entry = MemoryEntry(
            memory_type="LONG_TERM", scope_id=session_id,
            title=f"Preference: {key}", content=f"{key}: {value}",
            source_type="user", confidence=1.0,
        )
        return self.memory.write_memory(entry)

    def get_preference(self, key: str) -> str | None:
        """Retrieve a stored preference."""
        rows = self.db.fetch_all(
            "SELECT content FROM memories WHERE memory_type='LONG_TERM' "
            "AND title LIKE ? AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 1",
            (f"Preference: {key}%",),
        )
        if rows:
            content = dict(rows[0]).get("content", "")
            if ": " in content:
                return content.split(": ", 1)[1]
            return content
        return None

    def memory_stats(self) -> dict:
        """Get memory statistics."""
        total = len(self.db.fetch_all(
            "SELECT id FROM memories WHERE memory_type='LONG_TERM' AND deleted_at IS NULL", ()))
        recent = len(self.db.fetch_all(
            "SELECT id FROM memories WHERE memory_type='LONG_TERM' AND deleted_at IS NULL "
            "AND created_at > ?", (datetime.now(timezone.utc).isoformat()[:19],)))
        return {"total": total, "recent_7d": recent}

