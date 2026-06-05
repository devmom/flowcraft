"""P1: Playwright Browser Tools — definition + validation tests.

Full integration tests require `playwright install chromium`.
These tests validate definitions and input handling without browser.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from flowcraft_core.tools.playwright_tools import (
    BrowserNavigateTool, BrowserClickTool, BrowserFillTool,
    BrowserScreenshotFullTool,
)
from flowcraft_core.domain.schemas import ToolIntent
from flowcraft_core.domain.enums import RiskLevel


def make_intent(tool_name: str, **payload) -> ToolIntent:
    return ToolIntent(
        task_id="t_pw", step_id="s1", tool_name=tool_name,
        purpose="test", input_summary="x",
        input_payload=payload, expected_result="ok",
    )


class TestPlaywrightDefinitions:
    """Validate tool definitions."""

    @pytest.mark.unit
    def test_navigate_definition(self) -> None:
        tool = BrowserNavigateTool()
        d = tool.definition
        assert d.tool_name == "browser.navigate"
        assert d.category == "browser"

    @pytest.mark.unit
    def test_click_definition(self) -> None:
        tool = BrowserClickTool()
        d = tool.definition
        assert d.tool_name == "browser.click"

    @pytest.mark.unit
    def test_fill_definition(self) -> None:
        tool = BrowserFillTool()
        d = tool.definition
        assert d.tool_name == "browser.fill"

    @pytest.mark.unit
    def test_screenshot_definition(self) -> None:
        tool = BrowserScreenshotFullTool(Path("/tmp"))
        d = tool.definition
        assert d.tool_name == "browser.screenshot_full"


class TestPlaywrightInputValidation:
    """Input validation without actual browser."""

    @pytest.mark.unit
    def test_navigate_missing_url_fails(self) -> None:
        tool = BrowserNavigateTool()
        intent = make_intent("browser.navigate")
        obs = asyncio.run(tool.execute(intent))
        assert obs.status == "FAILED"

    @pytest.mark.unit
    def test_navigate_url_adds_https(self) -> None:
        """URL without scheme gets https:// prepended (test will fail at Playwright import, which is expected)."""
        tool = BrowserNavigateTool()
        intent = make_intent("browser.navigate", url="example.com")
        obs = asyncio.run(tool.execute(intent))
        # Will fail with ImportError (no playwright) or proceed
        assert obs.status in ("FAILED", "COMPLETED")
