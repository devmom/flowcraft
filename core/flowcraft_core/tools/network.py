"""Network tools — HTTP client and web search.

Transport: httpx.AsyncClient (truly async, non-blocking, cancellable).
"""

from __future__ import annotations

import asyncio
import json as _json
import re as _re
from pathlib import Path
from typing import Any

import httpx

from flowcraft_core.domain.enums import RiskLevel
from flowcraft_core.domain.schemas import ToolIntent
from flowcraft_core.logging_config import get_trace_logger
from urllib.parse import quote as _url_quote

from flowcraft_core.tools.base import Tool, ToolDefinition, is_path_allowed, observation_from_output

trace = get_trace_logger("tools.network")

# Shared httpx client (lazy-init per event loop)
_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    """Get or create a shared httpx client with sensible defaults."""
    global _client
    if _client is None or _client.is_closed:
        async with _client_lock:
            if _client is None or _client.is_closed:
                _client = httpx.AsyncClient(
                    timeout=httpx.Timeout(30.0, connect=10.0),
                    headers={"User-Agent": "FlowCraft/0.1.0"},
                    follow_redirects=True,
                )
    return _client


async def _http_get(url: str, headers: dict | None = None, timeout: float = 15.0) -> httpx.Response:
    """Async HTTP GET with proper timeout handling."""
    client = await _get_client()
    req_headers = {"User-Agent": "FlowCraft/0.1.0", "Accept": "application/json, text/plain, */*"}
    if headers:
        req_headers.update(headers)
    trace.debug(None, "network.http_get",
               f"GET {url[:80]} timeout={timeout}s",
               extra={"url": url[:100], "timeout": timeout})
    return await client.get(url, headers=req_headers, timeout=timeout)


async def _http_post(url: str, data: bytes | None = None, headers: dict | None = None, timeout: float = 15.0) -> httpx.Response:
    """Async HTTP POST."""
    client = await _get_client()
    req_headers = {"User-Agent": "FlowCraft/0.1.0", "Accept": "application/json, text/plain, */*"}
    if headers:
        req_headers.update(headers)
    return await client.post(url, content=data, headers=req_headers, timeout=timeout)


async def _http_request(method: str, url: str, data: bytes | None = None,
                        headers: dict | None = None, timeout: float = 15.0) -> httpx.Response:
    """Generic async HTTP request."""
    client = await _get_client()
    req_headers = {"User-Agent": "FlowCraft/0.1.0", "Accept": "application/json, text/plain, */*"}
    if headers:
        req_headers.update(headers)
    trace.debug(None, "network.http_request",
               f"{method} {url[:80]} timeout={timeout}s",
               extra={"method": method, "url": url[:100], "timeout": timeout})
    return await client.request(method, url, content=data, headers=req_headers, timeout=timeout)


