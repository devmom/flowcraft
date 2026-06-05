"""Tool Registry + Tool Harness — 完整执行流程含 Schema 校验 + Dry Run 预览.

Design: Doc/10-Tool-Harness.md
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from flowcraft_core.domain.enums import PolicyDecisionValue, RiskLevel
from flowcraft_core.domain.schemas import ToolIntent, ToolObservation
from flowcraft_core.policy.engine import PolicyEngine
from flowcraft_core.tools.base import Tool, observation_from_output

logger = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.definition.tool_name] = tool

    def unregister(self, name: str) -> bool:
        if name in self._tools:
            del self._tools[name]
            return True
        return False

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_definitions(self) -> list[dict]:
        return [tool.definition.model_dump(mode="json") for tool in self._tools.values()]


class ToolHarness:
    """工具执行网关 — 完整流程含 Schema 校验 + 策略 + Dry Run + 审计.

    执行流程:
        1. 检查工具是否存在
        2. 校验输入 schema
        3. 策略检查（含危险命令检测）
        4. Dry run 影响预览（如支持）
        5. 必要时创建审批请求
        6. 执行工具
        7. 规范化输出
        8. 记录审计日志
    """

    # 每个工具的必需输入字段
    TOOL_REQUIRED_INPUTS: dict[str, list[str]] = {
        "file.read": ["path"],
        "file.write": ["path", "content"],
        "file.delete": ["path"],
        "file.list": ["path"],
        "file.search": ["pattern"],
        "command.run": ["command"],
        "browser.read": ["url"],
        "browser.navigate": ["url"],
        "document.pdf.read": ["path"],
        "document.docx.read": ["path"],
        "document.xlsx.read": ["path"],
        "web.search": ["query"],
        "http.request": ["url"],
    }

    def __init__(self, registry: ToolRegistry, policy_engine: PolicyEngine) -> None:
        self.registry = registry
        self.policy_engine = policy_engine

    def validate_input(self, tool_name: str, payload: dict[str, Any]) -> list[str]:
        """校验工具输入 schema，返回错误列表."""
        errors: list[str] = []
        required = self.TOOL_REQUIRED_INPUTS.get(tool_name, [])
        for field in required:
            if field not in payload or not payload[field]:
                errors.append(f"缺少必需参数: {field}")
        # Path safety check for file/command tools
        if "path" in payload and tool_name.startswith(("file.", "document.", "command.")):
            path_str = str(payload.get("path", ""))
            if ".." in path_str:
                errors.append(f"路径包含非法字符 (..): {path_str}")
        return errors

    def generate_dry_run_preview(self, tool_name: str,
                                  payload: dict[str, Any]) -> dict[str, Any]:
        """生成工具执行的影响预览（Dry Run）."""
        preview: dict[str, Any] = {"tool_name": tool_name, "effects": []}
        if tool_name == "file.read":
            preview["effects"].append(
                {"type": "read", "target": payload.get("path", "?"),
                 "description": "将读取此文件的内容"})
        elif tool_name == "file.write":
            path = str(payload.get("path", "?"))
            preview["effects"].append(
                {"type": "write", "target": path,
                 "description": f"将在 {path} 写入内容"})
            if Path(path).exists():
                preview["effects"].append(
                    {"type": "overwrite", "target": path,
                     "description": "⚠ 文件已存在，将创建备份后再覆盖"})
        elif tool_name == "file.delete":
            preview["effects"].append(
                {"type": "delete", "target": payload.get("path", "?"),
                 "description": "⚠ 将永久删除此文件（需审批）"})
        elif tool_name == "command.run":
            preview["effects"].append(
                {"type": "execute", "target": payload.get("command", "?"),
                 "description": "将在授权工作目录执行此命令"})
        elif tool_name.startswith("browser."):
            preview["effects"].append(
                {"type": "web", "target": payload.get("url", "?"),
                 "description": "将访问此网页"})
        elif tool_name == "web.search":
            preview["effects"].append(
                {"type": "search", "target": payload.get("query", "?"),
                 "description": "将搜索此关键词"})
        else:
            preview["effects"].append(
                {"type": "unknown", "description": f"将执行工具 {tool_name}"})
        preview["risk_summary"] = "请检查以上操作是否符合预期"
        return preview

    async def invoke(self, intent: ToolIntent,
                     approval_granted: bool = False,
                     session_id: str = "",
                     dry_run: bool = False) -> ToolObservation:
        """执行工具调用（完整流程）.

        Args:
            intent: 工具调用意图
            approval_granted: 是否已获用户审批
            session_id: 会话 ID
            dry_run: 是否仅生成影响预览（不实际执行）
        """
        # 1. 检查工具存在
        tool = self.registry.get(intent.tool_name)
        if tool is None:
            return observation_from_output(
                intent, "FAILED",
                f"工具不存在：{intent.tool_name}", error="Unknown tool.")

        # 2. 校验输入 schema
        input_errors = self.validate_input(intent.tool_name,
                                           intent.input_payload)
        if input_errors:
            return observation_from_output(
                intent, "FAILED",
                f"输入参数校验失败: {'; '.join(input_errors)}",
                error="Input validation failed.",
                payload={"validation_errors": input_errors})

        # 3. 策略检查
        decision = self.policy_engine.check_tool_intent(intent, session_id)
        if decision.decision == PolicyDecisionValue.DENY:
            return observation_from_output(
                intent, "DENIED", decision.reason, error=decision.reason)
        if decision.decision == PolicyDecisionValue.REQUIRE_APPROVAL and not approval_granted:
            return observation_from_output(
                intent, "WAITING_APPROVAL", decision.reason)

        # 4. Dry run 预览
        if dry_run:
            preview = self.generate_dry_run_preview(
                intent.tool_name, intent.input_payload)
            return observation_from_output(
                intent, "DRY_RUN",
                "影响预览（未实际执行）",
                payload=preview)

        # 5. 执行工具
        result = await tool.execute(intent)

        # 6. 审计日志
        logger.info("Tool executed: %s by task=%s status=%s",
                    intent.tool_name, intent.task_id[:12], result.status)

        return result

