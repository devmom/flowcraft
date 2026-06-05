"""Test tool infrastructure - tool registry, harness, document tools."""
import pytest
from pathlib import Path
from flowcraft_core.tools.base import ToolDefinition
from flowcraft_core.domain.enums import RiskLevel
from flowcraft_core.tools.harness import ToolRegistry


class TestToolRegistry:
    def setup_method(self):
        self.registry = ToolRegistry()

    def test_register_and_get(self):
        class FakeTool:
            pass
        tool = FakeTool()
        tool.definition = ToolDefinition(
            tool_name="test.tool", display_name="Test Tool",
            description="For testing", category="test",
            risk_level=RiskLevel.LOW,
        )
        self.registry.register(tool)
        assert self.registry.get("test.tool") is tool
        assert self.registry.get("nonexistent") is None

    def test_list_definitions(self):
        class FakeTool:
            pass
        tool = FakeTool()
        tool.definition = ToolDefinition(
            tool_name="test.tool2", display_name="Test 2",
            description="Testing", category="test",
            risk_level=RiskLevel.LOW,
        )
        self.registry.register(tool)
        defs = self.registry.list_definitions()
        assert len(defs) == 1
        assert defs[0]["tool_name"] == "test.tool2"


class TestToolDefinition:
    def test_defaults(self):
        td = ToolDefinition(
            tool_name="test", display_name="Test",
            description="Desc", category="test",
            risk_level=RiskLevel.LOW,
        )
        assert td.requires_approval_by_default is False
        assert td.timeout_seconds == 30
        assert td.max_retries == 0

    def test_high_risk(self):
        td = ToolDefinition(
            tool_name="dangerous", display_name="Danger",
            description="Risky", category="system",
            risk_level=RiskLevel.CRITICAL,
            requires_approval_by_default=True,
        )
        assert td.risk_level == RiskLevel.CRITICAL
        assert td.requires_approval_by_default is True


class TestPluginSystem:
    def test_manifest_create(self):
        from flowcraft_core.tools.plugin_registry import PluginManifest
        m = PluginManifest(name="test", version="1.0", author="tester")
        assert m.name == "test"
        assert m.version == "1.0"

    def test_manifest_serialize(self):
        from flowcraft_core.tools.plugin_registry import PluginManifest
        m = PluginManifest(name="test", version="1.0",
                          tools=[{"tool_name": "t1", "entry_point": "mod:Cls"}])
        d = m.to_dict()
        assert d["name"] == "test"
        assert len(d["tools"]) == 1

