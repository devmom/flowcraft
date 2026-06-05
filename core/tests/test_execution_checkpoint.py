"""P0: Execution Checkpoint Tests — save, load, resume, cleanup."""

from __future__ import annotations

import json
import pytest

from flowcraft_core.execution.checkpoint import (
    CheckpointManager, TaskCheckpoint, _truncate_payload,
)
from flowcraft_core.domain.schemas import (
    ExecutionPlan, PlanStep, ToolObservation, ToolIntent,
)
from flowcraft_core.domain.enums import RiskLevel, PlanMode


def make_plan() -> ExecutionPlan:
    return ExecutionPlan(
        task_id="task_cp1", mode=PlanMode.LINEAR, goal="Test",
        steps=[PlanStep(
            index=1, title="Step 1", objective="Do something",
            action_type="TOOL", expected_output="done", risk_level=RiskLevel.LOW,
        )],
    )


def make_obs(task_id: str = "task_cp1", step_id: str = "s1", status: str = "COMPLETED") -> ToolObservation:
    intent = ToolIntent(
        task_id=task_id, step_id=step_id, tool_name="test.tool",
        purpose="test", input_summary="x", input_payload={},
        expected_result="ok",
    )
    return ToolObservation(
        tool_intent_id=intent.tool_intent_id, task_id=task_id, step_id=step_id,
        status=status, output_summary="Done",
        output_payload={"result": "ok"},
    )


class TestCheckpointManager:
    """TC-D4: Checkpoint save/load/prune."""

    @pytest.mark.unit
    def test_save_checkpoint_increments_idx(self, tmp_database) -> None:
        """Each save increments checkpoint_idx."""
        mgr = CheckpointManager(tmp_database)
        plan = make_plan()
        c1 = mgr.save("task_cp1", [1, 2], 3, [make_obs()], "ctx1", plan)
        c2 = mgr.save("task_cp1", [1, 2, 3], 4, [make_obs()], "ctx2", plan)
        assert c1.checkpoint_idx == 0
        assert c2.checkpoint_idx == 1

    @pytest.mark.unit
    def test_load_latest_returns_most_recent(self, tmp_database) -> None:
        """load_latest returns the highest checkpoint_idx."""
        mgr = CheckpointManager(tmp_database)
        plan = make_plan()
        mgr.save("task_cp2", [1], 2, [], "first")
        mgr.save("task_cp2", [1, 2], 3, [], "second")
        mgr.save("task_cp2", [1, 2, 3], 4, [], "latest", plan)
        latest = mgr.load_latest("task_cp2")
        assert latest is not None
        assert latest.context_summary == "latest"
        assert latest.checkpoint_idx == 2
        assert latest.completed_step_indices == [1, 2, 3]

    @pytest.mark.unit
    def test_load_latest_nonexistent_returns_none(self, tmp_database) -> None:
        """load_latest for unknown task returns None."""
        mgr = CheckpointManager(tmp_database)
        assert mgr.load_latest("no_such_task") is None

    @pytest.mark.unit
    def test_load_by_index(self, tmp_database) -> None:
        """load_by_index retrieves a specific checkpoint."""
        mgr = CheckpointManager(tmp_database)
        mgr.save("task_cp3", [1], 2, [], "idx0")
        mgr.save("task_cp3", [1, 2], 3, [], "idx1")
        c = mgr.load_by_index("task_cp3", 0)
        assert c is not None
        assert c.context_summary == "idx0"

    @pytest.mark.unit
    def test_list_checkpoints(self, tmp_database) -> None:
        """list_checkpoints returns metadata for all checkpoints."""
        mgr = CheckpointManager(tmp_database)
        mgr.save("task_cp4", [1], 2, [], "a")
        mgr.save("task_cp4", [1, 2], 3, [], "b")
        items = mgr.list_checkpoints("task_cp4")
        assert len(items) == 2
        assert items[0]["checkpoint_idx"] == 0
        assert items[1]["checkpoint_idx"] == 1

    @pytest.mark.unit
    def test_delete_for_task(self, tmp_database) -> None:
        """delete_for_task removes all checkpoints for a task."""
        mgr = CheckpointManager(tmp_database)
        mgr.save("task_del", [1], 1, [], "x")
        assert mgr.load_latest("task_del") is not None
        mgr.delete_for_task("task_del")
        assert mgr.load_latest("task_del") is None

    @pytest.mark.unit
    def test_prune_keeps_max_checkpoints(self, tmp_database) -> None:
        """Only last MAX_CHECKPOINTS_PER_TASK are kept."""
        mgr = CheckpointManager(tmp_database)
        for i in range(CheckpointManager.MAX_CHECKPOINTS_PER_TASK + 5):
            mgr.save("task_prune", [i], i + 1, [], f"ckpt_{i}")
        items = mgr.list_checkpoints("task_prune")
        assert len(items) <= CheckpointManager.MAX_CHECKPOINTS_PER_TASK


class TestTaskCheckpoint:
    """TaskCheckpoint dataclass tests."""

    @pytest.mark.unit
    def test_auto_generates_id(self) -> None:
        ckpt = TaskCheckpoint(task_id="t1")
        assert ckpt.id.startswith("ckpt_")
        assert len(ckpt.id) > 10

    @pytest.mark.unit
    def test_defaults(self) -> None:
        ckpt = TaskCheckpoint(task_id="t2")
        assert ckpt.checkpoint_idx == 0
        assert ckpt.current_step_index == 0
        assert ckpt.completed_step_indices == []
        assert ckpt.observation_snapshot == []


class TestTruncatePayload:
    """Payload truncation helper."""

    @pytest.mark.unit
    def test_short_string_not_truncated(self) -> None:
        r = _truncate_payload({"k": "short"}, 100)
        assert r["k"] == "short"

    @pytest.mark.unit
    def test_long_string_truncated(self) -> None:
        r = _truncate_payload({"k": "x" * 5000}, 2000)
        assert len(str(r["k"])) < 3000
        assert "total chars" in r["k"]

    @pytest.mark.unit
    def test_nested_dict_truncated(self) -> None:
        r = _truncate_payload({"outer": {"inner": "x" * 5000}}, 2000)
        assert "total chars" in str(r["outer"]["inner"])
