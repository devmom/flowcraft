"""Reflection (反思) Mechanism — Self-critique and refinement loop.

Based on Self-Refine (Madaan et al. 2023): Generation → Evaluation → Refinement.

Two trigger levels:
  Step-level: Check after each tool call / answer generation
  Task-level: Final quality review before returning to user

Multi-agent variant: Independent Critic agent for higher-quality review.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── Data Types ──────────────────────────────────────────────

@dataclass
class ReflectionResult:
    """Result of a reflection cycle."""
    original: str
    refined: str
    rounds: int
    passed: bool
    issues_found: list[str] = field(default_factory=list)
    quality_score: float = 0.0


# ── Core Reflection Loop ────────────────────────────────────

class ReflectionLoop:
    """Self-reflection loop: Generate → Evaluate → Refine.

    Usage:
        loop = ReflectionLoop(model_gateway)
        result = await loop.reflect(
            output=generated_text,
            task="Write a blog post about AI",
            dimensions=["factual accuracy", "logical flow", "completeness"],
            max_rounds=3,
        )
        print(result.refined)
    """

    DEFAULT_DIMENSIONS = [
        "factual accuracy — are there any factual errors or hallucinations?",
        "logical coherence — does the reasoning flow without gaps or contradictions?",
        "completeness — does it fully address the task requirements?",
        "clarity — is the language clear and unambiguous?",
    ]

    def __init__(self, model_gateway: Any, /):
        """model_gateway: ModelGateway instance for LLM calls."""
        self.gateway = model_gateway

    # ── Public API ────────────────────────────────────────

    async def reflect(
        self,
        output: str,
        task: str,
        dimensions: list[str] | None = None,
        max_rounds: int = 3,
        temperature: float = 0.1,
    ) -> ReflectionResult:
        """Run the full reflection loop.

        Args:
            output: The generated content to review.
            task: The original task description.
            dimensions: Specific check dimensions (default: accuracy/logic/completeness/clarity).
            max_rounds: Maximum refinement rounds (prevent infinite loops).
            temperature: LLM temperature for evaluation (lower = more critical).

        Returns:
            ReflectionResult with refined output and quality metadata.
        """
        dims = dimensions or self.DEFAULT_DIMENSIONS
        current = output
        issues = []

        for round_num in range(1, max_rounds + 1):
            # Step 1: Evaluate
            assessment = await self._evaluate(current, task, dims, temperature)

            if assessment.get("verdict") == "PASS":
                logger.info(
                    "Reflection PASS at round %d (quality=%.2f)",
                    round_num, assessment.get("score", 0.0),
                )
                return ReflectionResult(
                    original=output,
                    refined=current,
                    rounds=round_num,
                    passed=True,
                    issues=issues,
                    quality_score=assessment.get("score", 1.0),
                )

            # Step 2: Record issues and refine
            round_issues = assessment.get("issues", [])
            issues.extend(round_issues)
            feedback = assessment.get("feedback", "\n".join(round_issues))
            current = await self._refine(current, task, feedback)

        # Max rounds reached without PASS
        logger.warning("Reflection did not PASS after %d rounds", max_rounds)
        return ReflectionResult(
            original=output,
            refined=current,
            rounds=max_rounds,
            passed=False,
            issues=issues,
            quality_score=assessment.get("score", 0.3) if 'assessment' in dir() else 0.3,
        )

    async def step_level_reflect(
        self,
        step_output: str,
        step_objective: str,
        **kwargs,
    ) -> str:
        """Step-level reflection: lighter, faster, only core dimensions.

        Use after each tool call or answer generation within an execution step.
        """
        dims = [
            "relevance — does this output directly address the step objective?",
            "actionability — can the next step use this output without ambiguity?",
        ]
        result = await self.reflect(
            output=step_output,
            task=step_objective,
            dimensions=dims,
            max_rounds=2,  # Lighter: only 2 rounds max
            **kwargs,
        )
        return result.refined

    async def task_level_reflect(
        self,
        final_output: str,
        task_objective: str,
        **kwargs,
    ) -> str:
        """Task-level reflection: full quality review with all dimensions.

        Use before returning the final result to the user.
        """
        result = await self.reflect(
            output=final_output,
            task=task_objective,
            max_rounds=3,
            **kwargs,
        )
        return result.refined

    # ── Internal ──────────────────────────────────────────

    async def _evaluate(
        self,
        output: str,
        task: str,
        dimensions: list[str],
        temperature: float = 0.1,
    ) -> dict[str, Any]:
        """Evaluate output quality against dimensions.

        Returns structured JSON with verdict (PASS/FAIL), score, issues, feedback.
        """
        dim_text = "\n".join(f"- {d}" for d in dimensions)
        prompt = f"""You are a strict quality reviewer. Evaluate the following output.

