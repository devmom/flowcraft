"""Context Compressor — manage LLM context window for multi-turn agent loops.

Problems solved:
    1. Multi-turn tool calls accumulate observation results, eventually exceeding context window
    2. Historical step outputs become irrelevant but still occupy context space
    3. Large tool results (e.g., 12KB file content) dominate the prompt

Strategy:
    ┌─────────────────────────────────────────────────────────┐
    │  Layer 1: Recent (last 2 observations) → full detail     │
    │  Layer 2: Middle (3-5 back) → truncated to 500 chars     │
    │  Layer 3: Old (6+ back) → compressed summary only        │
    │  Layer 4: Historical steps → progressive running summary  │
    └─────────────────────────────────────────────────────────┘

Budget:
    Max context budget: 8000 chars (safe for most models)
    Recent layer: up to 4000 chars
    Middle layer: up to 1500 chars
    Summary layer: up to 2500 chars
"""

from __future__ import annotations

import json as _json
import logging
from dataclasses import dataclass, field
from typing import Any

from flowcraft_core.domain.schemas import ToolObservation

logger = logging.getLogger(__name__)

# Context budget limits
MAX_CONTEXT_CHARS = 8000
MAX_RECENT_DETAIL_CHARS = 4000
MAX_MIDDLE_CHARS = 1500
MAX_OBSERVATION_DETAIL = 2000  # per-observation detail cap


@dataclass
class CompressedContext:
    """Result of context compression."""
    context_text: str
    total_chars: int
    recent_count: int
    compressed_count: int
    summary: str


class ContextCompressor:
    """Compresses multi-turn agent context to fit within model context window."""

    def __init__(self, max_chars: int = MAX_CONTEXT_CHARS) -> None:
        self.max_chars = max_chars
        self._step_summaries: dict[str, list[str]] = {}  # task_id -> list of step summaries

    def compress(
        self,
        task_id: str,
        step_objective: str,
        observations: list[ToolObservation],
        previous_step_outputs: list[str] | None = None,
    ) -> CompressedContext:
        """Compress tool observations and step history into a bounded context string.

        Args:
            task_id: current task ID (for tracking step summaries)
            step_objective: current step's objective
            observations: all tool observations so far (ordered oldest first)
            previous_step_outputs: outputs from previously completed steps

        Returns:
            CompressedContext with the compressed text and metadata
        """
        parts: list[str] = []

        # Layer 4: Historical step summary (if we have previous outputs)
        if previous_step_outputs and len(previous_step_outputs) > 0:
            self._add_step_summary(task_id, previous_step_outputs[-1])
            history = self._get_step_summaries(task_id)
            if history:
                parts.append("## Completed Steps Summary\n" + history)

        # Layer 1-3: Compress observations
        if observations:
            obs_text = self._compress_observations(observations)
            parts.append(obs_text)

        # Current step
        parts.append(f"\n## Current Step\n{step_objective}")

        context = "\n".join(parts)
        total = len(context)

        # Hard truncation as safety net
        if total > self.max_chars:
            context = context[:self.max_chars] + "\n\n[Context truncated to fit model window]"
            total = self.max_chars

        return CompressedContext(
            context_text=context,
            total_chars=total,
            recent_count=min(2, len(observations)),
            compressed_count=max(0, len(observations) - 2),
            summary=self._get_step_summaries(task_id),
        )

    def _compress_observations(self, observations: list[ToolObservation]) -> str:
        """Layer 1-2-3 compression of tool observations."""
        if not observations:
            return ""

        n = len(observations)
        parts: list[str] = []
        parts.append("## Tool Results")

        # Layer 1: Last 2 observations → full detail (capped per observation)
        recent_start = max(0, n - 2)
        for i in range(recent_start, n):
            obs = observations[i]
            detail = self._format_observation(obs, max_chars=MAX_OBSERVATION_DETAIL)
            parts.append(f"### Result {i + 1} [{obs.status}]\n{detail}")

        # Layer 2: Observations 3-5 back → short format
        middle_start = max(0, n - 5)
        middle_end = recent_start
        if middle_start < middle_end:
            parts.append("### Earlier Results (summarized)")
            for i in range(middle_start, middle_end):
                obs = observations[i]
                parts.append(f"- [{obs.status}] {obs.output_summary[:200]}")

        # Layer 3: Observations 6+ → aggregated summary only
        if middle_start > 0:
            parts.append(f"- ... ({middle_start} earlier observations omitted)")

        return "\n".join(parts)

    @staticmethod
    def _format_observation(obs: ToolObservation, max_chars: int = 2000) -> str:
        """Format a single observation, truncating large payloads."""
        payload = obs.output_payload
        payload_str = _json.dumps(payload, ensure_ascii=False, default=str)

        if len(payload_str) > max_chars:
            # Try to keep the most useful content
            content_keys = ["content", "text", "body", "output", "stdout", "result"]
            for key in content_keys:
                if key in payload and isinstance(payload[key], str):
                    content = payload[key]
                    if len(content) > max_chars:
                        return content[:max_chars] + f"\n...[{len(content)} total chars]"
                    return content

            # Fallback: truncate JSON
            return payload_str[:max_chars] + f"\n...[{len(payload_str)} total chars]"

        return payload_str

    def _add_step_summary(self, task_id: str, step_output: str) -> None:
        """Add a completed step's output to the progressive summary."""
        if task_id not in self._step_summaries:
            self._step_summaries[task_id] = []
        # Keep a short summary of each step
        summary = step_output[:300].replace("\n", " ").strip()
        if summary:
            self._step_summaries[task_id].append(summary)
        # Keep only last 10 step summaries
        if len(self._step_summaries[task_id]) > 10:
            self._step_summaries[task_id] = self._step_summaries[task_id][-10:]

    def _get_step_summaries(self, task_id: str) -> str:
        """Get compressed history of completed steps."""
        summaries = self._step_summaries.get(task_id, [])
        if not summaries:
            return ""
        lines = []
        for i, s in enumerate(summaries):
            lines.append(f"- Step {i + 1}: {s}")
        return "\n".join(lines)

    def clear(self, task_id: str) -> None:
        """Clear cached summaries for a task."""
        self._step_summaries.pop(task_id, None)

    @staticmethod
    def estimate_token_count(text: str) -> int:
        """Rough token count estimation: ~4 chars per token for English, ~2 for Chinese."""
        # Simple heuristic: average 3 chars per token
        return max(1, len(text) // 3)
