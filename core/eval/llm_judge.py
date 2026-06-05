"""LLM-as-Judge evaluator for agent execution quality scoring."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


EVAL_SYSTEM_PROMPT = """You are an objective Agent Execution Quality Evaluator.
Rate each dimension from 1 (worst) to 5 (best):

1. goal_completion: Did the agent fully achieve the task goal?
2. accuracy: Are all facts in the output correct?
3. completeness: Does the output include all necessary information?
4. efficiency: Did the agent use an appropriate number of steps?
5. tool_appropriateness: Were tools used correctly with proper parameters?

Return ONLY a JSON object:
{
    "goal_completion": int,
    "accuracy": int,
    "completeness": int,
    "efficiency": int,
    "tool_appropriateness": int,
    "overall_score": float,
    "strengths": [str],
    "weaknesses": [str],
    "suggestions": [str],
    "explanation": str
}"""


def _build_eval_prompt(case: dict, traces_summary: str, final_output: str) -> str:
    """Build the evaluation prompt from case, traces, and output."""
    user_input = case.get("input", {}).get("raw_input", "")
    expected = case.get("expected", {})
    success_criteria = expected.get("success_criteria", [])
    task_type = expected.get("task_type", "UNKNOWN")
    difficulty = case.get("difficulty", "UNKNOWN")

    return f"""## Task Information
- User Input: {user_input}
- Task Type: {task_type}
- Difficulty: {difficulty}
- Success Criteria: {json.dumps(success_criteria, ensure_ascii=False)}

## Agent Execution Summary
{traces_summary}

## Agent Final Output
{final_output}

## Expected Output Patterns
{json.dumps(expected.get("golden_output_patterns", []), ensure_ascii=False)}

## Forbidden Patterns
{json.dumps(expected.get("forbidden_patterns", []), ensure_ascii=False)}

Please evaluate the agent's execution quality based on the rubric."""


class LLMJudge:
    """LLM-based quality evaluator for agent execution."""

    def __init__(self, model_gateway, judge_model: str = "default"):
        self.gateway = model_gateway
        self.judge_model = judge_model

    def evaluate(self, case: dict, traces: list[dict],
                 final_output: str) -> dict[str, Any]:
        """Evaluate agent execution quality using LLM Judge."""
        traces_summary = self._summarize_traces(traces)
        prompt = _build_eval_prompt(case, traces_summary, final_output)

        try:
            response = self._call_judge(prompt)
            result = self._parse_judge_response(response)
            return result
        except Exception as exc:
            logger.warning("LLM Judge evaluation failed: %s", exc)
            return self._fallback_evaluation(
                case, traces, final_output, str(exc))

    def _summarize_traces(self, traces: list[dict]) -> str:
        """Create a compact summary of trace events."""
        lines = []
        for e in traces:
            et = e.get("event_type", "?")
            title = e.get("title", "")
            msg = e.get("message", "")
            payload = e.get("payload", {})

            if et in ("task.created", "intent.recognized"):
                lines.append(f"[{et}] {title}: {msg}")
            elif et == "plan.created":
                steps = payload.get("steps", [])
                names = [s.get("title", str(s.get("index", "?")))
                        for s in (steps if isinstance(steps, list) else [])]
                lines.append(
                    f"[plan.created] {len(names)} steps: "
                    f"{' -> '.join(names[:6])}")
            elif et.startswith("step."):
                lines.append(f"[{et}] {title}: {msg[:200]}")
            elif et.startswith("tool."):
                tn = payload.get("tool_name", "")
                st = payload.get("status", "")
                sm = payload.get("output_summary", msg)[:150]
                lines.append(f"[{et}] {tn} ({st}): {sm}")
            elif et in ("approval.requested", "approval.resolved"):
                lines.append(f"[{et}] {title}")
            elif et in ("task.completed", "task.failed"):
                lines.append(f"[{et}] {msg[:200]}")
            elif et == "policy.checked":
                lines.append(
                    f"[policy.checked] "
                    f"decision={payload.get('decision', '')}")

        return "\n".join(lines[-60:])

    def _call_judge(self, prompt: str) -> str:
        """Call the judge model synchronously."""
        import asyncio
        try:
            return asyncio.run(
                self.gateway.generate(
                    system_prompt=EVAL_SYSTEM_PROMPT,
                    user_prompt=prompt,
                    temperature=0.1,
                )
            )
        except RuntimeError:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as ex:
                future = ex.submit(
                    asyncio.run,
                    self.gateway.generate(
                        system_prompt=EVAL_SYSTEM_PROMPT,
                        user_prompt=prompt,
                        temperature=0.1,
                    )
                )
                return future.result(timeout=60)

    def _parse_judge_response(self, response: str) -> dict[str, Any]:
        """Parse judge JSON response, with fallback handling."""
        cleaned = re.sub(r'^```(?:json)?\s*', '', response.strip())
        cleaned = re.sub(r'\s*```$', '', cleaned)

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r'\{.*\}', response, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    return self._fallback_parse(response)
            else:
                return self._fallback_parse(response)

        required = ["goal_completion", "accuracy", "completeness",
                     "efficiency", "tool_appropriateness"]
        for key in required:
            if key not in data:
                data[key] = 3
            else:
                data[key] = max(1, min(5, int(data[key])))

        if "overall_score" not in data:
            scores = [data[k] for k in required]
            data["overall_score"] = round(sum(scores) / len(scores), 1)

        data.setdefault("strengths", [])
        data.setdefault("weaknesses", [])
        data.setdefault("suggestions", [])
        data.setdefault("explanation", "")

        return data

    def _fallback_parse(self, response: str) -> dict[str, Any]:
        logger.warning("LLM Judge JSON parse failed")
        return {
            "goal_completion": 3, "accuracy": 3, "completeness": 3,
            "efficiency": 3, "tool_appropriateness": 3,
            "overall_score": 3.0,
            "strengths": [],
            "weaknesses": ["Judge response could not be parsed"],
            "suggestions": ["Check judge model output format"],
            "explanation": f"Raw: {response[:200]}",
            "parse_error": True,
        }

    def _fallback_evaluation(self, case: dict, traces: list[dict],
                             final_output: str, error: str) -> dict[str, Any]:
        """Heuristic fallback when judge model is unavailable."""
        score = 3.0
        if any(e.get("event_type") == "task.completed" for e in traces):
            score = 3.5
        if final_output and len(final_output) > 50:
            score += 0.5
        if final_output and len(final_output) > 200:
            score += 0.5
        if any(e.get("event_type") in ("tool.failed", "task.failed")
               for e in traces):
            score -= 1.0
        score = max(1.0, min(5.0, score))

        return {
            "goal_completion": int(score), "accuracy": int(score),
            "completeness": int(score), "efficiency": int(score),
            "tool_appropriateness": int(score),
            "overall_score": score,
            "strengths": [],
            "weaknesses": [f"LLM Judge unavailable: {error}"],
            "suggestions": ["Ensure model API key is configured"],
            "explanation": f"Fallback heuristic. Error: {error}",
            "fallback": True,
        }
