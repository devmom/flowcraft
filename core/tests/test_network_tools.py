"""P1: Network Tools Tests — HTTP, search, download (mock transport)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from flowcraft_core.tools.network import HttpRequestTool, WebSearchTool, HttpDownloadTool
from flowcraft_core.domain.schemas import ToolIntent


def make_intent(tool_name: str, **payload) -> ToolIntent:
    return ToolIntent(
        task_id="t_net", step_id="s1", tool_name=tool_name,
        purpose="test", input_summary="x",
        input_payload=payload, expected_result="ok",
    )


class TestHttpRequestTool:
    """HTTP request tool — mock transport tests."""

    @pytest.mark.unit
    def test_definition_requires_approval(self) -> None:
        tool = HttpRequestTool()
        assert tool.definition.requires_approval_by_default is True
        assert tool.definition.tool_name == "http.request"

    @pytest.mark.unit
    def test_missing_url_fails(self) -> None:
        tool = HttpRequestTool()
        intent = make_intent("http.request")
        obs = asyncio.run(tool.execute(intent))
        assert obs.status == "FAILED"
        assert "url" in obs.output_summary.lower()

    @pytest.mark.unit
    def test_unsupported_method_fails(self) -> None:
        tool = HttpRequestTool()
        intent = make_intent("http.request", url="https://example.com", method="CONNECT")
        obs = asyncio.run(tool.execute(intent))
        assert obs.status == "FAILED"

    @pytest.mark.unit
    def test_valid_get_with_mock_transport(self, monkeypatch) -> None:
        """Mock _http_request to return a fake response."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html>Hello</html>"
        mock_resp.headers = {"Content-Type": "text/html"}

        async def mock_request(*args, **kwargs):
            return mock_resp

        monkeypatch.setattr(
            "flowcraft_core.tools.network._http_request", mock_request)
        tool = HttpRequestTool()
        intent = make_intent("http.request", url="https://example.com", method="GET")
        obs = asyncio.run(tool.execute(intent))
        assert obs.status == "COMPLETED"
        assert obs.output_payload.get("status_code") == 200


class TestWebSearchTool:
    """Web search tool tests."""

    @pytest.mark.unit
    def test_definition_low_risk(self) -> None:
        tool = WebSearchTool()
        assert tool.definition.tool_name == "web.search"

    @pytest.mark.unit
    def test_empty_query_fails(self) -> None:
        tool = WebSearchTool()
        intent = make_intent("web.search", query="")
        obs = asyncio.run(tool.execute(intent))
        assert obs.status == "FAILED"


class TestHttpDownloadTool:
    """HTTP download tool tests."""

    @pytest.mark.unit
    def test_definition(self) -> None:
        tool = HttpDownloadTool([pytest.importorskip("pathlib").Path("/tmp")])
        assert tool.definition.tool_name == "http.download"

    @pytest.mark.unit
    def test_missing_url_fails(self) -> None:
        tool = HttpDownloadTool([pytest.importorskip("pathlib").Path("/tmp")])
        intent = make_intent("http.download")
        obs = asyncio.run(tool.execute(intent))
        assert obs.status == "FAILED"
