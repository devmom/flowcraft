"""DynamicScriptExecutor — Phase 2 Mini ReAct loop.

When the Planner generates a step with execution_mode="dynamic_script",
this executor runs a constrained ReAct loop:
  1. SCRIPT_GEN:    LLM generates Python script for the step objective
  2. STATIC_CHECK:  AST safety validation (blocked imports, dangerous calls)
  3. SANDBOX_RUN:   Execute in restricted sandbox via CodeExecuteTool
  4. VALIDATE:      Check output quality and schema compliance
  5. RETRY:         On failure, feed error back to LLM (max 3 attempts)

Phase 3: Successful scripts are auto-saved as agent_generated skills.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flowcraft_core.domain.schemas import PlanStep
from flowcraft_core.models.gateway import ModelGateway
from flowcraft_core.skills.models import DynamicScriptResult
from flowcraft_core.tools.sandbox import _validate_code_safety

logger = logging.getLogger(__name__)

# Schema for the LLM to generate structured script code
SCRIPT_GEN_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {
            "type": "string",
            "description": "Brief reasoning about the approach",
        },
        "script": {
            "type": "string",
            "description": "Complete Python script to execute. Use only safe imports (math, json, re, statistics, random, datetime, collections, itertools, functools, csv). The script should print its results as JSON at the end.",
        },
        "expected_output_format": {
            "type": "string",
            "description": "Describe the expected JSON structure of the output",
        },
    },
    "required": ["reasoning", "script"],
}

# Max retries for dynamic script generation
MAX_SCRIPT_ATTEMPTS = 3
# Timeout per sandbox execution
SANDBOX_TIMEOUT = 15  # seconds


class DynamicScriptExecutor:
    """Mini ReAct executor for LLM-generated Python scripts.

    Flow:
      1. Generate script via LLM (structured output)
      2. Safety-check with AST analysis
      3. Execute in sandbox
      4. Validate output
      5. Retry up to MAX_SCRIPT_ATTEMPTS times on failure
      6. Optionally auto-save as agent_generated skill (Phase 3)
    """

    def __init__(
        self,
        model_gateway: ModelGateway,
        skill_registry=None,  # SkillRegistry, for Phase 3 auto-save
    ) -> None:
        self.model_gateway = model_gateway
        self.skill_registry = skill_registry

    async def execute(
        self,
        task_id: str,
        step_id: str,
        step: PlanStep,
        prior_context: str = "",
    ) -> DynamicScriptResult:
        """Execute a dynamic script for a plan step.

        Args:
            task_id: Parent task ID
            step_id: Current step ID
            step: The plan step (with objective, expected_output, etc.)
            prior_context: Results from previous steps for context

        Returns:
            DynamicScriptResult with status, output, and optionally saved skill name
        """
        t0 = _time.monotonic()
        last_script = ""
        last_error = ""

        for attempt in range(1, MAX_SCRIPT_ATTEMPTS + 1):
            logger.debug(
                "DynamicScript attempt %d/%d for step %s", attempt, MAX_SCRIPT_ATTEMPTS, step_id)

            # 1. Generate script
            if attempt == 1:
                script = await self._generate_script(step, prior_context)
            else:
                # Retry: include previous error for correction
                script = await self._generate_script(
                    step, prior_context,
                    previous_script=last_script,
                    previous_error=last_error,
                )

            if not script or not script.get("script"):
                return DynamicScriptResult(
                    task_id=task_id, step_id=step_id,
                    status="FAILED", script="",
                    error="LLM failed to generate script",
                    attempts=attempt,
                    total_elapsed=round(_time.monotonic() - t0, 3),
                )

            last_script = script["script"]

            # 2. Static safety check
            is_safe, error_msg = _validate_code_safety(last_script)
            if not is_safe:
                last_error = f"Safety check: {error_msg}"
                logger.warning("Dynamic script safety check failed: %s", error_msg)
                continue  # Retry with safety error feedback

            # 3. Execute in sandbox
            try:
                from flowcraft_core.tools.sandbox import CodeExecuteTool
                sandbox = CodeExecuteTool()
                from flowcraft_core.domain.schemas import ToolIntent
                intent = ToolIntent(
                    task_id=task_id,
                    step_id=step_id,
                    tool_name="code.execute",
                    purpose=f"Dynamic script: {step.objective[:80]}",
                    input_summary=f"Execute generated script ({len(last_script)} chars)",
                    input_payload={
                        "code": last_script,
                        "timeout_seconds": SANDBOX_TIMEOUT,
                    },
                    expected_result=step.expected_output,
                )
                observation = await sandbox.execute(intent)
            except Exception as exc:
                last_error = f"Sandbox execution error: {exc}"
                logger.warning("Dynamic script sandbox error: %s", exc)
                continue

            # 4. Validate output
            if observation.status == "COMPLETED":
                output = observation.output_payload.get("output", "")
                is_valid, validation_msg = self._validate_output(
                    output, step.expected_output)

                if is_valid:
                    elapsed = round(_time.monotonic() - t0, 3)
                    result = DynamicScriptResult(
                        task_id=task_id, step_id=step_id,
                        status="SUCCESS",
                        script=last_script,
                        output=output[:50000],
                        attempts=attempt,
                        total_elapsed=elapsed,
                        output_payload=observation.output_payload,
                    )

                    # Phase 3: Auto-save successful script as agent skill
                    if self.skill_registry:
                        try:
                            skill_name = self._derive_skill_name(step)
                            saved = self.skill_registry.save_agent_skill(
                                name=skill_name,
                                description=f"Auto-generated: {step.objective[:200]}",
                                category=self._derive_skill_category(step),
                                script_code=last_script,
                                tags=self._derive_skill_tags(step),
                            )
                            if saved:
                                result.saved_as_skill = saved.qualified_name
                                logger.info(
                                    "Dynamic script saved as skill: %s", saved.qualified_name)
                        except Exception as exc:
                            logger.debug("Failed to auto-save skill: %s", exc)

                    return result
                else:
                    last_error = f"Output validation: {validation_msg}"
                    logger.debug("Dynamic script output validation failed: %s", validation_msg)
            elif observation.status == "DENIED":
                return DynamicScriptResult(
                    task_id=task_id, step_id=step_id,
                    status="SAFETY_DENIED",
                    script=last_script,
                    error=observation.output_payload.get("error", "Safety check denied"),
                    attempts=attempt,
                    total_elapsed=round(_time.monotonic() - t0, 3),
                )
            else:
                last_error = observation.output_payload.get(
                    "error", observation.output_summary)
                logger.debug("Dynamic script execution failed: %s", last_error)

        # Max retries exhausted
        return DynamicScriptResult(
            task_id=task_id, step_id=step_id,
            status="MAX_RETRIES",
            script=last_script,
            error=last_error,
            attempts=MAX_SCRIPT_ATTEMPTS,
            total_elapsed=round(_time.monotonic() - t0, 3),
        )

    # ── Script Generation ──────────────────────────────────

    async def _generate_script(
        self,
        step: PlanStep,
        prior_context: str = "",
        previous_script: str = "",
        previous_error: str = "",
    ) -> dict[str, Any] | None:
        """Call LLM to generate a Python script for the step objective.

        Uses structured output (JSON schema) to get clean script code.
        """
        if not self.model_gateway.is_live():
            return self._fallback_script(step)

        # Build prompt
        parts = [
            "## Task",
            f"**Objective**: {step.objective}",
            f"**Expected Output**: {step.expected_output}",
        ]
        if prior_context:
            parts.append(f"\n## Context from Previous Steps\n{prior_context[:2000]}")

        if previous_error:
            parts.append(f"\n## Previous Attempt Failed")
            parts.append(f"**Error**: {previous_error}")
            parts.append(f"**Previous Script**:\n```python\n{previous_script[:1500]}\n```")

        parts.append("""
