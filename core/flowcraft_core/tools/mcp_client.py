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


# ── FlowCraft Tool Bridge ────────────────────────────────────

from flowcraft_core.tools.base import Tool, ToolDefinition, observation_from_output


class MCPToolBridge(Tool):
    """Adapts an MCP tool into a FlowCraft Tool-compatible interface.

    This bridge wraps an MCP tool from a connected server so it can be
    registered in FlowCraft's ToolRegistry and called by the execution engine.

    Usage:
        bridge = MCPToolBridge(server_client, server_name, mcp_tool_def)
        tool_registry.register(bridge)
    """

    def __init__(self, server_client: MCPStdioClient, server_name: str, tool_def: dict):
        self._server_client = server_client  # Direct client ref for efficiency
        self._server_name = server_name
        self._tool_name = tool_def.get("name", "unknown")
        self._tool_def = tool_def

        from flowcraft_core.domain.enums import RiskLevel

        # Build a FlowCraft ToolDefinition from MCP tool schema
        desc = tool_def.get("description", f"MCP tool: {self._tool_name}")
        input_schema = tool_def.get("inputSchema", {})
        props = input_schema.get("properties", {})
        if props:
            param_desc = ", ".join(
                f"{k}: {v.get('type', 'string')}"
                for k, v in list(props.items())[:8]
            )
            desc += f"\n参数: {param_desc}"

        self.definition = ToolDefinition(
            tool_name=f"mcp.{server_name}.{self._tool_name}",
            display_name=f"[MCP:{server_name}] {self._tool_name}",
            description=desc,
            category="mcp",
            risk_level=RiskLevel.MEDIUM,
            permissions=[f"mcp.{server_name}"],
            requires_approval_by_default=False,
            timeout_seconds=60,
        )

    async def execute(self, intent):
        """Execute the MCP tool via the server client (ToolIntent → ToolObservation)."""
        try:
            result = await self._server_client.call_tool(
                self._tool_name, intent.input_payload
            )
            # Extract text content from MCP result
            text = result
            if isinstance(result, dict):
                content = result.get("content", [])
                if isinstance(content, list) and content:
                    text = content[0].get("text", str(content))
                else:
                    text = str(result)
            elif isinstance(result, list):
                text = "\n".join(
                    item.get("text", str(item)) if isinstance(item, dict) else str(item)
                    for item in result
                )
            else:
                text = str(result)
            return observation_from_output(
                intent, "COMPLETED",
                f"MCP tool '{self._tool_name}' completed",
                {"server": self._server_name, "result": text},
            )
        except Exception as exc:
            logger.error("MCP tool '%s' failed: %s", self._tool_name, exc)
            return observation_from_output(
                intent, "FAILED",
                f"MCP tool '{self._tool_name}' failed: {exc}",
                error=str(exc),
            )


def load_mcp_config(config_path: str | None = None) -> list[MCPServerConfig]:
    """Load MCP server configurations from a JSON file or environment.

    Priority:
    1. config_path parameter (explicit file)
    2. FLOWCRAFT_MCP_CONFIG environment variable (path to JSON)
    3. .mcp.json in project root (auto-detected from cwd upward)
    4. .mcp.json in cwd
    """
    import json as _json
    from pathlib import Path as _Path

    config_file: _Path | None = None

    if config_path:
        config_file = _Path(config_path)
    else:
        env_path = __import__('os').environ.get("FLOWCRAFT_MCP_CONFIG")
        if env_path:
            config_file = _Path(env_path)
        else:
            # Walk up from cwd looking for .mcp.json
            cwd = _Path.cwd()
            for ancestor in [cwd] + list(cwd.parents):
                candidate = ancestor / ".mcp.json"
                if candidate.exists():
                    config_file = candidate
                    break

    if not config_file or not config_file.exists():
        logger.info("No MCP config found, skipping MCP servers")
        return []

    try:
        data = _json.loads(config_file.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to parse MCP config %s: %s", config_file, exc)
        return []

    servers = data.get("mcpServers", {})
    if not servers:
        return []

    configs: list[MCPServerConfig] = []
    for name, cfg in servers.items():
        command = cfg.get("command", "")
        if not command:
            logger.warning("MCP server '%s' has no command, skipping", name)
            continue
        args = cfg.get("args", [])
        env = cfg.get("env", {})
        configs.append(MCPServerConfig(
            name=name,
            command=command,
            args=args,
            env=env,
        ))

    logger.info("Loaded %d MCP server config(s) from %s", len(configs), config_file)
    return configs


async def register_mcp_tools(tool_registry, config_path: str | None = None) -> int:
    """Load MCP config, connect to servers, discover tools, and register them.

    Returns the number of MCP tools registered.
    """
    configs = load_mcp_config(config_path)
    if not configs:
        return 0

    registry = MCPToolRegistry()
    connected = 0
    total_tools = 0

    for cfg in configs:
        try:
            await registry.add_server(cfg)
            connected += 1

            # Get the specific server client
            server_client = registry._servers.get(cfg.name)
            if not server_client:
                continue

            # Discover tools from this server
            tools = await server_client.list_tools()
            for tool_def in tools:
                try:
                    bridge = MCPToolBridge(server_client, cfg.name, tool_def)
                    tool_registry.register(bridge)
                    total_tools += 1
                    logger.info(
                        "Registered MCP tool: mcp.%s.%s",
                        cfg.name, tool_def.get("name", "?")
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to register MCP tool '%s' from server '%s': %s",
                        tool_def.get("name", "?"), cfg.name, exc,
                    )
        except Exception as exc:
            logger.warning(
                "Failed to connect MCP server '%s' (%s %s): %s",
                cfg.name, cfg.command, " ".join(cfg.args), exc,
            )

    if connected > 0:
        logger.info(
            "MCP integration: %d/%d servers connected, %d tools registered",
            connected, len(configs), total_tools,
        )
    return total_tools
