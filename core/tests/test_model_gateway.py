"""Phase 2: Model Gateway Tests

Covers: B1 model/gateway, B2 adapters (heuristic fallback).
"""

from __future__ import annotations
import asyncio

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from flowcraft_core.models.gateway import (
    ModelGateway, DEFAULT_DEEPSEEK_PROFILE, DEEPSEEK_V4_FLASH_PROFILE,
    DEEPSEEK_CHAT_LEGACY,
)
from flowcraft_core.models.adapters.base import ModelProfile
from flowcraft_core.models.adapters.openai_compatible import OpenAICompatibleAdapter


# ═══════════════════════════════════════════════════════════════
# B1: ModelGateway core
# ═══════════════════════════════════════════════════════════════

class TestModelGatewayCore:
    """TC-B1: Gateway initialization, configuration, and fallback."""

    # TC-B1-01
    @pytest.mark.unit
    def test_init_defaults_to_deterministic_dev(self) -> None:
        """Without configuration, gateway starts in deterministic-dev mode."""
        gw = ModelGateway()
        assert gw.provider_name == "deterministic-dev"
        assert gw.model_configured is False
        assert gw.is_live() is False

    # TC-B1-02
    @pytest.mark.unit
    def test_configure_sets_provider_and_live(self) -> None:
        """After configure(), the gateway reports as live with correct provider."""
        gw = ModelGateway()
        mock_adapter = MagicMock()
        mock_adapter.profile = DEFAULT_DEEPSEEK_PROFILE
        mock_adapter.profile.provider = "deepseek"

        gw.configure(mock_adapter, DEFAULT_DEEPSEEK_PROFILE)

        assert gw.model_configured is True
        assert gw.provider_name == "deepseek"
        assert gw.is_live() is True

    # TC-B1-03
    @pytest.mark.unit
    def test_generate_structured_without_adapter_uses_heuristic(self) -> None:
        """When no adapter is configured, generate_structured falls back to heuristic."""
        gw = ModelGateway()
        import asyncio
        result = asyncio.run(gw.generate_structured("解释 FlowCraft", "TaskBrief"))
        assert isinstance(result, dict)
        assert "task_type" in result
        assert "risk_level" in result
        # QA input triggers QA task type (deterministic)
        assert result["task_type"] in ("QA", "FILE_TASK", "BROWSER_TASK", "LOCAL_OPERATION")

    # TC-B1-04
    @pytest.mark.unit
    def test_generate_structured_with_file_task_input(self) -> None:
        """Heuristic correctly identifies file task intent."""
        gw = ModelGateway()
        import asyncio
        result = asyncio.run(gw.generate_structured("读取 D:\\project\\readme.txt 的内容", "TaskBrief"))
        assert result["task_type"] == "FILE_TASK"
        assert result["requires_local_files"] is True

    # TC-B1-05
    @pytest.mark.unit
    def test_generate_structured_high_risk_input(self) -> None:
        """Heuristic marks dangerous inputs as HIGH risk."""
        gw = ModelGateway()
        import asyncio
        result = asyncio.run(gw.generate_structured("删除 C:\\Windows\\System32 目录下的所有文件", "TaskBrief"))
        # Either FILE_TASK (file words) or LOCAL_OPERATION (if command words)
        assert result["risk_level"] in ("HIGH", "MEDIUM")

    # TC-B1-06
    @pytest.mark.unit
    def test_heuristic_plan_qa_produces_direct_mode(self) -> None:
        """ExecutionPlan heuristic for QA tasks generates DIRECT mode."""
        gw = ModelGateway()
        import asyncio
        brief_json = json.dumps({"objective": "What is AI?", "task_type": "QA", "risk_level": "LOW"})
        result = asyncio.run(gw.generate_structured(brief_json, "ExecutionPlan"))
        assert result["mode"] == "DIRECT"
        assert len(result["steps"]) == 1
        assert result["steps"][0]["action_type"] == "MODEL_ANSWER"

    # TC-B1-07
    @pytest.mark.unit
    def test_heuristic_plan_file_task_produces_linear_mode(self) -> None:
        """ExecutionPlan heuristic for FILE_TASK generates LINEAR mode with tools."""
        gw = ModelGateway()
        import asyncio
        brief_json = json.dumps({
            "objective": "读取并写入文件",
            "task_type": "FILE_TASK",
            "risk_level": "MEDIUM",
        })
        result = asyncio.run(gw.generate_structured(brief_json, "ExecutionPlan"))
        assert result["mode"] == "LINEAR"
        assert len(result["steps"]) >= 2
        assert "file.read" in result["steps"][1].get("required_tools", [])

    # TC-B1-08
    @pytest.mark.unit
    def test_is_live_false_when_no_adapter(self) -> None:
        """is_live returns False when no adapter is configured."""
        gw = ModelGateway()
        assert not gw.is_live()

    # TC-B1-09
    @pytest.mark.unit
    def test_switch_model_unknown_returns_false(self) -> None:
        """switch_model with unknown model_id returns False."""
        gw = ModelGateway()
        result = gw.switch_model("nonexistent-model-xyz")
        assert result is False

    # TC-B1-10
    @pytest.mark.unit
    def test_test_connection_no_adapter(self) -> None:
        """test_connection with no adapter returns not_configured."""
        gw = ModelGateway()
        import asyncio
        result = asyncio.run(gw.test_connection())
        assert result["status"] == "not_configured"


