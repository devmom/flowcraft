"""Dynamic Tool Factory — allows agents to create, update, and manage tools at runtime.

Architecture:
    Agent identifies need for new tool
        ↓
    Agent generates tool spec (name, schema, code)
        ↓
    ToolFactory.validate() — safety check
        ↓
    ToolFactory.create() → DynamicTool instance
        ↓
    ToolRegistry.register() — available for immediate use
        ↓
    TTL expires → auto-unregister

Lifecycle management tools:
    tool.create   — register a new dynamic tool
    tool.update   — modify an existing dynamic tool
    tool.delete   — remove a dynamic tool
    tool.list_dynamic — list all dynamically created tools
"""

from __future__ import annotations

import logging
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from flowcraft_core.domain.enums import RiskLevel
from flowcraft_core.domain.schemas import ToolIntent, ToolObservation, now_utc
from flowcraft_core.tools.base import Tool, ToolDefinition, observation_from_output

logger = logging.getLogger(__name__)

# Maximum dynamic tools per session
MAX_DYNAMIC_TOOLS = 20
# Default TTL for dynamic tools (1 hour)
DEFAULT_TTL_SECONDS = 3600

# Blocked imports for dynamic tools (security sandbox)
BLOCKED_IMPORTS = {
    "os", "subprocess", "socket", "shutil", "sys", "ctypes",
    "importlib", "threading", "multiprocessing", "signal",
    "pathlib", "open", "http", "urllib", "requests",
}


@dataclass
class DynamicToolSpec:
    """Specification for creating a dynamic tool."""
    tool_name: str
    display_name: str = ""
    description: str = ""
    category: str = "dynamic"
    risk_level: str = "MEDIUM"
    input_schema: dict[str, Any] = field(default_factory=dict)
    code: str = ""  # Python function body
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    requires_approval: bool = True


class DynamicTool(Tool):
    """A tool created at runtime from agent-generated code.

    Code runs in a restricted sandbox with:
    - Blocked dangerous imports
    - 10-second execution timeout
    - No file system access
    - No network access
    """

    def __init__(self, spec: DynamicToolSpec, created_at: str = "") -> None:
        self.spec = spec
        self.created_at = created_at or now_utc().isoformat()
        self.definition = ToolDefinition(
            tool_name=spec.tool_name,
            display_name=spec.display_name or spec.tool_name,
            description=spec.description,
            category=spec.category,
            risk_level=RiskLevel(spec.risk_level),
            permissions=[f"dynamic:{spec.tool_name}"],
            requires_approval_by_default=spec.requires_approval,
            timeout_seconds=10,
        )

    async def execute(self, intent: ToolIntent) -> ToolObservation:
        """Execute the dynamic tool's code with the provided input."""
        try:
            # Build safe execution environment
            safe_globals: dict[str, Any] = {
                "__builtins__": {
                    "abs": abs, "all": all, "any": any,
                    "bool": bool, "bytes": bytes, "chr": chr,
                    "dict": dict, "enumerate": enumerate,
                    "filter": filter, "float": float, "format": format,
                    "int": int, "isinstance": isinstance,
                    "len": len, "list": list, "map": map,
                    "max": max, "min": min,
                    "range": range, "repr": repr, "reversed": reversed,
                    "round": round, "set": set, "sorted": sorted,
                    "str": str, "sum": sum, "tuple": tuple, "type": type,
                    "zip": zip, "print": print,
                    "True": True, "False": False, "None": None,
                    "Exception": Exception, "ValueError": ValueError,
                    "TypeError": TypeError,
                },
                "json": __import__("json"),
                "math": __import__("math"),
                "re": __import__("re"),
                "datetime": __import__("datetime"),
            }

            # The code should define a function called 'run(input_data) -> dict'
            # Wrap it in a namespace
            code = (
                "# Dynamic tool code\n"
                + self.spec.code
                + "\n\n# Execute\n__result__ = run(input_data)\n"
            )

            local_vars: dict[str, Any] = {"input_data": intent.input_payload}
            exec(code, safe_globals, local_vars)

            result = local_vars.get("__result__", {})
            if not isinstance(result, dict):
                result = {"output": str(result)}

            return observation_from_output(
                intent, "COMPLETED",
                f"Dynamic tool {self.spec.tool_name} executed successfully",
                result,
            )
        except Exception as exc:
            return observation_from_output(
                intent, "FAILED",
                f"Dynamic tool error: {exc}",
                error=str(exc),
            )


