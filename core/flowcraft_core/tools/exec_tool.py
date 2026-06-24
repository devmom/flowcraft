"""Enhanced Exec Tool — local shell execution with safety profiles and approval gates.

Mirrors OpenClaw's exec tool with:
  - Safe bin profiles for risk-based auto-approval
  - Inline eval detection (blocks python -c, node -e, etc.)
  - Background process support for long-running commands
  - Working directory control
  - Timeout with configurable duration
  - Structured output (stdout, stderr, returncode, elapsed)

Security: All commands are vetted through ExecApprovalManager before execution.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time as _time
from pathlib import Path
from typing import Any

from flowcraft_core.approval.exec_approval import (
    ExecApprovalManager, ExecApprovalDecision,
)
from flowcraft_core.domain.enums import RiskLevel
from flowcraft_core.domain.schemas import ToolIntent
from flowcraft_core.tools.base import Tool, ToolDefinition, observation_from_output

logger = logging.getLogger(__name__)


class ExecTool(Tool):
    """Execute shell commands with safety profiles, timeouts, and approval gates.

    Use this tool when you need to:
      - Run Python scripts as files (not inline -c)
      - Install packages (pip install)
      - Use git (status, diff, add, commit)
      - Build projects (npm, cargo, make)
      - Run tests (pytest, npm test)
      - Inspect the system (ls, find, grep, cat, du, df)

    DO NOT use for:
      - Inline code: python -c "..." or node -e "..." (blocked — write a file first)
      - Destructive operations (rm -rf, format, etc.)
      - Anything you wouldn't type yourself
    """

    def __init__(
        self,
        allowed_paths: list[Path],
        approval_manager: ExecApprovalManager | None = None,
        security_mode: str = "allowlist",
        default_timeout: int = 120,
        workspace_dir: Path | None = None,
    ) -> None:
        self.allowed_paths = allowed_paths
        self.approval_manager = approval_manager or ExecApprovalManager(
            security_mode=security_mode,
        )
        self.default_timeout = default_timeout
        self.workspace_dir = workspace_dir or Path.cwd()

        self.definition = ToolDefinition(
            tool_name="exec",
            display_name="执行Shell命令",
            description=(
                "在本地工作目录执行 Shell 命令。支持 Python 脚本、git、pip、npm、文件操作等。"
                "安全策略: 安全二进制自动批准(LOW风险)，中高风险需要确认。"
                "禁止内联执行(python -c 等) — 请先用 file.write 写入脚本文件再执行。"
                "参数: command(命令字符串), cwd(工作目录, 可选), "
                "timeout_seconds(超时秒数, 默认120), background(是否后台运行, 默认false)"
            ),
            category="system",
            risk_level=RiskLevel.HIGH,
            permissions=["tool:exec"],
            requires_approval_by_default=True,
            timeout_seconds=max(default_timeout + 10, 130),
            examples=[
                {
                    "description": "Run a Python script",
                    "input": {"command": "python scripts/analyze.py", "cwd": "/workspace"},
                },
                {
                    "description": "Install a package",
                    "input": {"command": "pip install requests", "cwd": "/workspace"},
                },
                {
                    "description": "Check git status",
                    "input": {"command": "git status", "cwd": "/workspace/project"},
                },
                {
                    "description": "Run tests",
                    "input": {"command": "pytest tests/ -v", "cwd": "/workspace/project"},
                },
            ],
        )

    async def execute(self, intent: ToolIntent):
        """Execute a shell command with safety vetting.

        Input payload:
          - command: str (required)
          - cwd: str (optional, defaults to workspace)
          - timeout_seconds: int (optional, default 120)
          - background: bool (optional, default false)
          - env: dict (optional, extra environment variables)
        """
        command = str(intent.input_payload.get("command", "")).strip()
        if not command:
            return observation_from_output(
                intent, "FAILED", "Missing 'command' parameter",
                error="command is required")

        # Resolve working directory
        cwd_str = str(intent.input_payload.get("cwd", ""))
        if cwd_str:
            cwd = Path(cwd_str)
        else:
            cwd = self.workspace_dir

        timeout = min(
            int(intent.input_payload.get("timeout_seconds", self.default_timeout)),
            600,  # Absolute max 10 minutes
        )
        background = bool(intent.input_payload.get("background", False))
        extra_env = intent.input_payload.get("env", {}) or {}

        # ── Vetting ────────────────────────────────────────
        decision = self.approval_manager.vet_command(command, str(cwd))
        if not decision.allowed:
            return observation_from_output(
                intent, "DENIED",
                f"命令被安全策略阻止: {decision.reason}",
                error=decision.reason,
                payload={
                    "command": command,
                    "risk": decision.risk_level,
                    "suggested_alternative": decision.suggested_alternative,
                },
            )

        # ── Approval Gate ──────────────────────────────────
        if decision.requires_user_approval:
            preview = self.approval_manager.get_command_preview(command, str(cwd))
            return observation_from_output(
                intent, "WAITING_APPROVAL",
                f"需要用户确认执行此命令 (风险: {decision.risk_level})",
                payload={
                    "command": command,
                    "cwd": str(cwd),
                    "risk": decision.risk_level,
                    "profile": decision.matched_profile,
                    "preview": preview,
                },
            )

        # ── Execute ────────────────────────────────────────
        t0 = _time.monotonic()

        try:
            # Build environment
            env = {**os.environ, **extra_env}

            if background:
                # Background: spawn and return immediately
                proc = subprocess.Popen(
                    command,
                    cwd=str(cwd),
                    shell=True,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                return observation_from_output(
                    intent, "COMPLETED",
                    f"命令已在后台启动 (PID: {proc.pid})",
                    payload={
                        "command": command,
                        "cwd": str(cwd),
                        "pid": proc.pid,
                        "background": True,
                        "risk": decision.risk_level,
                        "profile": decision.matched_profile,
                    },
                )

            # Foreground: execute with timeout
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_shell(
                    command,
                    cwd=str(cwd),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=30.0,  # spawn timeout
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )

            elapsed = _time.monotonic() - t0
            stdout_text = stdout.decode("utf-8", errors="replace")
            stderr_text = stderr.decode("utf-8", errors="replace")

            # Truncate large outputs
            max_output = 50000
            if len(stdout_text) > max_output:
                stdout_text = stdout_text[:max_output] + (
                    f"\n\n[... output truncated at {max_output} chars, "
                    f"total: {len(stdout_text)} chars]"
                )
            if len(stderr_text) > max_output:
                stderr_text = stderr_text[:max_output] + (
                    f"\n\n[... stderr truncated at {max_output} chars]"
                )

            success = proc.returncode == 0
            status = "COMPLETED" if success else "FAILED"

            summary = (
                f"命令{'成功' if success else '失败'} "
                f"(exit={proc.returncode}, {elapsed:.2f}s)"
            )

            return observation_from_output(
                intent, status, summary,
                payload={
                    "command": command,
                    "cwd": str(cwd),
                    "returncode": proc.returncode,
                    "stdout": stdout_text,
                    "stderr": stderr_text,
                    "elapsed_seconds": round(elapsed, 3),
                    "risk": decision.risk_level,
                    "profile": decision.matched_profile,
                },
                error=None if success else stderr_text[:2000],
            )

        except asyncio.TimeoutError:
            elapsed = _time.monotonic() - t0
            return observation_from_output(
                intent, "FAILED",
                f"命令超时 ({timeout}s)",
                error=f"Command timed out after {timeout}s",
                payload={
                    "command": command,
                    "cwd": str(cwd),
                    "elapsed_seconds": round(elapsed, 3),
                    "timeout": timeout,
                },
            )
        except Exception as exc:
            elapsed = _time.monotonic() - t0
            return observation_from_output(
                intent, "FAILED",
                f"命令执行异常: {exc}",
                error=str(exc),
                payload={
                    "command": command,
                    "cwd": str(cwd),
                    "elapsed_seconds": round(elapsed, 3),
                },
            )
