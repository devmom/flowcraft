"""MCP (Model Context Protocol) Client for FlowCraft.

Integrates external MCP Servers into FlowCraft's tool system.
Supports stdio transport (local MCP servers).

MCP provides three primitives:
  - Tools: Model-controlled functions (like Function Calling but standardized)
  - Resources: Application-controlled data (files, DB schemas, etc.)
  - Prompts: Pre-defined prompt templates

Reference: https://modelcontextprotocol.io/
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MCPServerConfig:
    """Configuration for an MCP server."""
    name: str
    command: str           # e.g., "npx" or "python"
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    transport: str = "stdio"  # Currently only stdio supported


class MCPStdioClient:
    """MCP client using stdio transport.

    Spawns the MCP server as a subprocess and communicates via stdin/stdout
    using JSON-RPC 2.0 messages.

    Usage:
        client = MCPStdioClient(MCPServerConfig(
            name="filesystem",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"],
        ))
        await client.connect()
        tools = await client.list_tools()
        result = await client.call_tool("read_file", {"path": "/some/file.txt"})
    """

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self._process: subprocess.Popen | None = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None

    async def connect(self) -> None:
        """Start the MCP server subprocess and initialize."""
        self._process = subprocess.Popen(
            [self.config.command] + self.config.args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**__import__('os').environ, **self.config.env},
        )
        self._reader_task = asyncio.create_task(self._read_responses())

        # Initialize
        result = await self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "FlowCraft", "version": "0.2.0"},
        })
        logger.info("MCP server '%s' initialized: %s", self.config.name, result)

    async def disconnect(self) -> None:
        """Stop the MCP server."""
        if self._reader_task:
            self._reader_task.cancel()
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()

    async def list_tools(self) -> list[dict]:
        """List all tools provided by this MCP server."""
        result = await self._send_request("tools/list", {})
        return result.get("tools", [])

    async def list_resources(self) -> list[dict]:
        """List all resources provided by this MCP server."""
        result = await self._send_request("resources/list", {})
        return result.get("resources", [])

    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        """Call a tool on the MCP server."""
        result = await self._send_request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })
        if "error" in result:
            raise RuntimeError(f"MCP tool '{tool_name}' error: {result['error']}")
        # Extract content from result
        content = result.get("content", [])
        if content and isinstance(content, list):
            return content[0].get("text", str(content))
        return result

    async def _send_request(self, method: str, params: dict) -> dict:
        """Send a JSON-RPC request and wait for response."""
        if not self._process or not self._process.stdin:
            raise RuntimeError("MCP client not connected")

        self._request_id += 1
        rid = self._request_id

        request = json.dumps({
            "jsonrpc": "2.0",
            "id": rid,
            "method": method,
            "params": params,
        }) + "\n"

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[rid] = future

        self._process.stdin.write(request)
        self._process.stdin.flush()

        try:
            return await asyncio.wait_for(future, timeout=30.0)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise TimeoutError(f"MCP request '{method}' timed out after 30s")

    async def _read_responses(self) -> None:
        """Background task: read responses from MCP server stdout."""
        if not self._process or not self._process.stdout:
            return
        while True:
            try:
                line = await asyncio.get_event_loop().run_in_executor(
                    None, self._process.stdout.readline,
                )
                if not line:
                    break
                response = json.loads(line.strip())
                rid = response.get("id")
                if rid and rid in self._pending:
                    future = self._pending.pop(rid)
                    if "error" in response:
                        future.set_exception(RuntimeError(str(response["error"])))
                    else:
                        future.set_result(response.get("result", {}))
            except Exception as exc:
                logger.debug("MCP reader error: %s", exc)
                break


class MCPToolRegistry:
    """Registry that aggregates tools from multiple MCP servers.

    Usage:
        registry = MCPToolRegistry()
        await registry.add_server(MCPServerConfig(name="filesystem", ...))
        tools = await registry.list_all_tools()
    """

    def __init__(self):
        self._servers: dict[str, MCPStdioClient] = {}

    async def add_server(self, config: MCPServerConfig) -> None:
        """Add and connect an MCP server."""
        if config.name in self._servers:
            logger.warning("MCP server '%s' already registered", config.name)
            return
        client = MCPStdioClient(config)
        await client.connect()
        self._servers[config.name] = client
        logger.info("MCP server '%s' registered (%s)", config.name, config.command)

    async def remove_server(self, name: str) -> None:
        """Disconnect and remove an MCP server."""
        client = self._servers.pop(name, None)
        if client:
            await client.disconnect()

    async def list_all_tools(self) -> list[dict]:
        """Aggregate tools from all MCP servers."""
        all_tools = []
        for name, client in self._servers.items():
            try:
                tools = await client.list_tools()
                for tool in tools:
                    tool["_server"] = name
                    all_tools.append(tool)
            except Exception as exc:
                logger.warning("Failed to list tools from MCP server '%s': %s", name, exc)
        return all_tools

    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        """Route a tool call to the correct MCP server."""
        for client in self._servers.values():
            tools = await client.list_tools()
            if any(t.get("name") == tool_name for t in tools):
                return await client.call_tool(tool_name, arguments)
        raise ValueError(f"MCP tool '{tool_name}' not found in any server")

    async def shutdown(self) -> None:
        """Disconnect all servers."""
        for name in list(self._servers.keys()):
            await self.remove_server(name)


# ── SSE Transport (Remote MCP) ──────────────────────────────

class MCPSSEClient:
    """MCP client using SSE (Server-Sent Events) transport for remote servers.

    Unlike stdio (local subprocess), SSE enables communication with
    MCP servers running on remote machines or in containers.

    Reference: MCP Transport specification (2024-11-05)

    Usage:
        client = MCPSSEClient("https://mcp-server.example.com/sse")
        await client.connect()
        tools = await client.list_tools()
    """

    def __init__(self, endpoint_url: str, api_key: str | None = None):
        self.url = endpoint_url
        self.api_key = api_key
        self._session_id: str | None = None
        self._request_id = 0

    async def connect(self) -> None:
        """Establish SSE connection to the MCP server."""
        import httpx
        headers = {"Accept": "text/event-stream"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.url}/initialize",
                json={
                    "jsonrpc": "2.0",
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "FlowCraft", "version": "0.2.0"},
                    },
                },
                headers={"Content-Type": "application/json", **headers},
            )
            data = resp.json()
            self._session_id = data.get("result", {}).get("sessionId", "")
        logger.info("MCP SSE connected: %s (session=%s)", self.url, self._session_id)

    async def list_tools(self) -> list[dict]:
        """List tools from remote MCP server."""
        return await self._call("tools/list", {})

    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        """Call a tool on the remote MCP server."""
        return await self._call("tools/call", {"name": tool_name, "arguments": arguments})

    async def _call(self, method: str, params: dict) -> Any:
        """Make a JSON-RPC call to the remote MCP server."""
        import httpx
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(self.url, json=payload, headers=headers)
            data = resp.json()
            if "error" in data:
                raise RuntimeError(f"MCP SSE error: {data['error']}")
            return data.get("result", {})