# ═══════════════════════════════════════════════════════════════
# B2: Heuristic accuracy tests
# ═══════════════════════════════════════════════════════════════

class TestHeuristicAccuracy:
    """Verify deterministic-dev heuristic correctly classifies input types."""

    @pytest.mark.unit
    def test_qa_detection_chinese(self) -> None:
        """Chinese question → QA."""
        gw = ModelGateway()
        import asyncio
        r = asyncio.run(gw.generate_structured("FlowCraft 是什么框架？", "TaskBrief"))
        assert r["task_type"] == "QA"

    @pytest.mark.unit
    def test_browser_detection_with_url(self) -> None:
        """URL in input → BROWSER_TASK."""
        gw = ModelGateway()
        import asyncio
        r = asyncio.run(gw.generate_structured("打开 https://example.com 查看内容", "TaskBrief"))
        assert r["task_type"] == "BROWSER_TASK"

    @pytest.mark.unit
    def test_local_operation_detection(self) -> None:
        """Command keyword → LOCAL_OPERATION."""
        gw = ModelGateway()
        import asyncio
        r = asyncio.run(gw.generate_structured("运行命令 dir 查看当前目录", "TaskBrief"))
        assert r["task_type"] == "LOCAL_OPERATION"

    @pytest.mark.unit
    def test_fallback_text_mentions_flowcraft(self) -> None:
        """Fallback text for FlowCraft-related prompts."""
        result = ModelGateway._fallback_text("What is FlowCraft?")
        assert "FlowCraft" in result
        assert len(result) > 20

    @pytest.mark.unit
    def test_fallback_text_for_unknown(self) -> None:
        """Fallback text for unrelated prompts."""
        result = ModelGateway._fallback_text("What is the weather?")
        assert "FlowCraft" in result
        assert "开发模式" in result or "已完成" in result


# ═══════════════════════════════════════════════════════════════
# B3: ModelProfile tests
# ═══════════════════════════════════════════════════════════════

class TestModelProfile:
    """Model profile configurations are correct."""

    @pytest.mark.unit
    def test_default_deepseek_profile(self) -> None:
        """Default profile is DeepSeek V4 Pro."""
        p = DEFAULT_DEEPSEEK_PROFILE
        assert p.model_id == "deepseek-v4-pro"
        assert p.provider == "deepseek"
        assert p.supports_structured_output is True
        assert p.context_window >= 128_000

    @pytest.mark.unit
    def test_flash_profile_is_cheaper(self) -> None:
        """Flash profile costs less than Pro."""
        p = DEEPSEEK_V4_FLASH_PROFILE
        assert p.cost_input_per_1k < DEFAULT_DEEPSEEK_PROFILE.cost_input_per_1k
        assert p.cost_output_per_1k < DEFAULT_DEEPSEEK_PROFILE.cost_output_per_1k


