"""Root cause analysis for failed evaluation cases."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class RootCauseAnalyzer:
    """Analyzes trace events to identify root causes of agent failures."""

    # Known failure categories and their detection rules
    CATEGORIES = {
        "MODEL_OUTPUT_QUALITY": {
            "keywords": ["parse", "json", "decode", "invalid"],
            "suggestion": "Check Prompt Schema constraints; add fallback parsing logic",
        },
        "TOOL_SELECTION": {
            "event_types": ["tool.failed"],
            "check_payload": "error_message",
            "suggestion": "Verify tool list is correctly injected into the Prompt context",
        },
        "POLICY_VIOLATION": {
            "decision_values": ["DENY"],
            "suggestion": "Clarify permission boundaries in system prompt",
        },
        "EXECUTION_LOOP": {
            "detection": "loop",  # Custom detection logic
            "suggestion": "Check stop_conditions and CompletionChecker thresholds",
        },
        "INSUFFICIENT_INFO": {
            "failure_types": ["INSUFFICIENT_INFO"],
            "suggestion": "Check if clarification should be triggered instead of failing",
        },
        "TIMEOUT": {
            "failure_types": ["TIMEOUT"],
            "event_types": ["task.failed"],
            "suggestion": "Review tool timeout settings; consider splitting long tasks",
        },
        "APPROVAL_BLOCKED": {
            "event_types": ["approval.requested"],
            "check_sequence": True,
            "suggestion": "Task is waiting for user approval; this is expected behavior",
        },
    }

    def analyze(self, failed_result: dict) -> dict[str, Any]:
        """Analyze a failed evaluation result to find root causes.

        Args:
            failed_result: A single case result dict with traces and scores.

        Returns:
            Dict with causes list, primary cause, and severity.
        """
        traces = failed_result.get("traces", [])
        if not traces:
            return {
                "case_id": failed_result.get("case_id", "unknown"),
                "causes": [],
                "primary_cause": None,
                "severity": "LOW",
                "message": "No trace data available for analysis",
            }

        causes = []

        # 1. Check for model output parse errors
        parse_events = [
            e for e in traces
            if any(kw in str(e.get("message", "")).lower()
                   for kw in ["parse", "json", "decode error"])
        ]
        if parse_events:
            causes.append(self._make_cause(
                "MODEL_OUTPUT_QUALITY",
                f"Model output parsing failed ({len(parse_events)} occurrence(s))",
                parse_events[:3],
            ))

        # 2. Check for tool failures
        tool_failures = [
            e for e in traces
            if e.get("event_type") == "tool.failed"
        ]
        if tool_failures:
            tool_names = set(
                e.get("payload", {}).get("tool_name", "unknown")
                for e in tool_failures
            )
            causes.append(self._make_cause(
                "TOOL_SELECTION",
                f"Tool execution failed for: {', '.join(tool_names)}",
                tool_failures[:3],
            ))

        # 3. Check for policy denials
        denied = [
            e for e in traces
            if "DENY" in str(e.get("payload", {}).get("decision", ""))
        ]
        if denied:
            causes.append(self._make_cause(
                "POLICY_VIOLATION",
                f"Agent attempted policy-blocked operation(s)",
                denied[:3],
            ))

        # 4. Check for execution loops (repeated steps)
        if self._detect_loop(traces):
            causes.append(self._make_cause(
                "EXECUTION_LOOP",
                "Detected step loop or repeated operations",
                [],
            ))

        # 5. Check for insufficient info failures
        insufficient = [
            e for e in traces
            if e.get("payload", {}).get("failure_type") == "INSUFFICIENT_INFO"
        ]
        if insufficient:
            causes.append(self._make_cause(
                "INSUFFICIENT_INFO",
                "Agent stopped due to insufficient information",
                insufficient[:3],
            ))

        # 6. Check for timeout
        timeout_events = [
            e for e in traces
            if e.get("event_type") == "task.failed"
            and "超时" in str(e.get("message", ""))
        ]
        if timeout_events:
            causes.append(self._make_cause(
                "TIMEOUT",
                "Task timed out",
                timeout_events[:3],
            ))

        # 7. Check if stuck waiting for approval
        approval_events = [
            e for e in traces
            if e.get("event_type") == "approval.requested"
        ]
        has_resolution = any(
            e.get("event_type") == "approval.resolved" for e in traces
        )
        if approval_events and not has_resolution:
            causes.append(self._make_cause(
                "APPROVAL_BLOCKED",
                "Task is waiting for user approval",
                approval_events[:3],
            ))

        # Determine severity
        critical_categories = {"EXECUTION_LOOP", "POLICY_VIOLATION"}
        severity = (
            "CRITICAL" if any(c["category"] in critical_categories for c in causes)
            else "HIGH" if len(causes) >= 2
            else "MEDIUM" if causes
            else "LOW"
        )

        return {
            "case_id": failed_result.get("case_id", "unknown"),
            "causes": causes,
            "primary_cause": causes[0] if causes else None,
            "cause_count": len(causes),
            "severity": severity,
        }

    def _make_cause(self, category: str, detail: str,
                    evidence: list[dict]) -> dict:
        cat_info = self.CATEGORIES.get(category, {})
        return {
            "category": category,
            "detail": detail,
            "evidence_count": len(evidence),
            "evidence_sample": [
                {"event_type": e.get("event_type", ""),
                 "message": str(e.get("message", ""))[:200]}
                for e in evidence[:3]
            ],
            "suggestion": cat_info.get("suggestion", "Review trace for details"),
        }

    def _detect_loop(self, traces: list[dict]) -> bool:
        """Detect if the trace contains a step/tool loop."""
        # Check for 3+ consecutive identical tool calls
        tool_calls = [
            e.get("payload", {}).get("tool_name", "")
            for e in traces
            if e.get("event_type") == "tool.requested"
        ]
        for i in range(len(tool_calls) - 2):
            if tool_calls[i] == tool_calls[i + 1] == tool_calls[i + 2] and tool_calls[i]:
                return True

        # Check for 5+ steps with same title
        step_titles = [
            e.get("title", "")
            for e in traces
            if e.get("event_type") == "step.started"
        ]
        if len(step_titles) >= 5 and len(set(step_titles)) <= 2:
            return True

        return False
