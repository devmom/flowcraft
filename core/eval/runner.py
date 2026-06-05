"""Evaluation case runner - executes test cases against FlowCraft app."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from flowcraft_core.domain.schemas import AgentRequest

logger = logging.getLogger(__name__)


class CaseRunner:
    """Executes a single evaluation test case and captures results."""

    def __init__(self, app, events):
        self.app = app
        self.events = events

    def run(self, case: dict) -> dict[str, Any]:
        """Execute one evaluation case synchronously.

        Args:
            case: Evaluation case dict with input, expected, evaluation_config.

        Returns:
            Dict with case_id, traces, final_output, task status, and metadata.
        """
        case_id = case.get("case_id", "unknown")
        logger.info("Running case: %s", case_id)

        t0 = time.monotonic()

        try:
            # Create and execute the task
            request = AgentRequest(
                session_id=f"eval_{case_id}",
                raw_input=case.get("input", {}).get("raw_input", ""),
                source="eval_engine",
                metadata={"case_id": case_id},
            )

            task = asyncio.run(
                self.app.runtime.create_task_async(request)
            )

            # Wait for task completion (polling)
            max_wait = 120  # seconds
            poll_interval = 1.0
            elapsed = 0.0
            final_status = task.status

            while elapsed < max_wait:
                time.sleep(poll_interval)
                elapsed += poll_interval
                row = self.app.task_store.get_task_row(task.task_id)
                if row:
                    status = row.get("status", "")
                    if status in ("COMPLETED", "FAILED", "CANCELLED"):
                        final_status = status
                        break

            # Collect traces
            traces = self.events.list_for_task(task.task_id)

            # Extract final output
            final_output = self._extract_final_output(traces)

            duration_ms = int((time.monotonic() - t0) * 1000)

            # Collect token usage and step counts
            model_calls = sum(
                1 for e in traces
                if e.get("event_type") == "model.requested"
            )
            tool_calls = sum(
                1 for e in traces
                if e.get("event_type") == "tool.requested"
            )

            total_tokens = 0
            for e in traces:
                payload = e.get("payload", {})
                if isinstance(payload, dict):
                    total_tokens += payload.get("tokens", 0)
                    total_tokens += payload.get("input_tokens", 0)
                    total_tokens += payload.get("output_tokens", 0)

            return {
                "case_id": case_id,
                "description": case.get("description", ""),
                "category": case.get("category", ""),
                "difficulty": case.get("difficulty", ""),
                "task_id": task.task_id,
                "status": str(final_status),
                "traces": traces,
                "final_output": final_output,
                "duration_ms": duration_ms,
                "model_calls": model_calls,
                "tool_calls": tool_calls,
                "total_tokens": total_tokens,
                "trace_count": len(traces),
            }

        except Exception as exc:
            logger.exception("Case %s execution failed: %s", case_id, exc)
            return {
                "case_id": case_id,
                "description": case.get("description", ""),
                "category": case.get("category", ""),
                "difficulty": case.get("difficulty", ""),
                "status": "EXECUTION_ERROR",
                "traces": [],
                "final_output": "",
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "model_calls": 0,
                "tool_calls": 0,
                "total_tokens": 0,
                "trace_count": 0,
                "error": str(exc),
            }

    def _extract_final_output(self, traces: list[dict]) -> str:
        """Extract the agent's final answer from trace events."""
        # Try task.completed event first
        for e in reversed(traces):
            if e.get("event_type") == "task.completed":
                msg = e.get("message", "")
                payload = e.get("payload", {})
                if isinstance(payload, dict):
                    answer = payload.get("final_answer", "")
                    if answer:
                        return answer
                    output = payload.get("output", "")
                    if output:
                        return output
                if msg:
                    return msg

        # Try step.completed events
        outputs = []
        for e in traces:
            if e.get("event_type") == "step.completed":
                msg = e.get("message", "")
                payload = e.get("payload", {})
                if isinstance(payload, dict):
                    out = payload.get("output", payload.get("answer", ""))
                    if out:
                        outputs.append(out)
                elif msg:
                    outputs.append(msg)

        if outputs:
            return "\n\n".join(outputs)

        # Fallback: concatenate all answer-like messages
        all_outputs = []
        for e in traces:
            if e.get("event_type") in ("step.completed", "task.completed"):
                msg = e.get("message", "")
                if msg and len(msg) > 20:
                    all_outputs.append(msg)

        return "\n\n".join(all_outputs)
