"""Playwright browser automation tools - full web interaction.

Optional dependency: playwright (pip install playwright && playwright install chromium)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from flowcraft_core.domain.enums import RiskLevel
from flowcraft_core.domain.schemas import ToolIntent
from flowcraft_core.tools.base import Tool, ToolDefinition, is_path_allowed, observation_from_output

logger = logging.getLogger(__name__)


class BrowserNavigateTool(Tool):
    """Navigate to a URL and return page content."""

    def __init__(self) -> None:
        self.definition = ToolDefinition(
            tool_name="browser.navigate",
            display_name="Browser Navigate",
            description="Navigate to a URL and extract page content",
            category="browser",
            risk_level=RiskLevel.MEDIUM,
            permissions=["network.http"],
            requires_approval_by_default=True,
        )

    async def execute(self, intent: ToolIntent):
        url = intent.input_payload.get("url", "")
        if not url:
            return observation_from_output(intent, "FAILED", "No URL provided", error="Missing url")

        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(url, timeout=30000)
                content = await page.content()
                text = await page.inner_text("body")
                title = await page.title()
                await browser.close()

                return observation_from_output(
                    intent, "COMPLETED",
                    f"Navigated to {url}: {title}",
                    {"url": url, "title": title, "text": text[:8000], "text_length": len(text)},
                )
        except ImportError:
            return observation_from_output(intent, "FAILED",
                "Playwright not installed. Run: pip install playwright && playwright install chromium",
                error="Missing playwright dependency")
        except Exception as exc:
            return observation_from_output(intent, "FAILED", str(exc), error="Browser navigation failed")


class BrowserClickTool(Tool):
    """Click an element on the page."""

    def __init__(self) -> None:
        self.definition = ToolDefinition(
            tool_name="browser.click",
            display_name="Browser Click",
            description="Click an element identified by CSS selector or text",
            category="browser",
            risk_level=RiskLevel.HIGH,
            permissions=["browser.interact"],
            requires_approval_by_default=True,
        )

    async def execute(self, intent: ToolIntent):
        selector = intent.input_payload.get("selector", "")
        if not selector:
            return observation_from_output(intent, "FAILED", "No selector", error="Missing selector")
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(intent.input_payload.get("url", "about:blank"), timeout=15000)
                await page.click(selector, timeout=5000)
                await page.wait_for_timeout(1000)
                content = await page.inner_text("body")
                await browser.close()
                return observation_from_output(intent, "COMPLETED",
                    f"Clicked {selector}", {"selector": selector, "result_text": content[:3000]})
        except ImportError:
            return observation_from_output(intent, "FAILED",
                "Playwright not installed", error="Missing playwright")
        except Exception as exc:
            return observation_from_output(intent, "FAILED", str(exc), error="Click failed")


class BrowserFillTool(Tool):
    """Fill a form field."""

    def __init__(self) -> None:
        self.definition = ToolDefinition(
            tool_name="browser.fill",
            display_name="Browser Fill Form",
            description="Fill text into a form field identified by selector",
            category="browser",
            risk_level=RiskLevel.HIGH,
            permissions=["browser.interact"],
            requires_approval_by_default=True,
        )

    async def execute(self, intent: ToolIntent):
        selector = intent.input_payload.get("selector", "")
        value = intent.input_payload.get("value", "")
        if not selector:
            return observation_from_output(intent, "FAILED", "No selector", error="Missing selector")
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(intent.input_payload.get("url", "about:blank"), timeout=15000)
                await page.fill(selector, value, timeout=5000)
                await browser.close()
                return observation_from_output(intent, "COMPLETED",
                    f"Filled {selector}", {"selector": selector, "value_length": len(value)})
        except ImportError:
            return observation_from_output(intent, "FAILED", "Playwright not installed", error="Missing playwright")
        except Exception as exc:
            return observation_from_output(intent, "FAILED", str(exc), error="Fill failed")


class BrowserScreenshotFullTool(Tool):
    """Take a full-page screenshot."""

    def __init__(self, artifacts_dir: Path) -> None:
        self.artifacts_dir = artifacts_dir
        self.definition = ToolDefinition(
            tool_name="browser.screenshot_full",
            display_name="Browser Full Screenshot",
            description="Take a full-page screenshot of a URL",
            category="browser",
            risk_level=RiskLevel.LOW,
            permissions=["browser.screenshot"],
            requires_approval_by_default=False,
        )

    async def execute(self, intent: ToolIntent):
        url = intent.input_payload.get("url", "")
        if not url:
            return observation_from_output(intent, "FAILED", "No URL", error="Missing url")
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(url, timeout=30000)
                path = self.artifacts_dir / f"screenshot_{intent.task_id[:8]}.png"
                await page.screenshot(path=str(path), full_page=True)
                await browser.close()
                return observation_from_output(intent, "COMPLETED",
                    f"Screenshot saved: {path}", {"path": str(path)})
        except ImportError:
            return observation_from_output(intent, "FAILED", "Playwright not installed", error="Missing playwright")
        except Exception as exc:
            return observation_from_output(intent, "FAILED", str(exc), error="Screenshot failed")