class HttpRequestTool(Tool):
    """Generic HTTP client — agent specifies method, url, headers, body."""

    def __init__(self) -> None:
        self.definition = ToolDefinition(
            tool_name="http.request",
            display_name="HTTP请求",
            description=(
                "发送 HTTP 请求。参数: method(GET/POST/PUT/DELETE/PATCH), "
                "url, headers(对象), body(字符串), timeout_seconds(默认15)。"
                "返回: status_code, headers, body(截断到50KB)"
            ),
            category="network",
            risk_level=RiskLevel.MEDIUM,
            permissions=["network.http"],
            requires_approval_by_default=True,
            timeout_seconds=30,
        )

    async def execute(self, intent: ToolIntent):
        url = str(intent.input_payload.get("url", ""))
        method = str(intent.input_payload.get("method", "GET")).upper()
        headers = dict(intent.input_payload.get("headers", {}))
        body_str = str(intent.input_payload.get("body", ""))
        timeout = float(intent.input_payload.get("timeout_seconds", 15))

        if not url:
            return observation_from_output(intent, "FAILED", "Missing url")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        if method not in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
            return observation_from_output(intent, "FAILED",
                f"Unsupported method: {method}")

        try:
            data = body_str.encode("utf-8") if body_str else None
            resp = await _http_request(method, url, data=data, headers=headers, timeout=timeout)
            resp_body = resp.text
            if len(resp_body) > 50000:
                resp_body = resp_body[:50000] + "\n\n[Content truncated at 50KB]"

            return observation_from_output(intent, "COMPLETED",
                f"HTTP {method} {url} -> {resp.status_code}",
                {
                    "url": url, "method": method,
                    "status_code": resp.status_code,
                    "response_headers": dict(resp.headers),
                    "body": resp_body,
                    "body_length": len(resp_body),
                })

        except httpx.HTTPStatusError as exc:
            try:
                err_body = exc.response.text[:5000]
            except Exception:
                err_body = ""
            return observation_from_output(intent, "FAILED",
                f"HTTP {exc.response.status_code}: {exc.response.reason_phrase}",
                error=str(exc),
                payload={"url": url, "status_code": exc.response.status_code,
                         "reason": exc.response.reason_phrase, "body": err_body})

        except httpx.TimeoutException as exc:
            return observation_from_output(intent, "FAILED",
                f"Request timeout after {timeout}s: {url}",
                error=str(exc), payload={"url": url, "timeout": timeout})

        except (httpx.RequestError, OSError) as exc:
            return observation_from_output(intent, "FAILED",
                f"Network error: {exc}",
                error=str(exc), payload={"url": url})

        except Exception as exc:
            return observation_from_output(intent, "FAILED", str(exc))