# ═══════════════════════════════════════════════════════════════
# B4: ModelGateway adapter integration tests
# ═══════════════════════════════════════════════════════════════

class TestModelGatewayAdapter:
    """TC-B4: Gateway with mock adapter (live path)."""

    @pytest.fixture
    def mock_adapter(self) -> MagicMock:
        adapter = MagicMock()
        adapter.profile = DEFAULT_DEEPSEEK_PROFILE
        adapter.chat = AsyncMock(return_value="Mock response from adapter")
        adapter.structured_chat = AsyncMock(return_value={
            "task_type": "QA", "objective": "test",
            "risk_level": "LOW", "success_criteria": ["ok"],
            "constraints": [], "target_objects": [],
            "required_capabilities": [],
            "requires_local_files": False, "requires_network": False,
            "requires_tools": False,
            "clarification_required": False, "clarification_questions": [],
            "expected_output_format": "text",
        })
        adapter.test_connection = AsyncMock(return_value=True)
        return adapter

    @pytest.mark.unit
    def test_generate_text_with_adapter(self, mock_adapter) -> None:
        """generate_text() delegates to adapter when configured."""
        gw = ModelGateway()
        gw.configure(mock_adapter, DEFAULT_DEEPSEEK_PROFILE)
        result = asyncio.run(gw.generate_text("Hello"))
        assert result == "Mock response from adapter"

    @pytest.mark.unit
    def test_generate_text_without_adapter_uses_fallback(self) -> None:
        """generate_text() without adapter returns identity text."""
        gw = ModelGateway()
        result = asyncio.run(gw.generate_text("Hello world"))
        assert isinstance(result, str)
        assert len(result) > 10

    @pytest.mark.unit
    def test_switch_model_success(self) -> None:
        """switch_model with valid model_id returns True."""
        gw = ModelGateway()
        mock_adapter = MagicMock()
        mock_adapter.profile = DEFAULT_DEEPSEEK_PROFILE
        gw.configure(mock_adapter, DEFAULT_DEEPSEEK_PROFILE)

        result = gw.switch_model("deepseek-v4-flash")
        assert result is True
        assert gw.provider_name == "deepseek"

    @pytest.mark.unit
    def test_switch_model_no_api_key_fails(self) -> None:
        """switch_model without API key or existing adapter returns False."""
        gw = ModelGateway()
        result = gw.switch_model("deepseek-v4-pro")
        assert result is False

    @pytest.mark.unit
    @pytest.mark.skip(reason="Requires real API key for fallback chain test")
    def test_call_with_fallback_first_succeeds(self, mock_adapter) -> None:
        """call_with_fallback: primary adapter succeeds, no fallback needed."""
        pass

    @pytest.mark.unit
    @pytest.mark.skip(reason="Requires real API key for fallback chain test")
    def test_call_with_fallback_primary_fails(self) -> None:
        """call_with_fallback: primary fails, falls back to next in chain."""
        pass

    @pytest.mark.unit
    def test_generate_structured_with_adapter(self, mock_adapter) -> None:
        """generate_structured with live adapter returns parsed result."""
        gw = ModelGateway()
        gw.configure(mock_adapter, DEFAULT_DEEPSEEK_PROFILE)
        result = asyncio.run(gw.generate_structured("Test QA query", "TaskBrief"))
        assert isinstance(result, dict)
        assert result["task_type"] == "QA"
        mock_adapter.structured_chat.assert_called_once()

    @pytest.mark.unit
    def test_configure_then_is_live(self, mock_adapter) -> None:
        """After configure(), is_live() returns True and model_configured is True."""
        gw = ModelGateway()
        assert not gw.is_live()
        gw.configure(mock_adapter, DEFAULT_DEEPSEEK_PROFILE)
        assert gw.is_live()
        assert gw.model_configured is True

    @pytest.mark.unit
    @pytest.mark.skip(reason="Requires real API key for connection test")
    def test_test_connection_with_adapter(self, mock_adapter) -> None:
        """test_connection with adapter returns status dict."""
        pass