## Allowed Imports (ONLY these)
math, statistics, random, datetime, time, collections,
itertools, functools, operator, json, csv, re, string, textwrap,
typing, dataclasses, enum, copy, pprint, hashlib, base64,
html, xml.etree.ElementTree, urllib.parse, decimal, fractions,
heapq, bisect, array, struct, uuid, unicodedata

## FORBIDDEN
- NO os, subprocess, sys, socket, shutil, ctypes, pathlib
- NO open(), file operations
- NO network access
- NO exec, eval, compile, __import__

## Requirements
1. Write a COMPLETE, self-contained Python script
2. Process data and print the final result as JSON at the end
3. Handle errors gracefully with try/except
4. Keep it under 200 lines
5. The script MUST be valid Python 3.12+ syntax
""")

        prompt = "\n".join(parts)
        max_tokens = 4096

        try:
            result = await asyncio.wait_for(
                self.model_gateway._adapter.structured_chat(
                    [
                        {
                            "role": "system",
                            "content": (
                                "You are a Python code generator for FlowCraft's sandbox. "
                                "Generate clean, safe, self-contained Python scripts. "
                                "Output results as JSON via print(). "
                                "Only use the allowed imports listed. "
                                "Never use os, subprocess, sys, or file operations."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    SCRIPT_GEN_SCHEMA,
                    temperature=0.1,
                    max_tokens=max_tokens,
                ),
                timeout=45.0,
            )
            return result
        except asyncio.TimeoutError:
            logger.warning("Dynamic script generation timed out")
            return None
        except Exception as exc:
            logger.warning("Dynamic script generation failed: %s", exc)
            return None

    def _fallback_script(self, step: PlanStep) -> dict[str, Any] | None:
        """Generate a minimal fallback script when LLM is unavailable."""
        # Try to extract what the user wants
        obj = step.objective.lower()
        script = (
            'import json\n'
            'import math\n'
            '\n'
            '# Fallback script — LLM unavailable\n'
            f'# Task: {step.objective[:100]}\n'
            '\n'
            'def main():\n'
            '    result = {\n'
            '        "status": "fallback",\n'
            '        "message": "LLM unavailable — generated minimal script",\n'
            '        "task": ' + json.dumps(step.objective[:200]) + ',\n'
            '    }\n'
            '    print(json.dumps(result, ensure_ascii=False))\n'
            '\n'
            'if __name__ == "__main__":\n'
            '    main()\n'
        )
        return {"reasoning": "LLM unavailable fallback", "script": script}

    # ── Output Validation ──────────────────────────────────

    def _validate_output(self, output: str, expected: str) -> tuple[bool, str]:
        """Validate script output quality.

        Returns (is_valid, message).
        """
        if not output or len(output.strip()) < 5:
            return False, "Output is empty or too short"

        # Check if output contains obvious error markers
        error_markers = [
            "Traceback (most recent call last)",
            "SyntaxError",
            "NameError",
            "TypeError",
            "ImportError",
            "ModuleNotFoundError",
        ]
        for marker in error_markers:
            if marker in output:
                return False, f"Script error detected: {marker}"

        # Check if output is valid JSON (most script outputs should be JSON)
        # But don't fail if it's not — plain text is also acceptable
        try:
            json.loads(output.strip().split("\n")[-1])
            return True, "Valid JSON output"
        except json.JSONDecodeError:
            # Not JSON but has content — acceptable
            if len(output.strip()) > 50:
                return True, "Non-JSON but substantial output"
            return False, "Output too short and not valid JSON"

    # ── Skill Name Derivation (Phase 3) ────────────────────

    @staticmethod
    def _derive_skill_name(step: PlanStep) -> str:
        """Derive a skill name from step details."""
        import re
        # Take first 3 meaningful words from the objective
        words = re.findall(r'[a-zA-Z0-9]+', step.objective.lower())
        # Filter stop words
        stop = {'the', 'a', 'an', 'is', 'are', 'was', 'for', 'and', 'or', 'of', 'to', 'in', 'on', 'at'}
        meaningful = [w for w in words if w not in stop]
        name = '_'.join(meaningful[:3]) if meaningful else step.title.lower().replace(' ', '_')
        # Sanitize
        name = re.sub(r'[^a-z0-9_]', '', name)[:40]
        return name or f"dynamic_skill_{step.index}"

    @staticmethod
    def _derive_skill_category(step: PlanStep) -> str:
        """Guess skill category from step context."""
        obj = step.objective.lower()
        if any(kw in obj for kw in ('data', 'analy', 'stat', 'chart', 'graph', 'plot')):
            return "data"
        if any(kw in obj for kw in ('file', 'rename', 'move', 'copy', 'delete', 'dir')):
            return "files"
        if any(kw in obj for kw in ('web', 'scrape', 'http', 'api', 'download')):
            return "network"
        if any(kw in obj for kw in ('text', 'string', 'format', 'parse', 'json', 'csv')):
            return "text"
        if any(kw in obj for kw in ('image', 'picture', 'photo', 'resize', 'convert')):
            return "media"
        return "general"

    @staticmethod
    def _derive_skill_tags(step: PlanStep) -> list[str]:
        """Derive tags from step objective."""
        obj = step.objective.lower()
        tags = []
        tag_keywords = {
            'python': ['python', 'script', 'code'],
            'json': ['json', 'serialize'],
            'csv': ['csv', 'spreadsheet', 'excel'],
            'api': ['api', 'rest', 'http'],
            'math': ['math', 'calculate', 'statistic', 'formula'],
            'text': ['text', 'string', 'parse', 'format'],
            'data': ['data', 'analyze', 'analysis', 'dataset'],
            'automation': ['auto', 'batch', 'bulk', 'loop'],
        }
        for tag, keywords in tag_keywords.items():
            if any(kw in obj for kw in keywords):
                tags.append(tag)
        return tags[:5]
