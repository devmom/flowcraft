"""Phase 4: Policy & Approval Tests

Covers: F1 policy/engine, F2 approval/manager.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from flowcraft_core.policy.engine import PolicyEngine
from flowcraft_core.approval.manager import ApprovalManager
from flowcraft_core.domain.enums import (
    PolicyDecisionValue, RiskLevel, ApprovalStatus, PlanMode, StepStatus,
)
from flowcraft_core.domain.schemas import (
    ExecutionPlan, PlanStep, ToolIntent, PolicyDecision,
)


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def make_plan(steps_risk: list[tuple[str, RiskLevel]]) -> ExecutionPlan:
    """Build an ExecutionPlan with given (title, risk) steps."""
    return ExecutionPlan(
        task_id="task_1",
        mode=PlanMode.LINEAR,
        goal="Test plan",
        steps=[
            PlanStep(
                index=i, title=title, objective=title,
                action_type="TOOL", expected_output="done",
                risk_level=risk,
            )
            for i, (title, risk) in enumerate(steps_risk)
        ],
    )


def make_intent(risk: RiskLevel = RiskLevel.LOW, tool_name: str = "test.tool") -> ToolIntent:
    return ToolIntent(
        task_id="task_x", step_id="step_1",
        tool_name=tool_name, purpose="test",
        input_summary="test", input_payload={},
        expected_result="ok", risk_level=risk,
    )


# ═══════════════════════════════════════════════════════════════
# F1: Policy Engine tests
# ═══════════════════════════════════════════════════════════════

class TestPolicyEngine:
    """TC-F1: Policy decision on plans and tool intents."""

    # TC-F1-01
    @pytest.mark.unit
    def test_low_risk_plan_allowed(self) -> None:
        """All-LOW plan → ALLOW."""
        engine = PolicyEngine()
        plan = make_plan([("Step 1", RiskLevel.LOW), ("Step 2", RiskLevel.LOW)])
        decision = engine.check_plan("task_1", plan)
        assert decision.decision == PolicyDecisionValue.ALLOW
        assert "未发现" in decision.reason

    # TC-F1-02
    @pytest.mark.unit
    def test_high_risk_plan_requires_approval(self) -> None:
        """Plan with CRITICAL step → REQUIRE_APPROVAL."""
        engine = PolicyEngine()
        plan = make_plan([("Safe step", RiskLevel.LOW), ("Danger!", RiskLevel.CRITICAL)])
        decision = engine.check_plan("task_2", plan)
        assert decision.decision == PolicyDecisionValue.REQUIRE_APPROVAL
        assert "高风险" in decision.reason or "high" in decision.reason.lower()

    # TC-F1-02b
    @pytest.mark.unit
    def test_high_risk_step_triggers_approval(self) -> None:
        """HIGH risk step also triggers REQUIRE_APPROVAL."""
        engine = PolicyEngine()
        plan = make_plan([("Risky", RiskLevel.HIGH)])
        decision = engine.check_plan("task_3", plan)
        assert decision.decision == PolicyDecisionValue.REQUIRE_APPROVAL

    # TC-F1-03
    @pytest.mark.unit
    def test_low_risk_tool_intent_allowed(self) -> None:
        """LOW risk tool → ALLOW."""
        engine = PolicyEngine()
        intent = make_intent(RiskLevel.LOW)
        decision = engine.check_tool_intent(intent)
        assert decision.decision == PolicyDecisionValue.ALLOW

    # TC-F1-04
    @pytest.mark.unit
    def test_high_risk_tool_requires_approval(self) -> None:
        """HIGH risk tool → REQUIRE_APPROVAL."""
        engine = PolicyEngine()
        intent = make_intent(RiskLevel.HIGH, "command.run")
        decision = engine.check_tool_intent(intent)
        assert decision.decision == PolicyDecisionValue.REQUIRE_APPROVAL

    # TC-F1-05
    @pytest.mark.unit
    def test_critical_tool_requires_approval(self) -> None:
        """CRITICAL risk tool → REQUIRE_APPROVAL."""
        engine = PolicyEngine()
        intent = make_intent(RiskLevel.CRITICAL, "system.destroy")
        decision = engine.check_tool_intent(intent)
        assert decision.decision == PolicyDecisionValue.REQUIRE_APPROVAL

    # TC-F1-06
    @pytest.mark.unit
    def test_trusted_session_auto_approves(self) -> None:
        """Trusted session bypasses approval for HIGH risk tools."""
        engine = PolicyEngine()
        engine.trust_session("trusted_123")
        intent = make_intent(RiskLevel.HIGH, "command.run")
        decision = engine.check_tool_intent(intent, session_id="trusted_123")
        assert decision.decision == PolicyDecisionValue.ALLOW
        assert "trusted" in decision.reason.lower()

    # TC-F1-07
    @pytest.mark.unit
    def test_medium_risk_tool_allowed_by_default(self) -> None:
        """MEDIUM risk tool is auto-allowed (not blocked by approval check)."""
        engine = PolicyEngine()
        intent = make_intent(RiskLevel.MEDIUM, "web.search")
        decision = engine.check_tool_intent(intent)
        assert decision.decision == PolicyDecisionValue.ALLOW

    # TC-F1-08
    @pytest.mark.unit
    def test_matched_rules_populated(self) -> None:
        """Policy decisions include matched rule names."""
        engine = PolicyEngine()
        plan = make_plan([("Step", RiskLevel.LOW)])
        decision = engine.check_plan("t", plan)
        assert len(decision.matched_rules) > 0
        assert isinstance(decision.matched_rules[0], str)


# ═══════════════════════════════════════════════════════════════
# F2: Approval Manager tests
# ═══════════════════════════════════════════════════════════════

class TestApprovalManager:
    """TC-F2: Approval request creation and lifecycle."""

    # TC-F2-01
    @pytest.mark.unit
    def test_create_from_policy(self) -> None:
        """create_from_policy produces a PENDING ApprovalRequest."""
        mgr = ApprovalManager()
        decision = PolicyDecision(
            task_id="task_a",
            target_type="tool_intent",
            target_id="intent_1",
            decision=PolicyDecisionValue.REQUIRE_APPROVAL,
            reason="High risk detected",
            risk_level=RiskLevel.HIGH,
        )
        approval = mgr.create_from_policy(
            decision,
            title="Confirm action",
            description="This action is risky",
        )
        assert approval.status == ApprovalStatus.PENDING
        assert approval.task_id == "task_a"
        assert approval.action_title == "Confirm action"
        assert approval.risk_level == RiskLevel.HIGH

    # TC-F2-02
    @pytest.mark.unit
    def test_approval_transitions_to_approved(self) -> None:
        """Approval can transition from PENDING → APPROVED."""
        mgr = ApprovalManager()
        decision = PolicyDecision(
            task_id="task_b",
            target_type="plan", target_id="plan_1",
            decision=PolicyDecisionValue.REQUIRE_APPROVAL,
            reason="Needs approval", risk_level=RiskLevel.MEDIUM,
        )
        approval = mgr.create_from_policy(decision, "Approve?", "Details")
        assert approval.status == ApprovalStatus.PENDING
        approval.status = ApprovalStatus.APPROVED
        approval.user_decision = "approved"
        assert approval.status == "APPROVED"

    # TC-F2-03
    @pytest.mark.unit
    def test_approval_transitions_to_rejected(self) -> None:
        """Approval can transition from PENDING → REJECTED."""
        mgr = ApprovalManager()
        decision = PolicyDecision(
            task_id="task_c",
            target_type="tool_intent", target_id="intent_2",
            decision=PolicyDecisionValue.REQUIRE_APPROVAL,
            reason="Check needed", risk_level=RiskLevel.HIGH,
        )
        approval = mgr.create_from_policy(decision, "Dangerous", "Really dangerous")
        approval.status = ApprovalStatus.REJECTED
        approval.user_decision = "rejected"
        assert approval.status == "REJECTED"

    # TC-F2-04
    @pytest.mark.unit
    def test_approval_expiry(self) -> None:
        """Approval can transition to EXPIRED."""
        mgr = ApprovalManager()
        decision = PolicyDecision(
            task_id="task_d",
            target_type="plan", target_id="plan_2",
            decision=PolicyDecisionValue.REQUIRE_APPROVAL,
            reason="Timed out", risk_level=RiskLevel.LOW,
        )
        approval = mgr.create_from_policy(decision, "Old", "Expired request")
        approval.status = ApprovalStatus.EXPIRED
        assert approval.status == "EXPIRED"


# ═══════════════════════════════════════════════════════════════
# F3: Policy Engine — 5 new checkpoints (File, Network, Memory, Output, Plugin, Workflow, Input)
# ═══════════════════════════════════════════════════════════════

class TestPolicyCheckpoints:
    """TC-F3: All 9 policy checkpoints."""

    @pytest.mark.unit
    def test_check_input_empty_denied(self) -> None:
        engine = PolicyEngine()
        d = engine.check_input("t1", "")
        assert d.decision == PolicyDecisionValue.DENY

    @pytest.mark.unit
    def test_check_input_valid_allowed(self) -> None:
        engine = PolicyEngine()
        d = engine.check_input("t1", "Hello world")
        assert d.decision == PolicyDecisionValue.ALLOW

    @pytest.mark.unit
    def test_check_input_oversized_denied(self) -> None:
        engine = PolicyEngine()
        d = engine.check_input("t1", "x" * 60000)
        assert d.decision == PolicyDecisionValue.DENY

    @pytest.mark.unit
    def test_check_file_access_delete_requires_approval(self) -> None:
        engine = PolicyEngine()
        d = engine.check_file_access("t1", Path("/tmp/test.txt"), "delete")
        assert d.decision == PolicyDecisionValue.REQUIRE_APPROVAL

    @pytest.mark.unit
    def test_check_file_access_read_allowed(self) -> None:
        engine = PolicyEngine()
        d = engine.check_file_access("t1", Path("/tmp/test.txt"), "read",
                                      allowed_paths=[Path("/tmp")])
        assert d.decision == PolicyDecisionValue.ALLOW

    @pytest.mark.unit
    def test_check_file_access_overwrite_requires_approval(self, tmp_path: Path) -> None:
        engine = PolicyEngine()
        f = tmp_path / "exists.txt"
        f.write_text("data")
        d = engine.check_file_access("t1", f, "write", allowed_paths=[tmp_path])
        assert d.decision == PolicyDecisionValue.REQUIRE_APPROVAL

    @pytest.mark.unit
    def test_check_network_globally_disabled(self) -> None:
        engine = PolicyEngine()
        engine.network_allowed = False
        d = engine.check_network_access("t1", "https://example.com")
        assert d.decision == PolicyDecisionValue.DENY

    @pytest.mark.unit
    def test_check_network_localhost_requires_approval(self) -> None:
        engine = PolicyEngine()
        d = engine.check_network_access("t1", "http://localhost:8080/api")
        assert d.decision == PolicyDecisionValue.REQUIRE_APPROVAL

    @pytest.mark.unit
    def test_check_network_public_allowed(self) -> None:
        engine = PolicyEngine()
        d = engine.check_network_access("t1", "https://example.com")
        assert d.decision == PolicyDecisionValue.ALLOW

    @pytest.mark.unit
    def test_check_memory_sensitive_content_denied(self) -> None:
        engine = PolicyEngine()
        d = engine.check_memory_write("t1", "Here is my api_key: sk-abc123def456")
        assert d.decision == PolicyDecisionValue.DENY

    @pytest.mark.unit
    def test_check_memory_safe_content_allowed(self) -> None:
        engine = PolicyEngine()
        d = engine.check_memory_write("t1", "User prefers dark mode.")
        assert d.decision == PolicyDecisionValue.ALLOW

    @pytest.mark.unit
    def test_check_output_api_key_leak_blocked(self) -> None:
        engine = PolicyEngine()
        # Use a key format matching the regex: sk-[a-zA-Z0-9]{20,}
        d = engine.check_final_answer("t1", "Key: sk-abcdefghijklmnopqrst12345678")
        assert d.decision == PolicyDecisionValue.DENY

    @pytest.mark.unit
    def test_check_output_clean_allowed(self) -> None:
        engine = PolicyEngine()
        d = engine.check_final_answer("t1", "The answer is 42.")
        assert d.decision == PolicyDecisionValue.ALLOW

    @pytest.mark.unit
    def test_check_plugin_high_risk_requires_approval(self) -> None:
        engine = PolicyEngine()
        d = engine.check_plugin_install("t1", "evil_plugin",
                                         permissions=["tool:command.run"],
                                         source="unknown")
        assert d.decision == PolicyDecisionValue.REQUIRE_APPROVAL

    @pytest.mark.unit
    def test_check_plugin_unknown_source_requires_approval(self) -> None:
        engine = PolicyEngine()
        d = engine.check_plugin_install("t1", "mystery_plugin",
                                         permissions=["tool:file.read"],
                                         source="unknown")
        assert d.decision == PolicyDecisionValue.REQUIRE_APPROVAL

    @pytest.mark.unit
    def test_check_workflow_high_risk_requires_approval(self) -> None:
        engine = PolicyEngine()
        d = engine.check_workflow_run("t1", "wf_risky", risk_summary="HIGH")
        assert d.decision == PolicyDecisionValue.REQUIRE_APPROVAL

    @pytest.mark.unit
    def test_check_workflow_low_risk_allowed(self) -> None:
        engine = PolicyEngine()
        d = engine.check_workflow_run("t1", "wf_safe", risk_summary="LOW")
        assert d.decision == PolicyDecisionValue.ALLOW

    @pytest.mark.unit
    def test_dangerous_command_denied_in_tool_check(self) -> None:
        engine = PolicyEngine()
        intent = ToolIntent(
            task_id="t1", step_id="s1", tool_name="command.run",
            purpose="test", input_summary="rm",
            input_payload={"command": "rm -rf /"},
            expected_result="ok", risk_level=RiskLevel.HIGH,
        )
        d = engine.check_tool_intent(intent)
        assert d.decision == PolicyDecisionValue.DENY

    @pytest.mark.unit
    def test_denied_tool_globally_blocked(self) -> None:
        engine = PolicyEngine()
        engine.denied_tools.add("command.run")
        intent = ToolIntent(
            task_id="t1", step_id="s1", tool_name="command.run",
            purpose="test", input_summary="cmd",
            input_payload={"command": "dir"}, expected_result="ok",
            risk_level=RiskLevel.LOW,
        )
        d = engine.check_tool_intent(intent)
        assert d.decision == PolicyDecisionValue.DENY
