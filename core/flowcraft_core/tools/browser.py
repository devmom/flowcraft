"""Browser Tool — 基于 Playwright 的网页自动化工具.

安全限制（Doc/10-Tool-Harness.md）：
- 不读取浏览器 Cookie 或密码管理器
- 阻止浏览器内部页面（file://, chrome:// 等）
- 跨站提交表单前需确认
"""

from __future__ import annotations

from pathlib import Path

from flowcraft_core.domain.enums import RiskLevel
from flowcraft_core.domain.schemas import ToolIntent
from flowcraft_core.tools.base import Tool, ToolDefinition, observation_from_output

# Blocked URL schemes for browser safety
BLOCKED_URL_SCHEMES = [
    "file://", "chrome://", "about:", "edge://",
    "chrome-extension://", "moz-extension://", "view-source:",
    "data:", "javascript:", "vbscript:",
]


class BrowserReadTool(Tool):
    """读取网页内容（文本提取）—— Playwright 优先, httpx 兜底."""

    def __init__(self) -> None:
        self.definition = ToolDefinition(
            tool_name="browser.read",
            display_name="读取网页",
            description="打开 URL 并提取网页文本内容（去除 HTML 标签）。不会读取Cookie或密码。Playwright不可用时自动使用httpx。",
            category="browser",
            risk_level=RiskLevel.LOW,
            permissions=["tool:browser.read"],
            timeout_seconds=30,
        )

    async def execute(self, intent: ToolIntent):
        url = str(intent.input_payload.get("url", ""))
        if not url:
            return observation_from_output(intent, "FAILED", "缺少 URL 参数。", error="Missing url.")

        # Security: block internal/dangerous URL schemes
        url_lower = url.lower()
        for scheme in BLOCKED_URL_SCHEMES:
            if url_lower.startswith(scheme):
                return observation_from_output(
                    intent, "DENIED",
                    f"安全策略禁止访问此协议: {url}",
                    error="Blocked URL scheme.",
                    payload={"blocked": scheme})

        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        # ── Method 1: Playwright (best quality, renders JavaScript) ──
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                try:
                    await page.goto(url, timeout=20000, wait_until="domcontentloaded")
                    title = await page.title()
                    text = await page.inner_text("body")
                    if len(text) > 12000:
                        text = text[:12000] + "\n\n[内容过长，已截断]"
                    await browser.close()
                    return observation_from_output(
                        intent, "COMPLETED",
                        f"已读取网页: {title}",
                        {"url": url, "title": title, "content": text, "render_method": "playwright"},
                    )
                except Exception as exc:
                    await browser.close()
                    # Playwright failed → fall through to httpx fallback
                    raise exc
        except (ImportError, Exception):
            pass  # Playwright unavailable or failed → use httpx fallback

        # ── Method 2: httpx fallback (basic HTML text extraction) ──
        try:
            import httpx
            import re as _re_html

            async with httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=8.0),
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                follow_redirects=True,
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                html = resp.text

                # Extract title
                title_match = _re_html.search(r'<title[^>]*>(.*?)</title>', html, _re_html.IGNORECASE | _re_html.DOTALL)
                title = _re_html.sub(r'<[^>]+>', '', title_match.group(1)).strip() if title_match else url

                # Extract text: remove scripts, styles, and HTML tags
                # 1. Remove script and style blocks
                html = _re_html.sub(r'<script[^>]*>.*?</script>', '', html, flags=_re_html.DOTALL | _re_html.IGNORECASE)
                html = _re_html.sub(r'<style[^>]*>.*?</style>', '', html, flags=_re_html.DOTALL | _re_html.IGNORECASE)
                # 2. Replace common block elements with newlines
                html = _re_html.sub(r'</?(?:div|p|li|h[1-6]|tr|br|article|section|header|footer|nav)[^>]*>', '\n', html, flags=_re_html.IGNORECASE)
                # 3. Remove all remaining HTML tags
                text = _re_html.sub(r'<[^>]+>', '', html)
                # 4. Decode HTML entities
                text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"').replace('&#39;', "'").replace('&nbsp;', ' ')
                # 5. Collapse whitespace
                text = _re_html.sub(r'\n\s*\n', '\n', text)
                text = _re_html.sub(r' +', ' ', text)
                text = '\n'.join(line.strip() for line in text.split('\n') if line.strip())

                if len(text) > 12000:
                    text = text[:12000] + "\n\n[内容过长，已截断]"

                if not text.strip():
                    return observation_from_output(intent, "COMPLETED",
                        f"已读取网页 (无文本内容): {title}",
                        {"url": url, "title": title, "content": ""})

                return observation_from_output(
                    intent, "COMPLETED",
                    f"已读取网页 (httpx): {title}",
                    {"url": url, "title": title, "content": text, "render_method": "httpx"},
                )
        except ImportError:
            return observation_from_output(
                intent, "FAILED",
                "Playwright 和 httpx 都不可用。运行: pip install httpx",
                error="Neither Playwright nor httpx available.",
            )
        except httpx.HTTPStatusError as exc:
            return observation_from_output(
                intent, "FAILED", f"HTTP {exc.response.status_code}: {url}",
                error=str(exc), payload={"url": url, "status_code": exc.response.status_code},
            )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            return observation_from_output(
                intent, "FAILED", f"网页请求超时: {url}",
                error=str(exc), payload={"url": url},
            )
        except Exception as exc:
            return observation_from_output(
                intent, "FAILED", f"网页加载失败: {exc}",
                error=str(exc), payload={"url": url},
            )


class BrowserScreenshotTool(Tool):
    """网页截图工具。"""

    def __init__(self, output_dir: Path | None = None) -> None:
        self.output_dir = output_dir or Path.cwd()
        self.definition = ToolDefinition(
            tool_name="browser.screenshot",
            display_name="网页截图",
            description="打开 URL 并截取页面全屏截图，保存为 PNG。",
            category="browser",
            risk_level=RiskLevel.LOW,
            permissions=["tool:browser.screenshot"],
            timeout_seconds=30,
        )

    async def execute(self, intent: ToolIntent):
        url = str(intent.input_payload.get("url", ""))
        if not url:
            return observation_from_output(intent, "FAILED", "缺少 URL 参数。", error="Missing url.")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return observation_from_output(
                intent, "FAILED", "Playwright 未安装。",
                error="Playwright not installed.",
            )

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await page.goto(url, timeout=20000, wait_until="domcontentloaded")
                output_path = self.output_dir / f"screenshot_{intent.tool_intent_id}.png"
                await page.screenshot(path=str(output_path), full_page=True)
                title = await page.title()
                await browser.close()
                return observation_from_output(
                    intent, "COMPLETED",
                    f"已截图: {title}",
                    {"url": url, "title": title, "screenshot_path": str(output_path)},
                )
            except Exception as exc:
                await browser.close()
                return observation_from_output(
                    intent, "FAILED", f"截图失败: {exc}",
                    error=str(exc), payload={"url": url},
                )
