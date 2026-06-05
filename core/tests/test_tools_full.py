"""Phase 3: Tool System Tests

Covers: E1 tool registry/harness, E2 builtin tools, E3 filesystem, path safety.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from flowcraft_core.tools.base import (
    Tool, ToolDefinition, is_path_allowed, observation_from_output,
)
from flowcraft_core.tools.harness import ToolRegistry, ToolHarness
from flowcraft_core.domain.enums import RiskLevel
from flowcraft_core.domain.schemas import ToolIntent


# ═══════════════════════════════════════════════════════════════
# E1: Tool Registry tests
# ═══════════════════════════════════════════════════════════════

class FakeTool(Tool):
    """A minimal tool implementation for testing."""
    def __init__(self, tool_name: str = "test.fake", risk_level: RiskLevel = RiskLevel.LOW,
                 category: str = "test") -> None:
        self.definition = ToolDefinition(
            tool_name=tool_name,
            display_name=f"Fake {tool_name}",
            description="A fake tool for testing",
            category=category,
            risk_level=risk_level,
        )

    async def execute(self, intent: ToolIntent):
        return observation_from_output(intent, "COMPLETED", "Fake tool executed.")


def make_tool(name: str = "test.tool", risk: RiskLevel = RiskLevel.LOW,
              category: str = "test", requires_approval: bool = False) -> FakeTool:
    t = FakeTool(tool_name=name, risk_level=risk, category=category)
    t.definition.requires_approval_by_default = requires_approval
    return t


class TestToolRegistry:
    """TC-E1: Tool registration and retrieval."""

    # TC-E1-01
    @pytest.mark.unit
    def test_register_and_get(self) -> None:
        """Registered tool is retrievable by name."""
        reg = ToolRegistry()
        tool = make_tool("file.read")
        reg.register(tool)
        assert reg.get("file.read") is tool

    # TC-E1-01b
    @pytest.mark.unit
    def test_get_nonexistent_returns_none(self) -> None:
        """get() returns None for unknown tools."""
        reg = ToolRegistry()
        assert reg.get("nonexistent.tool") is None

    # TC-E1-02
    @pytest.mark.unit
    def test_list_definitions_returns_all(self) -> None:
        """list_definitions() returns all registered tool definitions."""
        reg = ToolRegistry()
        reg.register(make_tool("file.read", category="FS"))
        reg.register(make_tool("command.run", risk=RiskLevel.HIGH, category="SYSTEM"))
        defs = reg.list_definitions()
        assert len(defs) == 2
        names = {d["tool_name"] for d in defs}
        assert names == {"file.read", "command.run"}

    # TC-E1-02b
    @pytest.mark.unit
    def test_list_definitions_has_required_fields(self) -> None:
        """Each definition contains all required metadata fields."""
        reg = ToolRegistry()
        reg.register(make_tool("browser.read"))
        defs = reg.list_definitions()
        d = defs[0]
        for field in ("tool_name", "display_name", "description", "category", "risk_level"):
            assert field in d, f"Missing field: {field}"

    # TC-E1-03
    @pytest.mark.unit
    def test_reregister_overwrites(self) -> None:
        """Re-registering with the same name overwrites the old tool."""
        reg = ToolRegistry()
        t1 = make_tool("duplicate", risk=RiskLevel.LOW)
        t2 = make_tool("duplicate", risk=RiskLevel.CRITICAL)
        reg.register(t1)
        reg.register(t2)
        assert reg.get("duplicate") is t2

    # TC-E1-04
    @pytest.mark.unit
    def test_unregister_removes_tool(self) -> None:
        """unregister() removes the tool and returns True."""
        reg = ToolRegistry()
        reg.register(make_tool("remove.me"))
        assert reg.unregister("remove.me") is True
        assert reg.get("remove.me") is None

    # TC-E1-04b
    @pytest.mark.unit
    def test_unregister_nonexistent_returns_false(self) -> None:
        """unregister() for unknown tool returns False."""
        reg = ToolRegistry()
        assert reg.unregister("no.such.tool") is False


# ═══════════════════════════════════════════════════════════════
# E2: Tool Definition tests
# ═══════════════════════════════════════════════════════════════

class TestToolDefinition:
    """ToolDefinition model validation."""

    @pytest.mark.unit
    def test_defaults(self) -> None:
        """Default values are sensible."""
        td = ToolDefinition(
            tool_name="test", display_name="Test",
            description="Desc", category="test",
            risk_level=RiskLevel.LOW,
        )
        assert td.requires_approval_by_default is False
        assert td.timeout_seconds == 30
        assert td.max_retries == 0
        assert td.supports_dry_run is False

    @pytest.mark.unit
    def test_high_risk_approval_default(self) -> None:
        """CRITICAL tools can default to requiring approval."""
        td = ToolDefinition(
            tool_name="dangerous", display_name="Danger",
            description="Risky", category="system",
            risk_level=RiskLevel.CRITICAL,
            requires_approval_by_default=True,
            timeout_seconds=10,
            max_retries=0,
        )
        assert td.risk_level == RiskLevel.CRITICAL
        assert td.requires_approval_by_default is True
        assert td.timeout_seconds == 10

    @pytest.mark.unit
    def test_serialization_roundtrip(self) -> None:
        """ToolDefinition → dict → ToolDefinition roundtrip."""
        td = ToolDefinition(
            tool_name="test.roundtrip",
            display_name="Roundtrip Tool",
            description="Testing serialization",
            category="test",
            risk_level=RiskLevel.MEDIUM,
            permissions=["read", "write"],
            examples=[{"input": "test", "output": "expected"}],
        )
        dumped = td.model_dump(mode="json")
        reloaded = ToolDefinition.model_validate(dumped)
        assert reloaded.tool_name == "test.roundtrip"
        assert reloaded.permissions == ["read", "write"]
        assert reloaded.examples == [{"input": "test", "output": "expected"}]


# ═══════════════════════════════════════════════════════════════
# E3: Path safety tests
# ═══════════════════════════════════════════════════════════════

class TestPathSafety:
    """Path traversal and allowed-path enforcement."""

    # TC-E2-02
    @pytest.mark.unit
    def test_allowed_path_pass(self, tmp_path: Path) -> None:
        """A path under the allowed root passes the check."""
        allowed = [tmp_path]
        child = tmp_path / "subdir" / "file.txt"
        child.parent.mkdir(parents=True, exist_ok=True)
        child.write_text("test")
        assert is_path_allowed(child, allowed) is True

    # TC-E2-02b
    @pytest.mark.unit
    def test_path_traversal_rejected(self, tmp_path: Path) -> None:
        """A path escaping the allowed root is rejected."""
        allowed = [tmp_path / "safe"]
        allowed[0].mkdir(parents=True, exist_ok=True)
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        assert is_path_allowed(outside, allowed) is False

    # TC-E2-02c
    @pytest.mark.unit
    def test_relative_path_traversal_rejected(self, tmp_path: Path) -> None:
        """'../' style traversal is resolved and rejected."""
        allowed = [tmp_path / "sandbox"]
        allowed[0].mkdir(parents=True, exist_ok=True)
        evil = (allowed[0] / "../../outside.txt").resolve()
        evil.parent.mkdir(parents=True, exist_ok=True)
        evil.write_text("data")
        assert is_path_allowed(evil, allowed) is False

    @pytest.mark.unit
    def test_normalization_symlink_equivalent(self, tmp_path: Path) -> None:
        """Resolved paths are compared (handles symlinks, .., .)."""
        allowed = [tmp_path]
        child = tmp_path / "a" / "." / "b" / ".." / "a" / "file.txt"
        child.parent.mkdir(parents=True, exist_ok=True)
        child.write_text("x")
        assert is_path_allowed(child, allowed) is True


# ═══════════════════════════════════════════════════════════════
# E4: Tool intent / observation helpers
# ═══════════════════════════════════════════════════════════════

class TestObservationHelper:
    """observation_from_output factory tests."""

    @pytest.mark.unit
    def test_observation_from_output_success(self) -> None:
        """Creates a COMPLETED observation with correct fields."""
        intent = ToolIntent(
            task_id="t1", step_id="s1",
            tool_name="test.tool", purpose="testing",
            input_summary="no input",
            input_payload={}, expected_result="ok",
        )
        obs = observation_from_output(intent, "COMPLETED", "All good",
                                      payload={"key": "value"})
        assert obs.status == "COMPLETED"
        assert obs.output_summary == "All good"
        assert obs.output_payload == {"key": "value"}
        assert obs.tool_intent_id == intent.tool_intent_id
        assert obs.task_id == "t1"

    @pytest.mark.unit
    def test_observation_from_output_error(self) -> None:
        """Error observation has the right status and error message."""
        intent = ToolIntent(
            task_id="t2", step_id="s2",
            tool_name="bad.tool", purpose="test",
            input_summary="x", input_payload={}, expected_result="y",
        )
        obs = observation_from_output(intent, "FAILED", "Something broke",
                                      error="Traceback details")
        assert obs.status == "FAILED"
        assert obs.error_message == "Traceback details"


# ═══════════════════════════════════════════════════════════════
# E5: File read/write safety integration
# ═══════════════════════════════════════════════════════════════

class TestFileToolSafety:
    """File tool security integration tests."""

    @pytest.mark.component
    def test_file_read_in_allowed_path(self, tmp_path: Path) -> None:
        """Reading a file within allowed paths succeeds."""
        from flowcraft_core.tools.builtin import FileReadTool
        f = tmp_path / "test.txt"
        f.write_text("Hello content", encoding="utf-8")
        tool = FileReadTool([tmp_path])
        intent = ToolIntent(
            task_id="t_fr", step_id="s1",
            tool_name="file.read", purpose="read test",
            input_summary="read", input_payload={"path": str(f)},
            expected_result="content",
        )
        import asyncio
        obs = asyncio.run(tool.execute(intent))
        assert obs.status == "COMPLETED"
        assert "Hello content" in obs.output_summary or "Hello content" in str(obs.output_payload)

    @pytest.mark.component
    def test_file_read_outside_allowed_path(self, tmp_path: Path) -> None:
        """Reading a file outside allowed paths returns DENIED observation."""
        from flowcraft_core.tools.builtin import FileReadTool
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        outside = tmp_path / "secret.txt"
        outside.write_text("secret", encoding="utf-8")
        tool = FileReadTool([sandbox])
        intent = ToolIntent(
            task_id="t_fr2", step_id="s2",
            tool_name="file.read", purpose="read restricted",
            input_summary="read", input_payload={"path": str(outside)},
            expected_result="content",
        )
        import asyncio
        obs = asyncio.run(tool.execute(intent))
        # Tool returns DENIED status instead of raising exception
        assert obs.status in ("DENIED", "FAILED"), f"Expected DENIED, got {obs.status}"
        assert obs.error_message is not None

    @pytest.mark.component
    def test_file_write_in_allowed_path(self, tmp_path: Path) -> None:
        """Writing a file within allowed paths succeeds."""
        from flowcraft_core.tools.builtin import FileWriteTool
        f = tmp_path / "output.md"
        tool = FileWriteTool([tmp_path])
        intent = ToolIntent(
            task_id="t_fw", step_id="s3",
            tool_name="file.write", purpose="write output",
            input_summary="write",
            input_payload={"path": str(f), "content": "# Test Output"},
            expected_result="file written",
        )
        import asyncio
        obs = asyncio.run(tool.execute(intent))
        assert obs.status in ("COMPLETED", "completed")
        assert f.exists()
        content = f.read_text(encoding="utf-8")
        assert "Test Output" in content


# ═══════════════════════════════════════════════════════════════
# E6: Workflow tools (regression for tool_input bug)
# ═══════════════════════════════════════════════════════════════

class TestWorkflowTools:
    """WorkflowSearchTool and WorkflowExecuteTool — catch field-name bugs."""

    @pytest.mark.unit
    def test_workflow_search_execute_uses_input_payload(self, tmp_database) -> None:
        """WorkflowSearchTool.execute() reads from input_payload, not tool_input."""
        from flowcraft_core.tools.workflow_tools import WorkflowSearchTool
        mp_dir = tmp_database.path.parent / "marketplace"
        mp_dir.mkdir(parents=True, exist_ok=True)
        tool = WorkflowSearchTool(tmp_database, mp_dir)
        intent = ToolIntent(
            task_id="t_ws", step_id="s1",
            tool_name="workflow_search", purpose="search",
            input_summary="find novel workflow",
            input_payload={"query": "小说"},
            expected_result="workflows list",
        )
        import asyncio
        obs = asyncio.run(tool.execute(intent))
        # Must not raise AttributeError: 'ToolIntent' object has no attribute 'tool_input'
        assert obs.status == "success"
        assert "workflows" in obs.output_payload

    @pytest.mark.unit
    def test_workflow_search_no_query_returns_empty(self, tmp_database) -> None:
        """Empty query returns 'No workflows found'."""
        from flowcraft_core.tools.workflow_tools import WorkflowSearchTool
        mp_dir = tmp_database.path.parent / "marketplace"
        mp_dir.mkdir(parents=True, exist_ok=True)
        tool = WorkflowSearchTool(tmp_database, mp_dir)
        intent = ToolIntent(
            task_id="t_ws2", step_id="s2",
            tool_name="workflow_search", purpose="search",
            input_summary="empty search",
            input_payload={"query": ""},
            expected_result="workflows list",
        )
        import asyncio
        obs = asyncio.run(tool.execute(intent))
        assert obs.status == "success"
        assert "No workflows found" in obs.output_summary

    @pytest.mark.unit
    def test_workflow_execute_uses_input_payload(self, tmp_database) -> None:
        """WorkflowExecuteTool.execute() reads from input_payload, not tool_input."""
        from flowcraft_core.tools.workflow_tools import WorkflowExecuteTool
        tool = WorkflowExecuteTool(tmp_database)
        intent = ToolIntent(
            task_id="t_we", step_id="s3",
            tool_name="workflow_execute", purpose="execute",
            input_summary="run workflow",
            input_payload={"workflow_id": "wf_nonexistent", "input": "test"},
            expected_result="workflow executed",
        )
        import asyncio
        obs = asyncio.run(tool.execute(intent))
        # Must not raise AttributeError: 'ToolIntent' object has no attribute 'tool_input'
        assert obs.status == "error"  # nonexistent workflow
        assert "not found" in obs.output_summary.lower()


# ═══════════════════════════════════════════════════════════════
# E7: ToolHarness — schema validation + dry run
# ═══════════════════════════════════════════════════════════════

class TestToolHarnessValidation:
    """Schema validation and dry run preview."""

    @pytest.mark.unit
    def test_validate_input_missing_required_field(self) -> None:
        from flowcraft_core.tools.harness import ToolHarness
        from flowcraft_core.policy.engine import PolicyEngine
        harness = ToolHarness(ToolRegistry(), PolicyEngine())
        errors = harness.validate_input("file.read", {})
        assert len(errors) > 0
        assert any("path" in e for e in errors)

    @pytest.mark.unit
    def test_validate_input_valid_passes(self) -> None:
        from flowcraft_core.tools.harness import ToolHarness
        from flowcraft_core.policy.engine import PolicyEngine
        harness = ToolHarness(ToolRegistry(), PolicyEngine())
        errors = harness.validate_input("file.read", {"path": "/tmp/test.txt"})
        assert len(errors) == 0

    @pytest.mark.unit
    def test_validate_input_path_traversal_rejected(self) -> None:
        from flowcraft_core.tools.harness import ToolHarness
        from flowcraft_core.policy.engine import PolicyEngine
        harness = ToolHarness(ToolRegistry(), PolicyEngine())
        errors = harness.validate_input("file.read", {"path": "../../etc/passwd"})
        assert any(".." in e for e in errors)

    @pytest.mark.unit
    def test_dry_run_file_read_preview(self) -> None:
        from flowcraft_core.tools.harness import ToolHarness
        from flowcraft_core.policy.engine import PolicyEngine
        harness = ToolHarness(ToolRegistry(), PolicyEngine())
        preview = harness.generate_dry_run_preview(
            "file.read", {"path": "/tmp/doc.txt"})
        assert "effects" in preview
        assert any("读取" in str(e) for e in preview["effects"])

    @pytest.mark.unit
    def test_dry_run_file_write_shows_overwrite_warning(self) -> None:
        from flowcraft_core.tools.harness import ToolHarness
        from flowcraft_core.policy.engine import PolicyEngine
        harness = ToolHarness(ToolRegistry(), PolicyEngine())
        preview = harness.generate_dry_run_preview(
            "file.write", {"path": __file__, "content": "test"})
        assert any("overwrite" in str(e).lower() for e in preview["effects"])

    @pytest.mark.unit
    def test_dry_run_file_delete_warning(self) -> None:
        from flowcraft_core.tools.harness import ToolHarness
        from flowcraft_core.policy.engine import PolicyEngine
        harness = ToolHarness(ToolRegistry(), PolicyEngine())
        preview = harness.generate_dry_run_preview(
            "file.delete", {"path": "/tmp/delete_me.txt"})
        assert any("delete" in str(e).lower() for e in preview["effects"])

    @pytest.mark.unit
    def test_dry_run_command_shows_command(self) -> None:
        from flowcraft_core.tools.harness import ToolHarness
        from flowcraft_core.policy.engine import PolicyEngine
        harness = ToolHarness(ToolRegistry(), PolicyEngine())
        preview = harness.generate_dry_run_preview(
            "command.run", {"command": "dir"})
        assert any("dir" in str(e) for e in preview["effects"])

    @pytest.mark.component
    def test_invoke_with_validation_error_fails(self, tmp_path: Path) -> None:
        from flowcraft_core.tools.harness import ToolHarness
        from flowcraft_core.policy.engine import PolicyEngine
        from flowcraft_core.tools.builtin import FileReadTool
        reg = ToolRegistry()
        reg.register(FileReadTool([tmp_path]))
        harness = ToolHarness(reg, PolicyEngine())
        intent = ToolIntent(
            task_id="t1", step_id="s1", tool_name="file.read",
            purpose="test", input_summary="x",
            input_payload={"path": "../../etc/passwd"},
            expected_result="ok",
        )
        import asyncio
        obs = asyncio.run(harness.invoke(intent))
        assert obs.status == "FAILED"
        assert "校验" in obs.output_summary or "validation" in str(obs.error_message).lower()

    @pytest.mark.component
    def test_invoke_dry_run_returns_preview(self, tmp_path: Path) -> None:
        from flowcraft_core.tools.harness import ToolHarness
        from flowcraft_core.policy.engine import PolicyEngine
        from flowcraft_core.tools.builtin import FileReadTool
        f = tmp_path / "dry.txt"
        f.write_text("test")
        reg = ToolRegistry()
        reg.register(FileReadTool([tmp_path]))
        harness = ToolHarness(reg, PolicyEngine())
        intent = ToolIntent(
            task_id="t2", step_id="s2", tool_name="file.read",
            purpose="test", input_summary="x",
            input_payload={"path": str(f)},
            expected_result="ok",
        )
        import asyncio
        obs = asyncio.run(harness.invoke(intent, dry_run=True))
        assert obs.status == "DRY_RUN"
        assert "effects" in obs.output_payload


# ═══════════════════════════════════════════════════════════════
# E8: Browser safety — URL scheme blocking
# ═══════════════════════════════════════════════════════════════

class TestBrowserSafety:
    """Browser URL scheme blocking."""

    @pytest.mark.unit
    def test_browser_blocks_file_scheme(self) -> None:
        from flowcraft_core.tools.browser import BrowserReadTool
        tool = BrowserReadTool()
        intent = ToolIntent(
            task_id="t1", step_id="s1", tool_name="browser.read",
            purpose="test", input_summary="x",
            input_payload={"url": "file:///etc/passwd"},
            expected_result="ok",
        )
        import asyncio
        obs = asyncio.run(tool.execute(intent))
        assert obs.status == "DENIED"

    @pytest.mark.unit
    def test_browser_blocks_javascript_scheme(self) -> None:
        from flowcraft_core.tools.browser import BrowserReadTool
        tool = BrowserReadTool()
        intent = ToolIntent(
            task_id="t2", step_id="s2", tool_name="browser.read",
            purpose="test", input_summary="x",
            input_payload={"url": "javascript:alert(1)"},
            expected_result="ok",
        )
        import asyncio
        obs = asyncio.run(tool.execute(intent))
        assert obs.status == "DENIED"