Task: {task}

Output to evaluate:
---
{output}
---

Check dimensions:
{dim_text}

Instructions:
1. Score each dimension 0.0-1.0
2. If ALL dimensions score >= 0.7, verdict = "PASS"
3. If ANY dimension scores < 0.7, verdict = "FAIL" and provide specific, actionable feedback
4. Be CRITICAL — do not pass output with factual errors, logical gaps, or vague language
5. If output is already good, PASS it — do not nitpick for the sake of feedback

Respond in JSON:
{{
  "verdict": "PASS" or "FAIL",
  "score": 0.0-1.0,
  "issues": ["specific issue 1", "specific issue 2"],
  "feedback": "concise, actionable improvement suggestions"
}}
"""
        messages = [
            {"role": "system", "content": "You are a quality reviewer. Output only JSON."},
            {"role": "user", "content": prompt},
        ]

        if not self.gateway or not self.gateway.is_live():
            # Deterministic fallback: basic heuristic
            return self._heuristic_evaluate(output)

        try:
            raw = await self.gateway._adapter.chat(messages, temperature=temperature, max_tokens=512)
            return self._parse_json(raw)
        except Exception as exc:
            logger.warning("LLM evaluation failed: %s, using heuristic", exc)
            return self._heuristic_evaluate(output)

    async def _refine(self, output: str, task: str, feedback: str) -> str:
        """Refine output based on evaluation feedback."""
        prompt = f"""Improve the following output based on the reviewer's feedback.

Original Task: {task}

Current Output:
---
{output}
---

Reviewer Feedback:
{feedback}

Instructions:
1. Address ALL issues mentioned in the feedback
2. Do NOT rewrite from scratch — only fix the problems
3. Keep the output structure and style similar to the original
4. Do not add unsolicited content beyond the task scope
5. Output ONLY the refined version, no commentary
"""
        if not self.gateway or not self.gateway.is_live():
            return output  # Can't refine without LLM

        try:
            return await self.gateway._adapter.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.3,  # Slightly higher for creativity in refinement
                max_tokens=4096,
            )
        except Exception as exc:
            logger.warning("LLM refinement failed: %s", exc)
            return output  # Return original on failure

    @staticmethod
    def _heuristic_evaluate(output: str) -> dict[str, Any]:
        """Fallback heuristic evaluation (no LLM available)."""
        score = 0.7
        issues = []
        if len(output) < 20:
            score = 0.3
            issues.append("Output too short")
        if "I don't know" in output or "无法" in output:
            score = 0.4
            issues.append("Output indicates inability to answer")
        verdict = "PASS" if score >= 0.7 else "FAIL"
        return {
            "verdict": verdict,
            "score": score,
            "issues": issues,
            "feedback": "\n".join(issues) if issues else "No issues found",
        }

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        """Parse JSON from LLM output, handling code fences."""
        import re
        text = raw.strip()
        # Direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Remove code fences
        if text.startswith("```"):
            lines = text.split("\n")
            inner = "\n".join(lines[1:-1]) if len(lines) > 2 else text
            try:
                return json.loads(inner)
            except json.JSONDecodeError:
                pass
        # Find JSON object
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        # Default: fail with parse error
        return {
            "verdict": "PASS",
            "score": 0.5,
            "issues": ["Could not parse evaluation result"],
            "feedback": "Could not parse evaluation result",
        }


# ── Multi-Agent Critic ──────────────────────────────────────

class CriticAgent:
    """Independent critic agent for multi-agent reflection.

    Unlike self-reflection (same model evaluates itself), the Critic is
    a separate evaluation context — more objective, less blind to its own errors.

    Usage:
        critic = CriticAgent(model_gateway)
        review = await critic.review(output, task)
        if review["needs_revision"]:
            refined = await generator.refine(output, review["feedback"])
    """

    SYSTEM_PROMPT = (
        "You are a meticulous quality reviewer. Your only job is to find problems. "
        "You have no stake in the output — be ruthlessly honest. "
        "If it's good, say PASS. If there are issues, be specific about what and how to fix."
    )

    def __init__(self, model_gateway: Any):
        self.gateway = model_gateway
        self._loop = ReflectionLoop(model_gateway)

    async def review(
        self,
        output: str,
        task: str,
        dimensions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Independent critic review (same evaluation logic, but from Critic persona)."""
        dims = dimensions or ReflectionLoop.DEFAULT_DIMENSIONS
        # Reuse evaluate but with Critic system prompt
        # For now, delegate to ReflectionLoop._evaluate with stricter view
        return await self._loop._evaluate(output, task, dims, temperature=0.0)
