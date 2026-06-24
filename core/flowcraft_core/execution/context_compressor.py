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
    Dynamic — derived from model's context_window × 80%.
    DeepSeek V4 Pro (1M ctx) → ~800K tokens → ~2M chars budget
    Fallback when model unknown: 32,000 chars
"""

from __future__ import annotations

import json as _json
import logging
from dataclasses import dataclass, field
from typing import Any

from flowcraft_core.domain.schemas import ToolObservation

logger = logging.getLogger(__name__)

# Per-layer defaults — these are floors, actual budget scales with model
MIN_CONTEXT_CHARS = 8000          # absolute minimum for any model
FALLBACK_CONTEXT_CHARS = 32000    # used when model info unavailable
MAX_OBSERVATION_DETAIL = 2000     # per-observation detail cap (unchanged)


@dataclass
class CompressedContext:
    """Result of context compression."""
    context_text: str
    total_chars: int
    recent_count: int
    compressed_count: int
    summary: str


class ContextCompressor:
    """Compresses multi-turn agent context to fit within model context window.

    Budget is auto-detected from the model gateway when available.
    Layers are allocated as ratios of the total budget:

        Recent (last 2-3 observations): up to 50% of budget
        Middle (4-5 back): up to 20% of budget
        Summary (older + step history): up to 30% of budget
    """

    def __init__(self, max_chars: int | None = None, model_gateway: Any | None = None) -> None:
        self.max_chars = max_chars or self._resolve_budget(model_gateway)
        self._step_summaries: dict[str, list[str]] = {}  # task_id -> list of step summaries

    @staticmethod
    def _resolve_budget(model_gateway: Any | None) -> int:
        """Resolve context budget from model gateway, with fallback."""
        if model_gateway is None:
            return FALLBACK_CONTEXT_CHARS
        try:
            from flowcraft_core.memory.context_summarizer import get_context_budget
            return get_context_budget(model_gateway)
        except Exception:
            return FALLBACK_CONTEXT_CHARS

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
        # 保留完整步骤输出，不做硬截断。由 smart_truncate 在预算溢出时智能压缩。
        summary = step_output.replace("\n", " ").strip()
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
