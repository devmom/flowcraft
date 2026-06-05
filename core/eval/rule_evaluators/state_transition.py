"""State transition compliance evaluator.

Validates that task status transitions in the trace event stream
follow the defined state machine rules.
"""
from __future__ import annotations

from typing import Any

from flowcraft_core.domain.enums import TaskStatus


# Valid transitions per state
TRANSITION_RULES: dict[TaskStatus, list[TaskStatus]] = {
    TaskStatus.CREATED: [TaskStatus.INTENT_RECOGNIZED],
    TaskStatus.INTENT_RECOGNIZED: [TaskStatus.PLANNED],
    TaskStatus.PLANNED: [TaskStatus.WAITING_APPROVAL, TaskStatus.EXECUTING],
    TaskStatus.WAITING_APPROVAL: [TaskStatus.EXECUTING, TaskStatus.CANCELLED],
    TaskStatus.EXECUTING: [
        TaskStatus.WAITING_TOOL, TaskStatus.OBSERVING,
        TaskStatus.PAUSED, TaskStatus.FAILED, TaskStatus.COMPLETED,
    ],
    TaskStatus.WAITING_TOOL: [TaskStatus.OBSERVING, TaskStatus.FAILED],
    TaskStatus.OBSERVING: [
        TaskStatus.EXECUTING, TaskStatus.REPLANNING,
        TaskStatus.COMPLETED, TaskStatus.FAILED,
    ],
    TaskStatus.REPLANNING: [TaskStatus.PLANNED, TaskStatus.FAILED],
    TaskStatus.PAUSED: [TaskStatus.EXECUTING, TaskStatus.CANCELLED],
    TaskStatus.FAILED: [TaskStatus.REPLANNING, TaskStatus.CANCELLED],
}


class StateTransitionEvaluator:
    """Evaluates state transition compliance from trace events."""

    name = "state_transition"

    STATUS_EVENTS = {
        "task.created": TaskStatus.CREATED,
        "intent.recognized": TaskStatus.INTENT_RECOGNIZED,
        "plan.created": TaskStatus.PLANNED,
        "approval.requested": TaskStatus.WAITING_APPROVAL,
        "step.started": TaskStatus.EXECUTING,
        "tool.requested": TaskStatus.WAITING_TOOL,
        "tool.completed": TaskStatus.OBSERVING,
        "task.paused": TaskStatus.PAUSED,
        "task.completed": TaskStatus.COMPLETED,
        "task.failed": TaskStatus.FAILED,
        "task.cancelled": TaskStatus.CANCELLED,
        "plan.replanned": TaskStatus.REPLANNING,
    }

    def evaluate(self, traces: list[dict], case: dict | None = None) -> dict[str, Any]:
        """Extract status transitions from trace events and check compliance."""
        # Extract status sequence from events
        status_sequence: list[TaskStatus] = []
        for event in traces:
            event_type = event.get("event_type", "")
            if event_type in self.STATUS_EVENTS:
                status = self.STATUS_EVENTS[event_type]
                # Don't add duplicate consecutive statuses
                if not status_sequence or status_sequence[-1] != status:
                    status_sequence.append(status)

        # Also check payload status changes
        for event in traces:
            payload = event.get("payload", {})
            if isinstance(payload, dict):
                old_status = payload.get("old_status")
                new_status = payload.get("new_status")
                if new_status and new_status not in [s.value for s in status_sequence]:
                    try:
                        status_sequence.append(TaskStatus(new_status))
                    except ValueError:
                        pass

        if not status_sequence:
            return {
                "score": 1.0,
                "total_transitions": 0,
                "violations": 0,
                "compliance_rate": 1.0,
                "details": [],
                "status_sequence": [],
            }

        # Check each transition
        violations = []
        for i in range(len(status_sequence) - 1):
            current = status_sequence[i]
            next_state = status_sequence[i + 1]
            allowed = TRANSITION_RULES.get(current, [])
            if next_state not in allowed:
                violations.append({
                    "from": current.value,
                    "to": next_state.value,
                    "allowed": [s.value for s in allowed],
                })

        total = len(status_sequence) - 1
        compliance = 1.0 - (len(violations) / max(total, 1))
        score = compliance

        return {
            "score": score,
            "total_transitions": total,
            "violations": len(violations),
            "compliance_rate": compliance,
            "details": violations,
            "status_sequence": [s.value for s in status_sequence],
        }
