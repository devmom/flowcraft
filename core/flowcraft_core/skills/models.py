"""Skill data models - OpenClaw-inspired skill definitions.

Each skill is a directory containing:
  - SKILL.md: YAML frontmatter + markdown body (instructions for the agent)
  - scripts/: Deterministic executable code (Python/Bash)
  - references/: Optional documentation loaded on demand
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class SkillDefinition:
    """Definition of a single skill, parsed from SKILL.md YAML frontmatter."""

    name: str
    description: str
    category: str = "general"
    version: str = "1.0.0"
    author: str = "flowcraft"
    requires_approval: bool = False
    timeout_seconds: int = 60
    script_path: str | None = None  # Relative to skill dir, e.g. "scripts/main.py"
    script_language: str = "python"  # "python" | "bash"
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    skill_dir: str = ""  # Absolute path to skill directory (set by registry)
    usage_count: int = 0
    success_count: int = 0
    fail_count: int = 0
    last_used: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = ""
    source: str = "workspace"  # "builtin" | "workspace" | "marketplace" | "agent_generated"
    enabled: bool = True

    @property
    def qualified_name(self) -> str:
        """Unique tool-name compatible identifier."""
        return f"skill.{self.category}.{self.name}"

    @property
    def full_script_path(self) -> Path | None:
        """Absolute path to the deterministic script."""
        if not self.script_path or not self.skill_dir:
            return None
        return Path(self.skill_dir) / self.script_path

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.fail_count
        return self.success_count / total if total > 0 else 1.0

    def to_dict(self) -> dict:
        return {
            "name": self.name, "qualified_name": self.qualified_name,
            "description": self.description, "category": self.category,
            "script_language": self.script_language, "tags": self.tags,
            "source": self.source, "usage_count": self.usage_count,
            "success_rate": round(self.success_rate, 3),
        }

    def to_prompt_summary(self) -> str:
        """Compact one-line summary for planner / LLM prompt injection."""
        schema_hint = ""
        if self.input_schema:
            props = list(self.input_schema.get("properties", {}).keys())[:5]
            if props:
                schema_hint = f" params:({', '.join(props)})"
        return (
            f"- **{self.qualified_name}** [{self.script_language}]: "
            f"{self.description[:150]}{schema_hint}"
        )


@dataclass
class SkillManifest:
    """Complete parsed SKILL.md: frontmatter + body + optional files."""

    definition: SkillDefinition
    body: str = ""  # Markdown body (instructions the agent reads when skill is activated)
    raw_frontmatter: dict[str, Any] = field(default_factory=dict)

    def to_agent_context(self) -> str:
        """Build agent-readable context injected when skill is activated."""
        lines = [
            f"## Activated Skill: {self.definition.name}",
            f"**Purpose**: {self.definition.description}",
            f"**Category**: {self.definition.category}",
        ]
        if self.definition.tags:
            lines.append(f"**Tags**: {', '.join(self.definition.tags)}")
        if self.definition.script_path:
            lines.append("")
            lines.append(
                f"**Deterministic Script**: Execute "
                f"`{{baseDir}}/{self.definition.script_path}` "
                f"with appropriate arguments."
            )
            if self.definition.input_schema:
                lines.append(
                    f"**Input Schema**:\n```json\n"
                    f"{json.dumps(self.definition.input_schema, indent=2)}\n```"
                )
            if self.definition.output_schema:
                lines.append(
                    f"**Output Schema**:\n```json\n"
                    f"{json.dumps(self.definition.output_schema, indent=2)}\n```"
                )
        lines.append("")
        if self.body:
            lines.append("## Instructions")
            lines.append(self.body)
        return "\n".join(lines)


@dataclass
class SkillExecutionResult:
    """Result from executing a skill's deterministic script."""

    skill_name: str
    status: str = ""  # "SUCCESS" | "FAILED" | "TIMEOUT" | "DENIED"
    output: str = ""
    error: str | None = None
    elapsed_seconds: float = 0.0
    output_payload: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)  # Paths to generated files

    @property
    def is_success(self) -> bool:
        return self.status == "SUCCESS"


@dataclass
class DynamicScriptResult:
    """Result from a dynamic (LLM-generated) script execution via Mini ReAct loop."""

    task_id: str
    step_id: str
    status: str = ""  # "SUCCESS" | "FAILED" | "MAX_RETRIES" | "SAFETY_DENIED"
    script: str = ""  # The final script that was executed
    output: str = ""
    error: str | None = None
    attempts: int = 0
    total_elapsed: float = 0.0
    output_payload: dict[str, Any] = field(default_factory=dict)
    saved_as_skill: str | None = None  # Phase 3: Auto-saved skill name
