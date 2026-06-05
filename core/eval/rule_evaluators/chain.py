"""Call chain completeness evaluator.

Checks: ToolIntent->ToolObservation pairing, approval coverage, audit coverage.
"""
from __future__ import annotations

from typing import Any


class ChainEvaluator:
    """Evaluates the completeness of tool call chains and approval coverage."""

    name = "chain"

    def evaluate(self, traces: list[dict], case: dict | None = None) -> dict[str, Any]:
        """Check chain integrity: orphants, approval coverage, event sequence."""

        # 1. ToolIntent -> ToolObservation pairing
        tool_intents = [
            e for e in traces
            if e.get("event_type") == "tool.requested"
        ]
        tool_observations = [
            e for e in traces
            if e.get("event_type") in ("tool.completed", "tool.failed")
        ]

        intent_ids = {
            e.get("payload", {}).get("tool_intent_id", "")
            for e in tool_intents
            if e.get("payload", {}).get("tool_intent_id")
        }
        obs_ids = {
            e.get("payload", {}).get("tool_intent_id", "")
            for e in tool_observations
            if e.get("payload", {}).get("tool_intent_id")
        }
        orphan_intents = intent_ids - obs_ids
        extra_obs = obs_ids - intent_ids  # Observations without matching intent

        chain_completeness = (
            1.0 - len(orphan_intents) / max(len(intent_ids), 1)
        )

        # 2. High-risk tool approval coverage
        high_risk_intents = [
            e for e in tool_intents
            if e.get("payload", {}).get("risk_level") in ("HIGH", "CRITICAL")
        ]
        approvals = [
            e for e in traces
            if e.get("event_type") == "approval.requested"
        ]
        approval_coverage = (
            len(approvals) / max(len(high_risk_intents), 1)
            if high_risk_intents else 1.0
        )

        # 3. Model call response pairing
        model_requests = [
            e for e in traces
            if e.get("event_type") in ("model.requested",)
        ]
        model_responses = [
            e for e in traces
            if e.get("event_type") in ("model.completed",)
        ]
        model_pair_ratio = (
            len(model_responses) / max(len(model_requests), 1)
        )

        # 4. Task completion check
        has_task_end = any(
            e.get("event_type") in ("task.completed", "task.failed", "task.cancelled")
            for e in traces
        )

        # Aggregate
        scores = [
            chain_completeness,
            approval_coverage,
            model_pair_ratio,
            1.0 if has_task_end else 0.5,
        ]
        overall = sum(scores) / len(scores)

        return {
            "score": overall,
            "chain_completeness": chain_completeness,
            "orphan_intents": len(orphan_intents),
            "orphan_intent_ids": list(orphan_intents)[:10],
            "extra_observations": len(extra_obs),
            "approval_coverage": approval_coverage,
            "high_risk_tool_count": len(high_risk_intents),
            "approval_count": len(approvals),
            "model_pair_ratio": model_pair_ratio,
            "model_requests": len(model_requests),
            "model_responses": len(model_responses),
            "has_task_end": has_task_end,
            "total_events": len(traces),
        }
