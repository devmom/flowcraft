"""Exec Approval Manager — risk-based command vetting.

Mirrors OpenClaw's exec security model:
  - Safe bins: Known-safe commands with behavior profiles
  - Allowlist: Only explicitly allowed commands run
  - Deny list: Always-blocked commands
  - Risk tiers: LOW (auto-approve) / MEDIUM (ask once) / HIGH (require explicit approval)
  - Strict inline eval detection: blocks python -c, node -e, etc. unless explicitly allowed
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ExecApprovalDecision:
    """Result of command vetting."""
    allowed: bool
    risk_level: str  # "LOW" | "MEDIUM" | "HIGH" | "CRITICAL"
    reason: str = ""
    requires_user_approval: bool = False
    suggested_alternative: str | None = None
    matched_profile: str | None = None  # Which safe_bin profile matched


# ── Safe Binary Profiles ───────────────────────────────────

@dataclass
class SafeBinProfile:
    """Defines allowed behavior for a safe binary."""
    name: str
    risk: str = "LOW"  # default risk level
    read_only: bool = True  # Does this binary only read?
    mutates_files: bool = False
    mutates_system: bool = False
    allow_network: bool = False
    max_positional_args: int = 20
    allowed_subcommands: list[str] = field(default_factory=list)
    denied_subcommands: list[str] = field(default_factory=list)
    allowed_flags: list[str] = field(default_factory=list)  # glob patterns
    denied_flags: list[str] = field(default_factory=list)


# Default safe bin profiles (mirrors OpenClaw's approach)
SAFE_BIN_PROFILES: dict[str, SafeBinProfile] = {
    # ── Read-only inspection ──
    "ls": SafeBinProfile(name="ls", risk="LOW", read_only=True),
    "dir": SafeBinProfile(name="dir", risk="LOW", read_only=True),
    "cat": SafeBinProfile(name="cat", risk="LOW", read_only=True),
    "type": SafeBinProfile(name="type", risk="LOW", read_only=True),
    "head": SafeBinProfile(name="head", risk="LOW", read_only=True),
    "tail": SafeBinProfile(name="tail", risk="LOW", read_only=True),
    "wc": SafeBinProfile(name="wc", risk="LOW", read_only=True),
    "find": SafeBinProfile(name="find", risk="LOW", read_only=True, max_positional_args=30),
    "grep": SafeBinProfile(name="grep", risk="LOW", read_only=True, max_positional_args=10),
    "rg": SafeBinProfile(name="rg", risk="LOW", read_only=True, max_positional_args=10),
    "sort": SafeBinProfile(name="sort", risk="LOW", read_only=True),
    "uniq": SafeBinProfile(name="uniq", risk="LOW", read_only=True),
    "diff": SafeBinProfile(name="diff", risk="LOW", read_only=True),
    "du": SafeBinProfile(name="du", risk="LOW", read_only=True),
    "df": SafeBinProfile(name="df", risk="LOW", read_only=True),
    "stat": SafeBinProfile(name="stat", risk="LOW", read_only=True),
    "file": SafeBinProfile(name="file", risk="LOW", read_only=True),
    "which": SafeBinProfile(name="which", risk="LOW", read_only=True),
    "where": SafeBinProfile(name="where", risk="LOW", read_only=True),

    # ── Development tools (may mutate workspace files) ──
    "python": SafeBinProfile(
        name="python", risk="MEDIUM", read_only=False,
        mutates_files=True, max_positional_args=20,
        # NOTE: No allowed_subcommands — script paths are not subcommands.
        # We allow python script.py (file execution) by default.
        # Inline eval (-c) is blocked separately by strict_inline_eval.
    ),
    "python3": SafeBinProfile(
        name="python3", risk="MEDIUM", read_only=False,
        mutates_files=True, max_positional_args=20,
    ),
    "pip": SafeBinProfile(
        name="pip", risk="HIGH", read_only=False,
        mutates_system=True, allow_network=True,
        allowed_subcommands=["install", "list", "show", "freeze", "uninstall"],
        denied_subcommands=[],
    ),
    "pip3": SafeBinProfile(
        name="pip3", risk="HIGH", read_only=False,
        mutates_system=True, allow_network=True,
    ),
    "git": SafeBinProfile(
        name="git", risk="MEDIUM", read_only=False,
        mutates_files=True, allow_network=True,
        allowed_subcommands=["status", "diff", "log", "branch", "add", "commit", "push", "pull", "fetch", "checkout", "stash", "clone"],
        denied_subcommands=["push --force", "reset --hard"],
    ),
    "npm": SafeBinProfile(
        name="npm", risk="MEDIUM", read_only=False,
        mutates_files=True, allow_network=True,
        allowed_subcommands=["install", "test", "run", "build", "start", "list", "version"],
    ),
    "npx": SafeBinProfile(name="npx", risk="MEDIUM", allow_network=True, mutates_files=True),
    "node": SafeBinProfile(
        name="node", risk="MEDIUM", read_only=False,
        mutates_files=True, max_positional_args=10,
        denied_flags=["-e*"],  # Inline eval blocked
    ),
    "cargo": SafeBinProfile(name="cargo", risk="MEDIUM", mutates_files=True, allow_network=True),
    "go": SafeBinProfile(name="go", risk="MEDIUM", mutates_files=True, allow_network=True),
    "rustc": SafeBinProfile(name="rustc", risk="MEDIUM", mutates_files=True),
    "gcc": SafeBinProfile(name="gcc", risk="MEDIUM", mutates_files=True),
    "make": SafeBinProfile(name="make", risk="MEDIUM", mutates_files=True),
    "cmake": SafeBinProfile(name="cmake", risk="MEDIUM", mutates_files=True),

    # ── System utilities (cross-platform) ──
    "echo": SafeBinProfile(name="echo", risk="LOW", read_only=True),
    "pwd": SafeBinProfile(name="pwd", risk="LOW", read_only=True),
    "date": SafeBinProfile(name="date", risk="LOW", read_only=True),
    "env": SafeBinProfile(name="env", risk="LOW", read_only=True),
    "printenv": SafeBinProfile(name="printenv", risk="LOW", read_only=True),
    "mkdir": SafeBinProfile(name="mkdir", risk="LOW", mutates_files=True),
    "cp": SafeBinProfile(name="cp", risk="MEDIUM", mutates_files=True),
    "mv": SafeBinProfile(name="mv", risk="MEDIUM", mutates_files=True),
    "touch": SafeBinProfile(name="touch", risk="LOW", mutates_files=True),
    "rm": SafeBinProfile(
        name="rm", risk="HIGH", mutates_files=True,
        denied_flags=["-rf", "-r", "-f"],
    ),
    "chmod": SafeBinProfile(name="chmod", risk="MEDIUM", mutates_system=True),
    "curl": SafeBinProfile(name="curl", risk="MEDIUM", allow_network=True, mutates_files=True),
    "wget": SafeBinProfile(name="wget", risk="MEDIUM", allow_network=True, mutates_files=True),
    "tar": SafeBinProfile(name="tar", risk="MEDIUM", mutates_files=True),
    "zip": SafeBinProfile(name="zip", risk="LOW", mutates_files=True),
    "unzip": SafeBinProfile(name="unzip", risk="LOW", mutates_files=True),

    # ── Windows utilities ──
    "whoami": SafeBinProfile(name="whoami", risk="LOW", read_only=True),
    "hostname": SafeBinProfile(name="hostname", risk="LOW", read_only=True),
    "ver": SafeBinProfile(name="ver", risk="LOW", read_only=True),
    "systeminfo": SafeBinProfile(name="systeminfo", risk="LOW", read_only=True),
    "tasklist": SafeBinProfile(name="tasklist", risk="LOW", read_only=True),
    "netstat": SafeBinProfile(name="netstat", risk="LOW", read_only=True),
    "ipconfig": SafeBinProfile(name="ipconfig", risk="LOW", read_only=True),
    "ping": SafeBinProfile(name="ping", risk="LOW", read_only=True, allow_network=True),
    "nslookup": SafeBinProfile(name="nslookup", risk="LOW", read_only=True, allow_network=True),
    "tracert": SafeBinProfile(name="tracert", risk="LOW", read_only=True, allow_network=True),
    "set": SafeBinProfile(name="set", risk="LOW", read_only=True),
    "cd": SafeBinProfile(name="cd", risk="LOW", read_only=True),
    "copy": SafeBinProfile(name="copy", risk="MEDIUM", mutates_files=True),
    "xcopy": SafeBinProfile(name="xcopy", risk="MEDIUM", mutates_files=True),
    "robocopy": SafeBinProfile(name="robocopy", risk="MEDIUM", mutates_files=True),
    "move": SafeBinProfile(name="move", risk="MEDIUM", mutates_files=True),
    "del": SafeBinProfile(name="del", risk="HIGH", mutates_files=True),
    "ren": SafeBinProfile(name="ren", risk="LOW", mutates_files=True),
    "rename": SafeBinProfile(name="rename", risk="LOW", mutates_files=True),
    "icacls": SafeBinProfile(name="icacls", risk="HIGH", mutates_system=True),
    "takeown": SafeBinProfile(name="takeown", risk="HIGH", mutates_system=True),
    "mklink": SafeBinProfile(name="mklink", risk="HIGH", mutates_system=True),
    "schtasks": SafeBinProfile(name="schtasks", risk="MEDIUM", mutates_system=True),
    "sc": SafeBinProfile(name="sc", risk="HIGH", mutates_system=True),
    "net": SafeBinProfile(name="net", risk="MEDIUM", mutates_system=True),
}

# Always-blocked commands (even in allowlist mode with wildcards)
BLOCKED_COMMANDS: set[str] = {
    "rm -rf /", "rm -rf ~", "rm -rf .",
    "del /s /q", "del /f /s /q",
    "format", "fdisk", "diskpart",
    "shutdown", "reboot", "halt", "poweroff",
    "reg delete", "reg add",
    "chmod 777 /", "chown -R",
    "dd if=", "mkfs.",
    ":(){ :|:& };:",  # fork bomb
    "> /dev/sda", "> /dev/hda",
    "wget -O - http:// | sh", "curl http:// | sh",
    "eval", "exec",
}


class ExecApprovalManager:
    """Manages command vetting, risk assessment, and approval decisions."""

    def __init__(
        self,
        security_mode: str = "allowlist",  # "full" | "allowlist" | "deny"
        auto_approve_risk: str = "LOW",     # Risk level at or below which auto-approve
        strict_inline_eval: bool = True,    # Block python -c, node -e, etc.
        custom_safe_bins: dict[str, SafeBinProfile] | None = None,
        custom_blocked: set[str] | None = None,
    ) -> None:
        self.security_mode = security_mode
        self.auto_approve_risk = auto_approve_risk
        self.strict_inline_eval = strict_inline_eval

        # Merge custom profiles with defaults
        self.safe_bins: dict[str, SafeBinProfile] = dict(SAFE_BIN_PROFILES)
        if custom_safe_bins:
            self.safe_bins.update(custom_safe_bins)

        self.blocked = BLOCKED_COMMANDS | (custom_blocked or set())

    def vet_command(self, command: str, cwd: str | None = None) -> ExecApprovalDecision:
        """Analyze a shell command and return an approval decision.

        Steps:
        1. Parse tokens (once, reused)
        2. Check against blocklist
        3. Check inline eval patterns (python -c, node -e, etc.)
        4. Look up safe bin profile
        5. Determine risk level and approval requirement
        """
        cmd_lower = command.lower().strip()

        # 0. Parse tokens once (Windows-safe)
        tokens = self._split_command(command)
        first_token = tokens[0].lower() if tokens else ""

        # 1. Blocklist check (match whole-word at command start, not substring)
        for blocked in self.blocked:
            blocked_lower = blocked.lower()
            # Only block if the command's first token IS the blocked binary
            if first_token == blocked_lower:
                return ExecApprovalDecision(
                    allowed=False,
                    risk_level="CRITICAL",
                    reason=f"Command matches blocked binary: {blocked}",
                    requires_user_approval=True,
                )
            # Multi-word blocked patterns (e.g., "rm -rf /")
            if " " in blocked_lower and blocked_lower in cmd_lower:
                return ExecApprovalDecision(
                    allowed=False,
                    risk_level="CRITICAL",
                    reason=f"Command matches blocked pattern: {blocked}",
                    requires_user_approval=True,
                )

        # 2. Inline eval detection
        if self.strict_inline_eval:
            inline_patterns = [
                (r'\bpython3?\s+-c\s', "python -c"),
                (r'\bnode\s+-e\s', "node -e"),
                (r'\bruby\s+-e\s', "ruby -e"),
                (r'\bperl\s+-e\s', "perl -e"),
                (r'\bphp\s+-r\s', "php -r"),
                (r'\blua\s+-e\s', "lua -e"),
                (r'\bbash\s+-c\s', "bash -c"),
                (r'\bsh\s+-c\s', "sh -c"),
            ]
            for pattern, name in inline_patterns:
                if re.search(pattern, cmd_lower):
                    return ExecApprovalDecision(
                        allowed=False,
                        risk_level="HIGH",
                        reason=f"Inline eval detected ({name}). Use file.write + exec to run scripts instead.",
                        requires_user_approval=True,
                        suggested_alternative="Write the script to a file first, then execute the file.",
                    )

        # 3. Extract binary name from already-parsed tokens
        if not tokens:
            return ExecApprovalDecision(allowed=False, risk_level="LOW", reason="Empty command")

        binary = tokens[0]
        # Get just the binary name (strip path and extension)
        binary_name = Path(binary).stem if "/" in binary or "\\" in binary else binary
        # On Windows, also try with .exe stripped
        if binary_name.lower().endswith(".exe"):
            binary_name = binary_name[:-4]

        # 4. Look up profile
        profile = self.safe_bins.get(binary_name) or self.safe_bins.get(binary)

        if not profile:
            # In allowlist mode, unknown commands are denied
            if self.security_mode == "allowlist":
                return ExecApprovalDecision(
                    allowed=False,
                    risk_level="HIGH",
                    reason=f"Binary '{binary_name}' is not in the safe bin list.",
                    requires_user_approval=True,
                )
            # In full mode, unknown commands are MEDIUM risk with approval
            return ExecApprovalDecision(
                allowed=True,
                risk_level="MEDIUM",
                reason=f"Unknown binary '{binary_name}' (full mode)",
                requires_user_approval=True,
            )

        # In deny mode, all commands are blocked
        if self.security_mode == "deny":
            return ExecApprovalDecision(
                allowed=False,
                risk_level="HIGH",
                reason="Shell execution is disabled (security=deny)",
            )

        # 5. Check subcommand constraints
        if len(tokens) > 1:
            subcommand = tokens[1]
            if profile.allowed_subcommands and subcommand not in profile.allowed_subcommands:
                return ExecApprovalDecision(
                    allowed=False,
                    risk_level="MEDIUM",
                    reason=f"Subcommand '{subcommand}' not allowed for '{binary_name}'. Allowed: {profile.allowed_subcommands}",
                    requires_user_approval=True,
                )
            if profile.denied_subcommands and subcommand in profile.denied_subcommands:
                return ExecApprovalDecision(
                    allowed=False,
                    risk_level="HIGH",
                    reason=f"Subcommand '{subcommand}' is denied for '{binary_name}'.",
                    requires_user_approval=True,
                )

        # 6. Check argument count
        if len(tokens) - 1 > profile.max_positional_args:
            return ExecApprovalDecision(
                allowed=False,
                risk_level="MEDIUM",
                reason=f"Too many arguments ({len(tokens)-1} > max {profile.max_positional_args})",
                requires_user_approval=True,
            )

        # 7. Determine risk and approval
        risk_order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
        auto_risk_value = risk_order.get(self.auto_approve_risk, 0)
        command_risk_value = risk_order.get(profile.risk, 1)
        needs_approval = command_risk_value > auto_risk_value

        return ExecApprovalDecision(
            allowed=True,
            risk_level=profile.risk,
            reason=f"Matched safe bin profile: {binary_name} ({profile.risk} risk)",
            requires_user_approval=needs_approval,
            matched_profile=binary_name,
        )

    # ── Windows-Safe Command Parser ───────────────────────

    @staticmethod
    def _split_command(command: str) -> list[str]:
        """Split a shell command into tokens, handling Windows backslash paths.

        Unlike shlex.split() which treats \\ as POSIX escape characters,
        this parser preserves Windows path backslashes.

        Strategy:
        1. Try shlex with posix=False first
        2. If that fails or produces garbled paths, use smart splitting
        """
        import shlex as _shlex
        import os as _os

        # On Windows, shlex in POSIX mode destroys backslash paths.
        # Use posix=False to disable escape processing.
        if _os.name == "nt":
            try:
                tokens = _shlex.split(command, posix=False)
                # Post-process: strip surrounding quotes that shlex may leave
                tokens = [t.strip('"').strip("'") for t in tokens if t.strip()]
                return tokens
            except ValueError:
                pass

        # Fallback: smart split that respects quoted strings
        return ExecApprovalManager._smart_split(command)

    @staticmethod
    def _smart_split(command: str) -> list[str]:
        """Split command by spaces, respecting quoted substrings."""
        tokens = []
        current = []
        in_quote = None  # None | '"' | "'"

        for ch in command:
            if in_quote:
                if ch == in_quote:
                    in_quote = None
                else:
                    current.append(ch)
            elif ch in ('"', "'"):
                in_quote = ch
            elif ch in (' ', '\t'):
                if current:
                    tokens.append(''.join(current))
                    current = []
            else:
                current.append(ch)

        if current:
            tokens.append(''.join(current))

        return tokens

    # ── Approval Bypass (for user-approved commands) ──────

    def approve_command(self, command: str, cwd: str | None = None) -> ExecApprovalDecision:
        """Override: allow a previously-blocked command after explicit user approval.

        When the user explicitly approves a command that was blocked by
        safe_bin / subcommand / inline_eval checks, this method returns
        an approved decision with elevated risk tracking.
        """
        decision = self.vet_command(command, cwd)
        if not decision.allowed:
            # Override: user explicitly approved
            return ExecApprovalDecision(
                allowed=True,
                risk_level="HIGH",
                reason=f"User-approved override: {decision.reason}",
                requires_user_approval=False,  # Already approved
                suggested_alternative=decision.suggested_alternative,
                matched_profile=decision.matched_profile,
            )
        return decision

    def get_command_preview(self, command: str, cwd: str | None = None) -> str:
        """Generate a human-readable preview of what the command will do."""
        decision = self.vet_command(command, cwd)
        parts = [
            f"Command: `{command}`",
            f"Working directory: {cwd or '(current)'}",
            f"Risk: {decision.risk_level}",
            f"Profile: {decision.matched_profile or 'none'}",
            f"Approval: {'Required' if decision.requires_user_approval else 'Auto-approved'}",
        ]
        if not decision.allowed:
            parts.append(f"⚠ BLOCKED: {decision.reason}")
        if decision.suggested_alternative:
            parts.append(f"💡 Suggestion: {decision.suggested_alternative}")
        return "\n".join(parts)

    def add_safe_bin(self, name: str, profile: SafeBinProfile) -> None:
        """Register a new safe binary at runtime."""
        self.safe_bins[name] = profile
        logger.info("Added safe bin: %s (risk=%s)", name, profile.risk)

    def remove_safe_bin(self, name: str) -> bool:
        """Remove a safe binary from the allowlist."""
        return self.safe_bins.pop(name, None) is not None