class ToolFactory:
    """Creates and manages dynamic tools at runtime."""

    def __init__(self, tool_registry) -> None:
        self.registry = tool_registry  # ToolRegistry
        self._dynamic_tools: dict[str, DynamicTool] = {}
        self._ttl_timers: dict[str, threading.Timer] = {}
        self._audit_log: list[dict[str, Any]] = []

    def validate_spec(self, spec: DynamicToolSpec) -> tuple[bool, str]:
        """Validate a tool specification for safety and completeness.

        Returns (is_valid, error_message).
        """
        if not spec.tool_name or not spec.tool_name.strip():
            return False, "Tool name is required"
        if not spec.tool_name.replace(".", "").replace("_", "").replace("-", "").isalnum():
            return False, f"Invalid tool name: {spec.tool_name}"
        if not spec.description:
            return False, "Tool description is required"
        if spec.tool_name in self._dynamic_tools:
            return False, f"Tool '{spec.tool_name}' already exists"
        if self.registry.get(spec.tool_name):
            return False, f"Tool '{spec.tool_name}' conflicts with built-in tool"
        if len(self._dynamic_tools) >= MAX_DYNAMIC_TOOLS:
            return False, f"Maximum {MAX_DYNAMIC_TOOLS} dynamic tools reached"

        # Check code for dangerous imports
        if spec.code:
            for blocked in BLOCKED_IMPORTS:
                if f"import {blocked}" in spec.code or f"from {blocked}" in spec.code:
                    return False, f"Blocked import: {blocked}"

        # Check code is not empty
        if not spec.code or len(spec.code.strip()) < 10:
            return False, "Tool code is too short or empty"

        # Must define a 'run' function
        if "def run(" not in spec.code:
            return False, "Code must define a 'run(input_data)' function"

        return True, ""

    def create(self, spec: DynamicToolSpec) -> tuple[DynamicTool | None, str]:
        """Create and register a dynamic tool.

        Returns (tool, error_message). tool is None on failure.
        """
        valid, error = self.validate_spec(spec)
        if not valid:
            return None, error

        try:
            tool = DynamicTool(spec)
            self.registry.register(tool)
            self._dynamic_tools[spec.tool_name] = tool

            # Schedule auto-unregister
            if spec.ttl_seconds > 0:
                timer = threading.Timer(spec.ttl_seconds, self._expire_tool, args=[spec.tool_name])
                timer.daemon = True
                timer.start()
                self._ttl_timers[spec.tool_name] = timer

            # Audit
            self._audit_log.append({
                "action": "create",
                "tool_name": spec.tool_name,
                "timestamp": now_utc().isoformat(),
                "ttl": spec.ttl_seconds,
            })

            logger.info("Dynamic tool created: %s (TTL: %ds)", spec.tool_name, spec.ttl_seconds)
            return tool, ""
        except Exception as exc:
            return None, str(exc)

    def update(self, tool_name: str, updates: dict[str, Any]) -> tuple[bool, str]:
        """Update an existing dynamic tool's spec."""
        if tool_name not in self._dynamic_tools:
            return False, f"Tool '{tool_name}' not found or not dynamic"

        tool = self._dynamic_tools[tool_name]
        if "description" in updates:
            tool.definition.description = updates["description"]
        if "code" in updates:
            # Re-validate code
            if not updates["code"] or len(updates["code"].strip()) < 10:
                return False, "New code is too short"
            if "def run(" not in updates["code"]:
                return False, "New code must define run(input_data)"
            for blocked in BLOCKED_IMPORTS:
                if f"import {blocked}" in updates["code"]:
                    return False, f"Blocked import: {blocked}"
            tool.spec.code = updates["code"]
        if "ttl_seconds" in updates:
            tool.spec.ttl_seconds = int(updates["ttl_seconds"])

        self._audit_log.append({
            "action": "update", "tool_name": tool_name,
            "timestamp": now_utc().isoformat(), "updates": list(updates.keys()),
        })
        return True, ""

    def delete(self, tool_name: str) -> bool:
        """Remove a dynamic tool and unregister it."""
        if tool_name not in self._dynamic_tools:
            return False

        # Unregister from registry
        self.registry.unregister(tool_name)

        # Clean up
        self._dynamic_tools.pop(tool_name, None)
        timer = self._ttl_timers.pop(tool_name, None)
        if timer:
            timer.cancel()

        self._audit_log.append({
            "action": "delete", "tool_name": tool_name,
            "timestamp": now_utc().isoformat(),
        })
        logger.info("Dynamic tool deleted: %s", tool_name)
        return True

    def list_dynamic(self) -> list[dict[str, Any]]:
        """List all currently registered dynamic tools."""
        return [
            {
                "tool_name": name,
                "display_name": tool.definition.display_name,
                "description": tool.definition.description,
                "risk_level": tool.definition.risk_level.value,
                "created_at": tool.created_at,
                "ttl_seconds": tool.spec.ttl_seconds,
                "category": tool.definition.category,
            }
            for name, tool in self._dynamic_tools.items()
        ]

    def get_audit_log(self) -> list[dict[str, Any]]:
        return self._audit_log[-50:]  # Last 50 entries

    def _expire_tool(self, tool_name: str) -> None:
        """Auto-unregister a tool when its TTL expires."""
        if tool_name in self._dynamic_tools:
            self.delete(tool_name)
            logger.info("Dynamic tool expired: %s", tool_name)

    def cleanup_all(self) -> None:
        """Remove all dynamic tools (e.g., on shutdown)."""
        for name in list(self._dynamic_tools.keys()):
            self.delete(name)


