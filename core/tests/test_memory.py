"""P0: Memory System Tests — Manager, decay, semantic search."""

from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta

import pytest

from flowcraft_core.memory.manager import (
    MemoryManager, MemoryEntry, MEMORY_TYPES, SENSITIVITY_LEVELS,
    DEFAULT_DECAY_HALF_LIFE_HOURS, DEFAULT_MIN_CONFIDENCE,
)


class TestMemoryEntry:
    """MemoryEntry dataclass tests."""

    @pytest.mark.unit
    def test_auto_generates_id(self) -> None:
        entry = MemoryEntry(title="test", content="hello")
        assert entry.memory_id.startswith("mem_")

    @pytest.mark.unit
    def test_defaults(self) -> None:
        entry = MemoryEntry(title="t", content="c")
        assert entry.memory_type == "TASK"
        assert entry.sensitivity_level == "normal"
        assert entry.confidence == 1.0


class TestMemoryManager:
    """TC-G1: Session memory, isolation, decay."""

    @pytest.mark.unit
    def test_remember_session_stores_memory(self, tmp_database) -> None:
        """remember_session stores a session memory."""
        mgr = MemoryManager(tmp_database)
        entry = mgr.remember_session("sess_a", "Title", "Content body")
        assert entry.title == "Title"
        assert entry.content == "Content body"
        assert entry.memory_type == "SESSION"

    @pytest.mark.unit
    def test_get_session_memories_returns_stored(self, tmp_database) -> None:
        """get_session_memories lists stored memories for a session."""
        mgr = MemoryManager(tmp_database)
        mgr.remember_session("sess_x", "M1", "Content 1")
        mgr.remember_session("sess_x", "M2", "Content 2")
        memories = mgr.get_session_memories("sess_x")
        assert len(memories) == 2
        titles = {m["title"] for m in memories}
        assert titles == {"M1", "M2"}

    @pytest.mark.unit
    def test_session_isolation(self, tmp_database) -> None:
        """Session A memories are not visible from Session B."""
        mgr = MemoryManager(tmp_database)
        mgr.remember_session("sess_a", "A", "a")
        mgr.remember_session("sess_b", "B", "b")
        a_mems = mgr.get_session_memories("sess_a")
        b_mems = mgr.get_session_memories("sess_b")
        assert len(a_mems) == 1
        assert len(b_mems) == 1
        assert a_mems[0]["title"] == "A"
        assert b_mems[0]["title"] == "B"

    @pytest.mark.unit
    def test_purge_expired_memories(self, tmp_database) -> None:
        """Expired memories are purged."""
        mgr = MemoryManager(tmp_database)
        # Create an already-expired memory
        past = (datetime.now(timezone.utc) - timedelta(hours=999)).isoformat()
        tmp_database.insert_json("memories", {
            "id": "mem_expired", "memory_type": "SESSION",
            "scope_id": "sess_old", "title": "Old", "content": "old",
            "source_type": "session", "source_id": "sess_old",
            "sensitivity_level": "normal", "confidence": 1.0,
            "expires_at": past, "created_at": past, "updated_at": past,
            "deleted_at": None,
        })
        purged = mgr.purge_expired_memories()
        assert purged >= 1

    @pytest.mark.unit
    def test_calc_decay_factor_recent(self) -> None:
        """Recently created memory has factor near 1.0."""
        now = datetime.now(timezone.utc).isoformat()
        factor = MemoryManager.calc_decay_factor(now)
        assert 0.99 <= factor <= 1.01

    @pytest.mark.unit
    def test_calc_decay_factor_old(self) -> None:
        """Old memory has low decay factor."""
        old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
        factor = MemoryManager.calc_decay_factor(old, half_life_hours=24)
        assert factor < 0.3  # 3 half-lives => 1/8

    @pytest.mark.unit
    def test_enforce_memory_cap(self, tmp_database) -> None:
        """Memory cap prevents session overflow (test verifies cap runs without error)."""
        mgr = MemoryManager(tmp_database)
        # Insert many memories to trigger the cap enforcement
        for i in range(250):  # > DEFAULT_MAX_MEMORIES_PER_SESSION (200)
            mgr.remember_session("sess_full", f"M{i}", f"content {i}")
        memories = mgr.get_session_memories("sess_full", max_count=300)
        assert len(memories) <= 200  # capped at DEFAULT_MAX_MEMORIES_PER_SESSION

    @pytest.mark.unit
    def test_cross_task_context_empty(self, tmp_database) -> None:
        """Empty session returns empty context."""
        mgr = MemoryManager(tmp_database)
        ctx = mgr.get_cross_task_context("no_session", "task_x")
        assert ctx == ""


class TestMemoryConstants:
    """Constant validation."""

    @pytest.mark.unit
    def test_memory_types(self) -> None:
        assert "SESSION" in MEMORY_TYPES
        assert "TASK" in MEMORY_TYPES
        assert "LONG_TERM" in MEMORY_TYPES

    @pytest.mark.unit
    def test_sensitivity_levels(self) -> None:
        assert "normal" in SENSITIVITY_LEVELS
        assert "confidential" in SENSITIVITY_LEVELS
