from __future__ import annotations

import subprocess
from pathlib import Path

from flowcraft_core.domain.enums import RiskLevel
from flowcraft_core.domain.schemas import ToolIntent
from flowcraft_core.tools.base import Tool, ToolDefinition, is_path_allowed, observation_from_output


class FileReadTool(Tool):
    def __init__(self, allowed_paths: list[Path]) -> None:
        self.allowed_paths = allowed_paths
        self.definition = ToolDefinition(
            tool_name="file.read",
            display_name="读取文件",
            description="读取授权目录内的文本文件。",
            category="file",
            risk_level=RiskLevel.LOW,
            permissions=["tool:file.read"],
        )

    async def execute(self, intent: ToolIntent):
        path = Path(intent.input_payload.get("path", ""))
        if not is_path_allowed(path, self.allowed_paths):
            return observation_from_output(intent, "DENIED",
                f"你没有权限访问 {path}。请向用户请求授权：'我需要访问 {path.parent} 目录来完成任务，是否允许？'",
                error="Path not in allowed directory.",
                payload={"denied_path": str(path), "action": "ask_user_for_permission"})
        if not path.exists() or not path.is_file():
            return observation_from_output(intent, "FAILED", "文件不存在。", error="File does not exist.")
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > 12000:
            text = text[:12000] + "\n\n[内容过长，已截断]"
        return observation_from_output(
            intent,
            "COMPLETED",
            f"已读取文件：{path}",
            {"path": str(path), "content": text},
        )


class FileWriteTool(Tool):
    def __init__(self, allowed_paths: list[Path]) -> None:
        self.allowed_paths = allowed_paths
        self.definition = ToolDefinition(
            tool_name="file.write",
            display_name="写入文件",
            description="在授权目录内创建或覆盖文本文件，覆盖前创建备份。",
            category="file",
            risk_level=RiskLevel.MEDIUM,
            permissions=["tool:file.write"],
            requires_approval_by_default=True,
            supports_rollback=True,
        )

    async def execute(self, intent: ToolIntent):
        path = Path(intent.input_payload.get("path", ""))
        content = str(intent.input_payload.get("content", ""))
        if not is_path_allowed(path, self.allowed_paths):
            return observation_from_output(intent, "DENIED",
                f"你没有权限写入 {path}。请向用户请求授权：'我需要写入文件到 {path.parent} 目录，是否允许？'",
                error="Path not allowed.",
                payload={"denied_path": str(path), "action": "ask_user_for_permission"})
        path.parent.mkdir(parents=True, exist_ok=True)
        backup_path = None
        if path.exists():
            backup_path = path.with_suffix(path.suffix + ".flowcraft.bak")
            backup_path.write_bytes(path.read_bytes())
        path.write_text(content, encoding="utf-8")
        return observation_from_output(
            intent,
            "COMPLETED",
            f"已写入文件：{path}",
            {"path": str(path), "backup_path": str(backup_path) if backup_path else None},
        )


class CommandRunTool(Tool):
    def __init__(self, allowed_paths: list[Path]) -> None:
        self.allowed_paths = allowed_paths
        self.definition = ToolDefinition(
            tool_name="command.run",
            display_name="执行命令",
            description="在授权工作目录执行用户批准的命令。",
            category="system",
            risk_level=RiskLevel.HIGH,
            permissions=["tool:command.run"],
            requires_approval_by_default=True,
            timeout_seconds=30,
        )

    async def execute(self, intent: ToolIntent):
        command = str(intent.input_payload.get("command", ""))
        cwd = Path(intent.input_payload.get("cwd", self.allowed_paths[0]))
        if not is_path_allowed(cwd, self.allowed_paths):
            return observation_from_output(intent, "DENIED",
                "命令目录不在授权范围内。请向用户请求授权：'我需要在XX目录执行命令，是否允许？'",
                error="Cwd not allowed.",
                payload={"action": "ask_user_for_permission"})
        blocked = ["format", "del /s", "rm -rf", "reg delete", "shutdown"]
        if any(token in command.lower() for token in blocked):
            return observation_from_output(intent, "DENIED", "命令被安全策略拦截。", error="Command is blocked.")
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            shell=True,
            text=True,
            capture_output=True,
            timeout=30,
        )
        return observation_from_output(
            intent,
            "COMPLETED" if completed.returncode == 0 else "FAILED",
            "命令执行完成。" if completed.returncode == 0 else "命令执行失败。",
            {
                "returncode": completed.returncode,
                "stdout": completed.stdout[-8000:],
                "stderr": completed.stderr[-8000:],
            },
            error=None if completed.returncode == 0 else completed.stderr[-2000:],
        )