class WebSearchTool(Tool):
    """Web search with multi-provider fallback (DuckDuckGo -> Bing -> DuckDuckGo HTML).

    Automatically falls through providers to find one that works in the current network.
    DuckDuckGo works outside China; Bing (cn.bing.com) works inside China.
    """

    def __init__(self) -> None:
        self.definition = ToolDefinition(
            tool_name="web.search",
            display_name="Web Search",
            description=(
                "Search the web. Params: query (search terms), max_results (default 5). "
                "Auto-selects from DuckDuckGo, Bing, etc. No API key needed."
            ),
            category="network",
            risk_level=RiskLevel.MEDIUM,
            permissions=["network.http"],
            requires_approval_by_default=False,
            timeout_seconds=25,
        )

    async def execute(self, intent: ToolIntent):
        query = str(intent.input_payload.get("query", ""))
        max_results = int(intent.input_payload.get("max_results", 5))

        if not query:
            return observation_from_output(intent, "FAILED", "Missing query")

        last_error = ""
        results: list[dict] = []

        # ── Provider 1: DuckDuckGo Instant Answer API ──
        try:
            results = await self._search_duckduckgo_api(query, max_results)
            if results:
                return observation_from_output(intent, "COMPLETED",
                    f"Found {len(results)} results (DuckDuckGo) for '{query}'",
                    {"query": query, "results": results, "count": len(results), "provider": "duckduckgo"})
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ConnectTimeout) as exc:
            last_error = f"DuckDuckGo API: {exc}"
        except Exception as exc:
            last_error = f"DuckDuckGo API: {exc}"

        # ── Provider 2: Bing search (cn.bing.com, works in China) ──
        try:
            results = await self._search_bing(query, max_results)
            if results:
                return observation_from_output(intent, "COMPLETED",
                    f"Found {len(results)} results (Bing) for '{query}'",
                    {"query": query, "results": results, "count": len(results), "provider": "bing"})
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ConnectTimeout) as exc:
            last_error += f"; Bing: {exc}"
        except Exception as exc:
            last_error += f"; Bing: {exc}"

        # ── Provider 3: DuckDuckGo HTML scrape (last resort) ──
        try:
            results = await self._fallback_html_search_async(query, max_results)
            if results:
                return observation_from_output(intent, "COMPLETED",
                    f"Found {len(results)} results (DuckDuckGo HTML) for '{query}'",
                    {"query": query, "results": results, "count": len(results), "provider": "duckduckgo_html"})
        except Exception as exc:
            last_error += f"; DuckDuckGo HTML: {exc}"

        # All providers failed
        if not results:
            return observation_from_output(intent, "FAILED",
                f"Search failed for '{query}'. All providers unreachable. {last_error}",
                error=last_error, payload={"query": query, "results": []})

        return observation_from_output(intent, "COMPLETED",
            f"Found {len(results)} results for '{query}'",
            {"query": query, "results": results, "count": len(results)})

    # ── Provider implementations ──────────────────────────

    @staticmethod
    async def _search_duckduckgo_api(query: str, max_results: int) -> list[dict]:
        """DuckDuckGo Instant Answer API."""
        ddg_url = f"https://api.duckduckgo.com/?q={_url_quote(query)}&format=json&no_html=1&skip_disambig=1"
        resp = await _http_get(ddg_url, timeout=5.0)
        data = resp.json()
        results = []

        if data.get("AbstractText"):
            results.append({
                "title": data.get("Heading", query),
                "url": data.get("AbstractURL", ""),
                "snippet": data.get("AbstractText", ""),
                "source": data.get("AbstractSource", "DuckDuckGo"),
                "type": "instant_answer",
            })

        for topic in data.get("RelatedTopics", [])[:max_results]:
            if isinstance(topic, dict) and topic.get("Text"):
                results.append({
                    "title": topic.get("FirstURL", "").rsplit("/", 1)[-1].replace("_", " "),
                    "url": topic.get("FirstURL", ""),
                    "snippet": topic.get("Text", ""),
                    "source": "DuckDuckGo",
                    "type": "related",
                })
        return results

    @staticmethod
    async def _search_bing(query: str, max_results: int) -> list[dict]:
        """Bing search via HTML scraping (cn.bing.com, no API key needed).

        Works in China and globally. Parses Bing's HTML result page.
        """
        bing_url = f"https://cn.bing.com/search?q={_url_quote(query)}&setlang=en"
        resp = await _http_get(bing_url, timeout=8.0,
                               headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                                        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7"})
        html = resp.text

        results = []
        # Bing result patterns: each result is in <li class="b_algo">
        # Title: <h2><a href="...">Title</a></h2>
        # Snippet: <p> or <div class="b_caption"><p>
        algo_pattern = _re.compile(
            r'<li class="b_algo"[^>]*>.*?<h2[^>]*>.*?<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?</h2>.*?<p[^>]*>(.*?)</p>',
            _re.DOTALL | _re.IGNORECASE)

        matches = algo_pattern.findall(html)
        for url, title_raw, snippet_raw in matches[:max_results]:
            title = _re.sub(r'<[^>]+>', '', title_raw).strip()
            snippet = _re.sub(r'<[^>]+>', '', snippet_raw).strip()
            if title and (snippet or url):
                # Also try to find a cleaner snippet
                # Remove extra whitespace and HTML entities
                snippet = _re.sub(r'&[a-z]+;', ' ', snippet)
                snippet = _re.sub(r'\s+', ' ', snippet).strip()
                results.append({
                    "title": title,
                    "url": url,
                    "snippet": snippet[:500],
                    "source": "Bing",
                    "type": "web_result",
                })

        # Fallback: broader pattern if b_algo not found
        if not results:
            link_pattern = _re.compile(
                r'<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>',
                _re.DOTALL | _re.IGNORECASE)
            snippet_pattern = _re.compile(
                r'<p[^>]*>(.*?)</p>',
                _re.DOTALL | _re.IGNORECASE)

            links = link_pattern.findall(html)
            snippets = snippet_pattern.findall(html)

            for i in range(min(len(links), max_results)):
                url = links[i][0] if links[i] else ""
                title = _re.sub(r'<[^>]+>', '', links[i][1]).strip() if len(links[i]) > 1 else ""
                snippet = _re.sub(r'<[^>]+>', '', snippets[i]).strip() if i < len(snippets) else ""

                # Skip navigation/utility links
                if any(skip in url for skip in ('bing.com', 'microsoft.com/bing', 'go.microsoft.com')):
                    continue
                if not url.startswith('http'):
                    continue

                results.append({
                    "title": title or f"Result {i+1}",
                    "url": url,
                    "snippet": snippet[:500],
                    "source": "Bing",
                    "type": "web_result",
                })

        return results[:max_results]

    @staticmethod
    async def _fallback_html_search_async(query: str, max_results: int) -> list[dict]:
        """Fallback: scrape DuckDuckGo HTML results (async)."""
        try:
            url = f"https://html.duckduckgo.com/html/?q={_url_quote(query)}"
            resp = await _http_get(url, timeout=3.0,
                                   headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
            html = resp.text

            results = []
            link_pattern = _re.compile(
                r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
                _re.DOTALL | _re.IGNORECASE)
            snippet_pattern = _re.compile(
                r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
                _re.DOTALL | _re.IGNORECASE)

            links = link_pattern.findall(html)
            snippets = snippet_pattern.findall(html)

            for i in range(min(len(links), max_results)):
                url_match = links[i][0] if links[i] else ""
                title = _re.sub(r'<[^>]+>', '', links[i][1] if len(links[i]) > 1 else "").strip()
                snippet = _re.sub(r'<[^>]+>', '', snippets[i] if i < len(snippets) else "").strip()
                results.append({
                    "title": title or f"Result {i + 1}",
                    "url": url_match,
                    "snippet": snippet,
                    "source": "DuckDuckGo",
                    "type": "web_result",
                })
            return results
        except Exception:
            return []


class HttpDownloadTool(Tool):
    """Download a file from URL to local directory (async)."""

    def __init__(self, allowed_paths: list[Path]) -> None:
        self.allowed_paths = allowed_paths
        self.definition = ToolDefinition(
            tool_name="http.download",
            display_name="Download File",
            description=(
                "Download a file from URL to local authorized directory. "
                "Params: url, save_path, overwrite (default false)."
            ),
            category="network",
            risk_level=RiskLevel.MEDIUM,
            permissions=["network.http", "tool:file.write"],
            requires_approval_by_default=True,
            timeout_seconds=120,
        )

    async def execute(self, intent: ToolIntent):
        url = str(intent.input_payload.get("url", ""))
        save_path_str = str(intent.input_payload.get("save_path", ""))
        overwrite = bool(intent.input_payload.get("overwrite", False))

        if not url:
            return observation_from_output(intent, "FAILED", "Missing url")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        # Determine save path
        if save_path_str:
            save_path = Path(save_path_str)
        else:
            from urllib.parse import urlparse as _urlparse
            filename = Path(_urlparse(url).path).name or "downloaded_file"
            save_path = self.allowed_paths[0] / filename

        if not save_path.is_absolute():
            save_path = self.allowed_paths[0] / save_path

        if not is_path_allowed(save_path, self.allowed_paths):
            return observation_from_output(intent, "DENIED", "Save path not allowed.",
                payload={"action": "ask_user_for_permission"})

        if save_path.exists() and not overwrite:
            return observation_from_output(intent, "FAILED",
                f"File exists: {save_path.name}. Set overwrite=true to replace.")

        try:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            resp = await _http_get(url, timeout=60.0)
            content = resp.content
            save_path.write_bytes(content)

            return observation_from_output(intent, "COMPLETED",
                f"Downloaded: {save_path.name} ({len(content)} bytes)",
                {"url": url, "save_path": str(save_path), "size": len(content)})

        except httpx.TimeoutException as exc:
            return observation_from_output(intent, "FAILED",
                f"Download timeout: {url}", error=str(exc))

        except Exception as exc:
            return observation_from_output(intent, "FAILED", str(exc))