# ── Built-in meta-tools for agent self-management ────────────

class ToolCreateMetaTool(Tool):
    """Meta-tool: allows agent to create new tools dynamically."""

    def __init__(self, tool_factory: ToolFactory) -> None:
        self.factory = tool_factory
        self.definition = ToolDefinition(
            tool_name="tool.create",
            display_name="创建工具",
            description=(
                "动态创建新工具。参数: tool_name, description, category, "
                "code(Python代码,需定义run(input_data)函数), "
                "input_schema(JSON Schema对象), ttl_seconds(存活时间,默认3600)"
            ),
            category="meta",
            risk_level=RiskLevel.HIGH,
            permissions=["tool:manage"],
            requires_approval_by_default=True,
            timeout_seconds=15,
        )

    async def execute(self, intent: ToolIntent):
        try:
            spec = DynamicToolSpec(
                tool_name=str(intent.input_payload.get("tool_name", "")),
                display_name=str(intent.input_payload.get("display_name", "")),
                description=str(intent.input_payload.get("description", "")),
                category=str(intent.input_payload.get("category", "dynamic")),
                risk_level=str(intent.input_payload.get("risk_level", "MEDIUM")),
                code=str(intent.input_payload.get("code", "")),
                input_schema=intent.input_payload.get("input_schema", {}),
                ttl_seconds=int(intent.input_payload.get("ttl_seconds", DEFAULT_TTL_SECONDS)),
                requires_approval=bool(intent.input_payload.get("requires_approval", True)),
            )

            tool, error = self.factory.create(spec)
            if tool:
                return observation_from_output(intent, "COMPLETED",
                    f"Tool '{spec.tool_name}' created (TTL: {spec.ttl_seconds}s)",
                    {"tool_name": spec.tool_name, "ttl": spec.ttl_seconds})
            else:
                return observation_from_output(intent, "FAILED",
                    f"Failed to create tool: {error}", error=error)
        except Exception as exc:
            return observation_from_output(intent, "FAILED", str(exc))


class ToolDeleteMetaTool(Tool):
    """Meta-tool: allows agent to delete dynamic tools."""

    def __init__(self, tool_factory: ToolFactory) -> None:
        self.factory = tool_factory
        self.definition = ToolDefinition(
            tool_name="tool.delete",
            display_name="删除工具",
            description="删除一个动态创建的工具。参数: tool_name",
            category="meta",
            risk_level=RiskLevel.MEDIUM,
            permissions=["tool:manage"],
            requires_approval_by_default=True,
            timeout_seconds=10,
        )

    async def execute(self, intent: ToolIntent):
        tool_name = str(intent.input_payload.get("tool_name", ""))
        if not tool_name:
            return observation_from_output(intent, "FAILED", "Missing tool_name")
        ok = self.factory.delete(tool_name)
        if ok:
            return observation_from_output(intent, "COMPLETED",
                f"Tool '{tool_name}' deleted")
        else:
            return observation_from_output(intent, "FAILED",
                f"Tool '{tool_name}' not found or not dynamic")


class ToolListDynamicMetaTool(Tool):
    """Meta-tool: list all dynamically created tools."""

    def __init__(self, tool_factory: ToolFactory) -> None:
        self.factory = tool_factory
        self.definition = ToolDefinition(
            tool_name="tool.list_dynamic",
            display_name="列出动态工具",
            description="列出所有当前已注册的动态创建的工具。",
            category="meta",
            risk_level=RiskLevel.LOW,
            permissions=["tool:manage"],
            timeout_seconds=10,
        )

    async def execute(self, intent: ToolIntent):
        tools = self.factory.list_dynamic()
        return observation_from_output(intent, "COMPLETED",
            f"{len(tools)} dynamic tool(s) registered",
            {"dynamic_tools": tools, "count": len(tools)})
