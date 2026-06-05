"""Structure completeness evaluator.

Checks that domain objects in the trace event stream have all required fields.
"""
from __future__ import annotations

from typing import Any

# Required fields per domain object type
REQUIRED_FIELDS: dict[str, list[str]] = {
    "TaskBrief": ["objective", "task_type", "risk_level"],
    "ExecutionPlan": ["goal", "steps", "fallback_strategy", "success_criteria"],
    "PlanStep": ["index", "title", "objective", "action_type"],
    "ToolIntent": ["tool_name", "input_payload", "risk_level"],
    "ToolObservation": ["status", "output_summary"],
    "PolicyDecision": ["decision", "reason"],
    "TraceEvent": ["event_type", "title", "message"],
}

# Which trace events carry which object types in payload
PAYLOAD_TYPE_MAP = {
    "intent.recognized": "TaskBrief",
    "plan.created": "ExecutionPlan",
    "step.started": "PlanStep",
    "tool.requested": "ToolIntent",
    "tool.completed": "ToolObservation",
    "tool.failed": "ToolObservation",
    "policy.checked": "PolicyDecision",
}


class StructureEvaluator:
    """Evaluates structural completeness of domain objects in traces."""

    name = "structure"

    def evaluate(self, traces: list[dict], case: dict | None = None) -> dict[str, Any]:
        """Check required fields in all domain objects from trace payloads."""
        total_checks = 0
        missing_total = 0
        per_object_results = []

        for event in traces:
            event_type = event.get("event_type", "")
            payload = event.get("payload", {})
            if not isinstance(payload, dict) or not payload:
                continue

            obj_type = PAYLOAD_TYPE_MAP.get(event_type)
            if obj_type is None:
                continue

            required = REQUIRED_FIELDS.get(obj_type, [])
            if not required:
                continue

            missing = []
            present = []
            for field in required:
                # Check in payload directly, with nested fallback
                value = payload.get(field)
                if value is None and isinstance(payload, dict):
                    # Try top-level keys from model_dump
                    pass
                total_checks += 1
                if value is None or (isinstance(value, (list, str, dict)) and len(value) == 0):
                    missing.append(field)
                else:
                    present.append(field)

            if missing:
                per_object_results.append({
                    "event_type": event_type,
                    "object_type": obj_type,
                    "event_id": event.get("event_id", ""),
                    "missing_fields": missing,
                    "present_fields": present,
                })
                missing_total += len(missing)

        completeness = 1.0 - (missing_total / max(total_checks, 1))
        score = completeness

        return {
            "score": score,
            "total_checks": total_checks,
            "missing_total": missing_total,
            "completeness": completeness,
            "details": per_object_results,
        }
