"""SkillExecuteTool — wraps skill scripts as FlowCraft Tools.

When the LLM decides to use a skill, this tool:
  1. Resolves the skill from the registry
  2. Injects the skill's agent context into the conversation
  3. Executes the deterministic script
  4. Returns the result as a ToolObservation
"""

from __future__ import annotations

import logging
from pathlib import Path

from flowcraft_core.domain.enums import RiskLevel
from flowcraft_core.domain.schemas import ToolIntent
from flowcraft_core.skills.models import SkillExecutionResult
from flowcraft_core.tools.base import Tool, ToolDefinition, observation_from_output

logger = logging.getLogger(__name__)


class SkillExecuteTool(Tool):
    """FlowCraft Tool that invokes a skill's deterministic script.

    Registered as "skill.execute" in the tool registry. The LLM calls this tool
    with a skill name and parameters, and the tool handles the execution.
    """

    def __init__(self, skill_registry=None) -> None:
        self._skill_registry = skill_registry
        self.definition = ToolDefinition(
            tool_name="skill.execute",
            display_name="执行技能脚本",
            description=(
                "执行一个预定义的技能脚本（确定性的 Python/Bash 代码）。"
                "参数: skill_name(技能名称, 不含 category 前缀), "
                "params(JSON 参数对象, 可选), "
                "timeout_seconds(超时秒数, 默认60)"
            ),
            category="skill",
            risk_level=RiskLevel.MEDIUM,
            permissions=["tool:skill.execute"],
            requires_approval_by_default=False,
            timeout_seconds=120,
        )

    def set_registry(self, skill_registry) -> None:
        """Lazy-set the skill registry (to avoid circular imports)."""
        self._skill_registry = skill_registry

    async def execute(self, intent: ToolIntent):
        """Execute a skill's deterministic script.

        Input payload:
          - skill_name: str (simple name, e.g., "data_analysis")
          - params: dict (optional, passed to the script)
          - timeout_seconds: int (optional, default 60)
        """
        if not self._skill_registry:
            return observation_from_output(
                intent, "FAILED",
                "Skill registry not available",
                error="Skill registry not initialized",
            )

        skill_name = str(intent.input_payload.get("skill_name", ""))
        if not skill_name:
            return observation_from_output(
                intent, "FAILED",
                "Missing skill_name parameter",
                error="skill_name is required",
            )

        params = intent.input_payload.get("params", {})
        if isinstance(params, str):
            import json
            try:
                params = json.loads(params)
            except json.JSONDecodeError:
                params = {"value": params}

        timeout = int(intent.input_payload.get("timeout_seconds", 60))

        # Try qualified name first, then simple name lookup
        manifest = self._skill_registry.get_skill(skill_name)
        if not manifest:
            manifest = self._skill_registry.get_skill_by_name(skill_name)

        if not manifest:
            # List available skills for helpful error message
            skills = self._skill_registry.list_skills(enabled_only=True)
            available = ", ".join(
                s.qualified_name for s in skills[:10])
            return observation_from_output(
                intent, "FAILED",
                f"Skill '{skill_name}' not found. Available: {available}",
                error=f"Unknown skill: {skill_name}",
            )

        qname = manifest.definition.qualified_name

        # Execute the deterministic script
        result: SkillExecutionResult = await self._skill_registry.execute_skill(
            qname, params=params, timeout_seconds=timeout,
        )

        if result.is_success:
            return observation_from_output(
                intent, "COMPLETED",
                f"Skill '{qname}' executed successfully in {result.elapsed_seconds:.2f}s",
                {
                    "skill_name": qname,
                    "output": result.output,
                    "output_payload": result.output_payload,
                    "elapsed_seconds": result.elapsed_seconds,
                    "artifacts": result.artifacts,
                },
            )
        elif result.status == "DENIED":
            return observation_from_output(
                intent, "DENIED",
                f"Skill '{qname}' is disabled",
                error=result.error,
            )
        elif result.status == "TIMEOUT":
            return observation_from_output(
                intent, "FAILED",
                f"Skill '{qname}' timed out after {result.elapsed_seconds:.0f}s",
                error=result.error,
            )
        else:
            return observation_from_output(
                intent, "FAILED",
                f"Skill '{qname}' failed: {result.error or 'Unknown error'}",
                error=result.error,
            )
