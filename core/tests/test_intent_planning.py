"""P0: Intent Engine & Planner Tests

Covers: intent/engine (C1), planning/planner + PlanValidator (C2), DAG planner.
"""

from __future__ import annotations

import json
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from flowcraft_core.domain.schemas import (
    AgentRequest, TaskBrief, ExecutionPlan, PlanStep,
)
from flowcraft_core.domain.enums import (
    TaskStatus, RiskLevel, PlanMode, StepStatus,
)
from flowcraft_core.intent.engine import IntentEngine
from flowcraft_core.planning.planner import Planner, PlanValidator


# ═══════════════════════════════════════════════════════════════
# C1: Intent Engine tests
# ═══════════════════════════════════════════════════════════════

class TestIntentEngine:
    """TC-C1: Intent recognition with mock gateway."""

    @pytest.fixture
    def mock_gateway(self) -> MagicMock:
        gw = MagicMock()
        gw.generate_structured = AsyncMock(return_value={
            "task_type": "QA", "objective": "test",
            "risk_level": "LOW", "success_criteria": ["ok"],
            "constraints": [], "target_objects": [],
            "required_capabilities": [],
            "requires_local_files": False, "requires_network": False,
            "requires_tools": False,
            "clarification_required": False, "clarification_questions": [],
            "expected_output_format": "text",
        })
        gw._heuristic_task_brief = MagicMock(return_value={
            "task_type": "QA", "objective": "test",
            "risk_level": "LOW", "success_criteria": ["ok"],
            "constraints": [], "target_objects": [],
            "required_capabilities": [],
            "requires_local_files": False, "requires_network": False,
            "requires_tools": False,
            "clarification_required": False, "clarification_questions": [],
            "expected_output_format": "text",
        })
        return gw

    @pytest.mark.unit
    def test_recognize_returns_task_brief(self, mock_gateway) -> None:
        """recognize() returns a TaskBrief with correct fields."""
        engine = IntentEngine(mock_gateway)
        request = AgentRequest(session_id="s1", raw_input="What is AI?")
        brief = asyncio.run(engine.recognize("task_1", request))
        assert isinstance(brief, TaskBrief)
        assert brief.task_id == "task_1"
        assert brief.task_type == "QA"
        mock_gateway.generate_structured.assert_called_once()

    @pytest.mark.unit
    def test_recognize_timeout_falls_back_to_heuristic(self, mock_gateway) -> None:
        """Timeout triggers heuristic fallback."""
        mock_gateway.generate_structured = AsyncMock(
            side_effect=asyncio.TimeoutError())
        engine = IntentEngine(mock_gateway)
        request = AgentRequest(session_id="s2", raw_input="test")
        brief = asyncio.run(engine.recognize("task_2", request))
        assert isinstance(brief, TaskBrief)
        # Should have called heuristic fallback
        mock_gateway._heuristic_task_brief.assert_called_once()

    @pytest.mark.unit
    def test_recognize_exception_falls_back(self, mock_gateway) -> None:
        """Any exception triggers heuristic fallback."""
        mock_gateway.generate_structured = AsyncMock(
            side_effect=RuntimeError("model down"))
        engine = IntentEngine(mock_gateway)
        request = AgentRequest(session_id="s3", raw_input="test")
        brief = asyncio.run(engine.recognize("task_3", request))
        assert isinstance(brief, TaskBrief)
        mock_gateway._heuristic_task_brief.assert_called_once()


# ═══════════════════════════════════════════════════════════════
# C2: Planner & PlanValidator tests
# ═══════════════════════════════════════════════════════════════

