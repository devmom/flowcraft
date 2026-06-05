"""Efficiency metrics evaluator.

Computes: step count, model call count, tool call count, token usage,
execution duration, redundancy ratio.
"""
from __future__ import annotations

from typing import Any


class EfficiencyEvaluator:
    """Evaluates efficiency metrics from trace events."""

    name = "efficiency"

    # Expected ranges for efficiency scoring (lower half gives max score)
    EFFICIENCY_BENCHMARKS = {
        "step_count": {"excellent": 3, "good": 5, "max": 8},
        "model_calls": {"excellent": 4, "good": 8, "max": 15},
        "tool_calls": {"excellent": 3, "good": 6, "max": 12},
        "duration_s": {"excellent": 15, "good": 45, "max": 120},
    }

    def evaluate(self, traces: list[dict], case: dict | None = None) -> dict[str, Any]:
        """Compute efficiency metrics from trace events."""
        # Count events by type
        step_count = sum(
            1 for e in traces
            if e.get("event_type") in ("step.started", "step.completed")
        ) // 2  # Divide by 2 for start+complete pairs

        model_calls = sum(
            1 for e in traces
            if e.get("event_type") == "model.requested"
        )

        tool_calls = sum(
            1 for e in traces
            if e.get("event_type") == "tool.requested"
        )

        # Tool result quality: completed vs failed
        tool_completed = sum(
            1 for e in traces
            if e.get("event_type") == "tool.completed"
        )
        tool_failed = sum(
            1 for e in traces
            if e.get("event_type") == "tool.failed"
        )

        # Token usage from payload
        total_tokens = 0
        for e in traces:
            payload = e.get("payload", {})
            if isinstance(payload, dict):
                total_tokens += payload.get("tokens", 0)
                total_tokens += payload.get("input_tokens", 0)
                total_tokens += payload.get("output_tokens", 0)

        # Duration from trace timestamps
        duration_s = 0.0
        if traces:
            first_ts = traces[0].get("created_at", "")
            last_ts = traces[-1].get("created_at", "")
            try:
                from datetime import datetime
                if first_ts and last_ts:
                    t1 = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
                    t2 = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                    duration_s = (t2 - t1).total_seconds()
            except (ValueError, TypeError):
                pass

        # Redundancy detection
        tool_names_used = [
            e.get("payload", {}).get("tool_name", "")
            for e in traces
            if e.get("event_type") == "tool.requested"
        ]
        duplicate_tools = len(tool_names_used) - len(set(tool_names_used))
        redundancy_ratio = duplicate_tools / max(len(tool_names_used), 1)

        # Score each metric (0-1)
        def _score_metric(value: float, benchmarks: dict) -> float:
            if value <= benchmarks["excellent"]:
                return 1.0
            if value <= benchmarks["good"]:
                return 0.8
            if value <= benchmarks["max"]:
                return 0.5
            return 0.2

        step_score = _score_metric(step_count, self.EFFICIENCY_BENCHMARKS["step_count"])
        model_score = _score_metric(model_calls, self.EFFICIENCY_BENCHMARKS["model_calls"])
        tool_score = _score_metric(tool_calls, self.EFFICIENCY_BENCHMARKS["tool_calls"])
        duration_score = _score_metric(duration_s, self.EFFICIENCY_BENCHMARKS["duration_s"])
        redundancy_score = max(0.0, 1.0 - redundancy_ratio * 3)

        overall = (step_score + model_score + tool_score + duration_score + redundancy_score) / 5

        return {
            "score": overall,
            "step_count": step_count,
            "model_calls": model_calls,
            "tool_calls": tool_calls,
            "tool_completed": tool_completed,
            "tool_failed": tool_failed,
            "tool_success_rate": tool_completed / max(tool_calls, 1),
            "total_tokens": total_tokens,
            "duration_s": round(duration_s, 2),
            "duplicate_tools": duplicate_tools,
            "redundancy_ratio": round(redundancy_ratio, 3),
            "metric_scores": {
                "step_score": step_score,
                "model_score": model_score,
                "tool_score": tool_score,
                "duration_score": duration_score,
                "redundancy_score": redundancy_score,
            },
        }
