"""Policy Engine — 9 个策略检查点：Plan, Tool, File, Network, Memory, Output, Plugin, Workflow, Input.

Design: Doc/11-Policy-Engine.md
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flowcraft_core.domain.enums import PolicyDecisionValue, RiskLevel
from flowcraft_core.domain.schemas import ExecutionPlan, PolicyDecision, ToolIntent


# ── Dangerous command patterns ──────────────────────────────
DANGEROUS_COMMAND_PATTERNS: list[str] = [
    "format", "del /s", "del /f /s", "rm -rf", "rm -rf /",
    "reg delete", "reg add", "shutdown", "shutdown /s",
    "diskpart", "bcdedit", "netsh", "icacls",
    "takeown", "cacls", "wmic", "sc delete",
    "powershell -enc", "powershell -EncodedCommand",
    "rundll32", "regsvr32", "mshta",
    "cmd /c del", "cmd /c rd", "cmd /c format",
    "chkdsk /f", "fsutil", "cipher /w",
    "> /dev/sda", "dd if=", "mkfs.",
    ":(){ :|:& };:",  # fork bomb
]


class PolicyEngine:
    """统一策略引擎，覆盖全部 9 个检查点。

    策略来源（优先级从高到低）：
    1. 系统默认策略（内置硬编码）
    2. 企业策略（EnterprisePolicyEngine）
    3. 用户设置策略
    4. 工具定义策略（ToolDefinition.requires_approval_by_default）
    5. 工作流声明策略
    6. 插件声明策略
    """

    def __init__(self) -> None:
        self.trusted_sessions: set[str] = set()
        # 安全配置
        self.blocked_paths: set[str] = set()       # 绝对禁止的路径模式
        self.network_allowed: bool = True           # 全局网络开关
        self.denied_tools: set[str] = set()         # 全局禁用的工具
        self.max_memory_per_session: int = 200      # 单会话记忆上限

    def trust_session(self, session_id: str) -> None:
        self.trusted_sessions.add(session_id)

    # ── 1. Input Policy ─────────────────────────────────────

    def check_input(self, task_id: str, raw_input: str,
                    session_id: str = "") -> PolicyDecision:
        """检查用户输入是否包含被禁止的内容。"""
        if not raw_input or not raw_input.strip():
            return PolicyDecision(
                task_id=task_id, target_type="input", target_id=task_id,
                decision=PolicyDecisionValue.DENY,
                reason="输入为空，请提供有效的任务描述。",
                matched_rules=["reject_empty_input"],
                risk_level=RiskLevel.LOW,
            )
        if len(raw_input) > 50000:
            return PolicyDecision(
                task_id=task_id, target_type="input", target_id=task_id,
                decision=PolicyDecisionValue.DENY,
                reason="输入过长（超过50000字符），请简化描述。",
                matched_rules=["reject_oversized_input"],
                risk_level=RiskLevel.LOW,
            )
        return PolicyDecision(
            task_id=task_id, target_type="input", target_id=task_id,
            decision=PolicyDecisionValue.ALLOW,
            reason="输入合法。",
            matched_rules=["default_allow_valid_input"],
            risk_level=RiskLevel.LOW,
        )

    # ── 2. Plan Policy ──────────────────────────────────────

    def check_plan(self, task_id: str, plan: ExecutionPlan) -> PolicyDecision:
        high_risk_steps = [
            step.title for step in plan.steps
            if step.risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL}
        ]
        if high_risk_steps:
            return PolicyDecision(
                task_id=task_id, target_type="plan", target_id=plan.plan_id,
                decision=PolicyDecisionValue.REQUIRE_APPROVAL,
                reason="计划包含高风险步骤，需要用户确认。",
                matched_rules=["high_risk_step_requires_approval"],
                risk_level=RiskLevel.HIGH,
            )
        # Block plans that use globally denied tools
        for step in plan.steps:
            for tool_name in step.required_tools:
                if tool_name in self.denied_tools:
                    return PolicyDecision(
                        task_id=task_id, target_type="plan", target_id=plan.plan_id,
                        decision=PolicyDecisionValue.DENY,
                        reason=f"计划使用了被禁用的工具: {tool_name}",
                        matched_rules=["denied_tool_in_plan"],
                        risk_level=RiskLevel.HIGH,
                    )
        return PolicyDecision(
            task_id=task_id, target_type="plan", target_id=plan.plan_id,
            decision=PolicyDecisionValue.ALLOW,
            reason="计划未发现需要阻止的风险。",
            matched_rules=["default_allow_low_risk_plan"],
            risk_level=RiskLevel.LOW,
        )

    # ── 3. Tool Policy ──────────────────────────────────────

    def check_tool_intent(self, intent: ToolIntent,
                          session_id: str = "") -> PolicyDecision:
        if session_id and session_id in self.trusted_sessions:
            return PolicyDecision(
                task_id=intent.task_id, step_id=intent.step_id,
                target_type="tool_intent", target_id=intent.tool_intent_id,
                decision=PolicyDecisionValue.ALLOW,
                reason="Session trusted, auto-approved.",
                matched_rules=["trusted_session_auto_approve"],
                risk_level=intent.risk_level,
            )
        # Globally denied tool
        if intent.tool_name in self.denied_tools:
            return PolicyDecision(
                task_id=intent.task_id, step_id=intent.step_id,
                target_type="tool_intent", target_id=intent.tool_intent_id,
                decision=PolicyDecisionValue.DENY,
                reason=f"Tool '{intent.tool_name}' is globally disabled.",
                matched_rules=["denied_tool"],
                risk_level=intent.risk_level,
            )
        # Dangerous command detection
        if intent.tool_name == "command.run":
            cmd = str(intent.input_payload.get("command", "")).lower()
            for pattern in DANGEROUS_COMMAND_PATTERNS:
                if pattern.lower() in cmd:
                    return PolicyDecision(
                        task_id=intent.task_id, step_id=intent.step_id,
                        target_type="tool_intent", target_id=intent.tool_intent_id,
                        decision=PolicyDecisionValue.DENY,
                        reason=f"命令被安全策略拦截（匹配危险模式: {pattern}）。",
                        matched_rules=["dangerous_command_blocked"],
                        risk_level=RiskLevel.CRITICAL,
                    )
        if intent.risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
            return PolicyDecision(
                task_id=intent.task_id, step_id=intent.step_id,
                target_type="tool_intent", target_id=intent.tool_intent_id,
                decision=PolicyDecisionValue.REQUIRE_APPROVAL,
                reason="High-risk tool requires user confirmation.",
                matched_rules=["high_risk_tool_requires_approval"],
                risk_level=intent.risk_level,
            )
        return PolicyDecision(
            task_id=intent.task_id, step_id=intent.step_id,
            target_type="tool_intent", target_id=intent.tool_intent_id,
            decision=PolicyDecisionValue.ALLOW,
            reason="Tool risk acceptable.",
            matched_rules=["default_allow_low_risk_tool"],
            risk_level=intent.risk_level,
        )

    # ── 4. File Policy (NEW) ─────────────────────────────────

    def check_file_access(self, task_id: str, path: Path,
                          operation: str,  # "read", "write", "delete"
                          allowed_paths: list[Path] | None = None,
                          session_id: str = "") -> PolicyDecision:
        """检查文件访问是否被允许。

        Args:
            operation: "read" | "write" | "delete"
        """
        resolved = path.resolve()
        # Check blocked path patterns
        path_str = str(resolved).lower()
        for blocked in self.blocked_paths:
            if blocked.lower() in path_str:
                return PolicyDecision(
                    task_id=task_id, target_type="file", target_id=str(path),
                    decision=PolicyDecisionValue.DENY,
                    reason=f"路径 {path} 在禁止访问列表中。",
                    matched_rules=["blocked_path"],
                    risk_level=RiskLevel.HIGH,
                )
        # Check allowed paths
        if allowed_paths:
            try:
                resolved.relative_to(Path(p).resolve()
                                     for p in allowed_paths if any(
                                         True for _ in [None]))  # simplified check
                in_allowed = any(
                    _is_subpath(resolved, ap.resolve()) for ap in allowed_paths)
                if not in_allowed:
                    return PolicyDecision(
                        task_id=task_id, target_type="file", target_id=str(path),
                        decision=PolicyDecisionValue.DENY,
                        reason=f"路径 {path} 不在授权工作目录内。",
                        matched_rules=["path_not_allowed"],
                        risk_level=RiskLevel.MEDIUM,
                    )
            except Exception:
                pass
        # Delete always requires approval
        if operation == "delete":
            return PolicyDecision(
                task_id=task_id, target_type="file", target_id=str(path),
                decision=PolicyDecisionValue.REQUIRE_APPROVAL,
                reason="删除文件需要用户确认。",
                matched_rules=["file_delete_requires_approval"],
                risk_level=RiskLevel.HIGH,
            )
        # Write to existing file requires approval
        if operation == "write" and path.exists():
            return PolicyDecision(
                task_id=task_id, target_type="file", target_id=str(path),
                decision=PolicyDecisionValue.REQUIRE_APPROVAL,
                reason="覆盖已有文件需要用户确认。",
                matched_rules=["file_overwrite_requires_approval"],
                risk_level=RiskLevel.MEDIUM,
            )
        return PolicyDecision(
            task_id=task_id, target_type="file", target_id=str(path),
            decision=PolicyDecisionValue.ALLOW,
            reason="文件访问允许。",
            matched_rules=["default_allow_safe_file_access"],
            risk_level=RiskLevel.LOW,
        )

    # ── 5. Network Policy (NEW) ──────────────────────────────

    def check_network_access(self, task_id: str, url: str,
                             session_id: str = "") -> PolicyDecision:
        """检查网络访问是否被允许。"""
        if not self.network_allowed:
            return PolicyDecision(
                task_id=task_id, target_type="network", target_id=url,
                decision=PolicyDecisionValue.DENY,
                reason="网络访问已被用户全局关闭。",
                matched_rules=["network_globally_disabled"],
                risk_level=RiskLevel.LOW,
            )
        # Check localhost / internal IP access
        lower_url = url.lower()
        blocked_hosts = ["localhost", "127.0.0.1", "0.0.0.0",
                         "169.254.", "10.", "172.16.", "192.168."]
        for host in blocked_hosts:
            if host in lower_url:
                return PolicyDecision(
                    task_id=task_id, target_type="network", target_id=url,
                    decision=PolicyDecisionValue.REQUIRE_APPROVAL,
                    reason=f"访问内部地址 {url} 需要用户确认。",
                    matched_rules=["internal_network_requires_approval"],
                    risk_level=RiskLevel.MEDIUM,
                )
        return PolicyDecision(
            task_id=task_id, target_type="network", target_id=url,
            decision=PolicyDecisionValue.ALLOW,
            reason="网络访问允许。",
            matched_rules=["default_allow_safe_network"],
            risk_level=RiskLevel.LOW,
        )

    # ── 6. Memory Policy (NEW) ───────────────────────────────

    def check_memory_write(self, task_id: str, content: str,
                           sensitivity: str = "normal") -> PolicyDecision:
        """检查记忆写入是否安全。"""
        # Block storing API keys / secrets in memory
        sensitive_patterns = [
            "sk-", "api_key", "api key", "Bearer ", "Authorization:",
            "password", "密码", "secret", "token",
        ]
        lower = content.lower()
        detected = [p for p in sensitive_patterns if p.lower() in lower]
        if detected and sensitivity != "confidential":
            return PolicyDecision(
                task_id=task_id, target_type="memory", target_id=task_id,
                decision=PolicyDecisionValue.DENY,
                reason=f"检测到疑似敏感信息（{', '.join(detected[:3])}），已阻止写入记忆。",
                matched_rules=["sensitive_content_in_memory"],
                risk_level=RiskLevel.HIGH,
            )
        return PolicyDecision(
            task_id=task_id, target_type="memory", target_id=task_id,
            decision=PolicyDecisionValue.ALLOW,
            reason="记忆写入允许。",
            matched_rules=["default_allow_safe_memory"],
            risk_level=RiskLevel.LOW,
        )

    # ── 7. Output Policy (NEW) ───────────────────────────────

    def check_final_answer(self, task_id: str, answer: str) -> PolicyDecision:
        """检查最终输出是否包含敏感信息。"""
        # Check for API key leakage in output
        api_key_patterns = [
            r'sk-[a-zA-Z0-9]{20,}',           # OpenAI key
            r'AIza[0-9A-Za-z\-_]{35}',        # Google API key
            r'Bearer [A-Za-z0-9\-._~+/]+=*',  # Bearer token
        ]
        import re
        for pattern in api_key_patterns:
            if re.search(pattern, answer):
                return PolicyDecision(
                    task_id=task_id, target_type="output", target_id=task_id,
                    decision=PolicyDecisionValue.DENY,
                    reason="最终输出中含疑似 API Key，已拦截。",
                    matched_rules=["api_key_in_output"],
                    risk_level=RiskLevel.CRITICAL,
                )
        return PolicyDecision(
            task_id=task_id, target_type="output", target_id=task_id,
            decision=PolicyDecisionValue.ALLOW,
            reason="输出安全检查通过。",
            matched_rules=["default_allow_clean_output"],
            risk_level=RiskLevel.LOW,
        )

    # ── 8. Plugin Policy (NEW) ───────────────────────────────

    def check_plugin_install(self, task_id: str, plugin_name: str,
                             permissions: list[str] | None = None,
                             source: str = "unknown") -> PolicyDecision:
        """检查插件安装是否安全。"""
        perms = permissions or []
        high_risk_perms = [
            "tool:command.run", "tool:file.delete",
            "tool:network.request", "memory:long_term.write",
        ]
        risky = [p for p in perms if p in high_risk_perms]
        if risky:
            return PolicyDecision(
                task_id=task_id, target_type="plugin", target_id=plugin_name,
                decision=PolicyDecisionValue.REQUIRE_APPROVAL,
                reason=f"插件 '{plugin_name}' 请求高风险权限: {', '.join(risky)}",
                matched_rules=["plugin_high_risk_permissions"],
                risk_level=RiskLevel.HIGH,
            )
        if source not in ("marketplace", "trusted"):
            return PolicyDecision(
                task_id=task_id, target_type="plugin", target_id=plugin_name,
                decision=PolicyDecisionValue.REQUIRE_APPROVAL,
                reason=f"插件 '{plugin_name}' 来源未知，需要用户确认安装。",
                matched_rules=["plugin_unknown_source"],
                risk_level=RiskLevel.MEDIUM,
            )
        return PolicyDecision(
            task_id=task_id, target_type="plugin", target_id=plugin_name,
            decision=PolicyDecisionValue.ALLOW,
            reason="插件安全检查通过。",
            matched_rules=["default_allow_safe_plugin"],
            risk_level=RiskLevel.LOW,
        )

    # ── 9. Workflow Policy (NEW) ─────────────────────────────

    def check_workflow_run(self, task_id: str, workflow_id: str,
                           required_permissions: list[str] | None = None,
                           risk_summary: str = "LOW") -> PolicyDecision:
        """检查工作流运行是否安全。"""
        if risk_summary in ("HIGH", "CRITICAL"):
            return PolicyDecision(
                task_id=task_id, target_type="workflow", target_id=workflow_id,
                decision=PolicyDecisionValue.REQUIRE_APPROVAL,
                reason=f"工作流风险等级为 {risk_summary}，需要用户确认。",
                matched_rules=["workflow_high_risk"],
                risk_level=RiskLevel.HIGH,
            )
        return PolicyDecision(
            task_id=task_id, target_type="workflow", target_id=workflow_id,
            decision=PolicyDecisionValue.ALLOW,
            reason="工作流安全检查通过。",
            matched_rules=["default_allow_safe_workflow"],
            risk_level=RiskLevel.LOW,
        )


def _is_subpath(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False