class TestPlanValidator:
    """TC-C2: PlanValidator checks."""

    @pytest.mark.unit
    def test_empty_steps_error(self) -> None:
        """Empty steps list → error."""
        v = PlanValidator()
        plan = ExecutionPlan(
            task_id="t1", mode=PlanMode.LINEAR, goal="test", steps=[])
        errors = v.validate(plan)
        assert len(errors) > 0
        assert any("至少包含一个步骤" in e for e in errors)

    @pytest.mark.unit
    def test_too_many_steps_error(self) -> None:
        """> 20 steps → error."""
        v = PlanValidator()
        steps = [PlanStep(index=i, title=f"S{i}", objective=f"Obj{i}",
                          action_type="TOOL", expected_output="ok",
                          risk_level=RiskLevel.LOW)
                 for i in range(25)]
        plan = ExecutionPlan(
            task_id="t2", mode=PlanMode.LINEAR, goal="big", steps=steps)
        errors = v.validate(plan)
        assert any("20 步" in e for e in errors)

    @pytest.mark.unit
    def test_duplicate_step_index_error(self) -> None:
        """Duplicate indices → error."""
        v = PlanValidator()
        steps = [
            PlanStep(index=1, title="A", objective="a",
                     action_type="TOOL", expected_output="ok",
                     risk_level=RiskLevel.LOW),
            PlanStep(index=1, title="B", objective="b",
                     action_type="TOOL", expected_output="ok",
                     risk_level=RiskLevel.LOW),
        ]
        plan = ExecutionPlan(
            task_id="t3", mode=PlanMode.LINEAR, goal="dup", steps=steps)
        errors = v.validate(plan)
        assert any("重复" in e for e in errors)

    @pytest.mark.unit
    def test_high_risk_step_without_approval_error(self) -> None:
        """HIGH risk without approval_required → error."""
        v = PlanValidator()
        steps = [PlanStep(index=1, title="Danger", objective="do dangerous",
                          action_type="TOOL", expected_output="ok",
                          risk_level=RiskLevel.HIGH, approval_required=False)]
        plan = ExecutionPlan(
            task_id="t4", mode=PlanMode.LINEAR, goal="risky", steps=steps)
        errors = v.validate(plan)
        assert any("审批" in e for e in errors)

    @pytest.mark.unit
    def test_tool_step_without_tools_error(self) -> None:
        """TOOL action_type without required_tools → error."""
        v = PlanValidator()
        steps = [PlanStep(index=1, title="ToolStep", objective="use tool",
                          action_type="TOOL", expected_output="ok",
                          required_tools=[], risk_level=RiskLevel.LOW)]
        plan = ExecutionPlan(
            task_id="t5", mode=PlanMode.LINEAR, goal="notools", steps=steps)
        errors = v.validate(plan)
        assert any("TOOL" in e or "工具" in e for e in errors)

    @pytest.mark.unit
    def test_valid_plan_passes(self) -> None:
        """A well-formed plan passes validation."""
        v = PlanValidator()
        steps = [PlanStep(index=1, title="S1", objective="obj",
                          action_type="TOOL", expected_output="ok",
                          required_tools=["file.read"],
                          risk_level=RiskLevel.LOW)]
        plan = ExecutionPlan(
            task_id="t6", mode=PlanMode.LINEAR, goal="good", steps=steps)
        errors = v.validate(plan)
        assert len(errors) == 0

    @pytest.mark.unit
    def test_missing_title_error(self) -> None:
        """Step without title → error."""
        v = PlanValidator()
        steps = [PlanStep(index=1, title="", objective="obj",
                          action_type="TOOL", expected_output="ok",
                          risk_level=RiskLevel.LOW)]
        plan = ExecutionPlan(
            task_id="t7", mode=PlanMode.LINEAR, goal="notitle", steps=steps)
        errors = v.validate(plan)
        assert any("标题" in e for e in errors)

    @pytest.mark.unit
    def test_depends_on_invalid_reference_error(self) -> None:
        """depends_on referencing non-existent step → error."""
        v = PlanValidator()
        steps = [PlanStep(index=1, title="S1", objective="obj",
                          action_type="TOOL", expected_output="ok",
                          depends_on=[5], risk_level=RiskLevel.LOW)]
        plan = ExecutionPlan(
            task_id="t8", mode=PlanMode.DAG, goal="baddep", steps=steps)
        errors = v.validate(plan)
        assert any("依赖" in e for e in errors)


# ═══════════════════════════════════════════════════════════════
# C3: Planner (heuristic path) tests
# ═══════════════════════════════════════════════════════════════

class TestPlannerHeuristic:
    """Planner heuristic path (no live model)."""

    @pytest.mark.unit
    def test_create_plan_uses_heuristic_when_no_model(self) -> None:
        """When model gateway is not live, heuristic plan is used."""
        mock_gw = MagicMock()
        mock_gw.is_live.return_value = False
        mock_gw._heuristic_plan.return_value = {
            "mode": "DIRECT",
            "goal": "Answer question",
            "steps": [{
                "index": 1, "title": "Answer", "objective": "test",
                "action_type": "MODEL_ANSWER",
                "required_tools": [], "expected_output": "answer",
                "risk_level": "LOW", "approval_required": False,
            }],
        }
        planner = Planner(mock_gw)
        brief = TaskBrief(
            task_id="t_h1", objective="What is Python?",
            task_type="QA", risk_level=RiskLevel.LOW,
        )
        plan = asyncio.run(planner.create_plan(brief))
        assert isinstance(plan, ExecutionPlan)
        assert len(plan.steps) == 1
        assert plan.steps[0].action_type == "MODEL_ANSWER"
