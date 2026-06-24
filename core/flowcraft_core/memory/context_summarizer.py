"""Context Summarizer — LLM-powered hierarchical memory compression.

Three-tier compression strategy:
    Layer 1 (Recent, 0-10 turns): Keep verbatim — most relevant to current task
    Layer 2 (Mid, 10-50 turns): Summarize — compress into "mid-term summary"
    Layer 3 (Long, 50+ turns): Compress further — extract only key decisions/facts

Analogy: Company meeting notes —
    Today: full transcript
    This month: summary of key points
    Last year: only the decisions that still matter

Combined approach: Sliding Window (length control) + Summarization (before dropping).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Budget settings — now dynamic, keyed off model context window.
# Static fallback used only when model gateway unavailable.
FALLBACK_CONTEXT_CHARS = 32000  # generous default for unknown models
CONTEXT_WINDOW_USAGE_RATIO = 0.80  # reserve 20% for LLM output

# Priority budgets within total context window (as ratios of total)
CRITICAL_RATIO = 0.30   # task goal + current step = 30% of budget
HIGH_RATIO = 0.50       # + recent observations = cumulative 50%
MEDIUM_RATIO = 0.75     # + session memory = cumulative 75%
# Remaining 25% for older / long-term content


def get_context_budget(model_gateway: Any | None = None) -> int:
    """Compute context budget in characters, based on the active model's context window.

    Uses the model's advertised context_window (tokens) × 80% to leave
    room for the LLM's own output, then converts tokens → characters
    using a conservative ~2.5 chars/token for Chinese+English mixed text.

    Falls back to FALLBACK_CONTEXT_CHARS when model info is unavailable.
    """
    if model_gateway is None:
        return FALLBACK_CONTEXT_CHARS

    # Try to read from adapter profile
    context_window_tokens = _get_model_context_window(model_gateway)
    if context_window_tokens is None:
        return FALLBACK_CONTEXT_CHARS

    usable_tokens = int(context_window_tokens * CONTEXT_WINDOW_USAGE_RATIO)
    # Tokens → characters: ~2.5 for CJK-majority, ~4 for English-majority.
    # Use 2.5 as conservative (fewer chars) to stay safe.
    budget_chars = usable_tokens * 2.5

    # Cap at a sane max so we don't accidentally try to stuff
    # 2 million chars into a prompt.  384K chars ≈ 154K tokens is
    # the practical upper bound for most agent contexts.
    return min(int(budget_chars), 384_000)


def _get_model_context_window(model_gateway: Any) -> int | None:
    """Extract context window size from the active model profile."""
    # Try _profile (Pydantic ModelProfile)
    profile = getattr(model_gateway, '_profile', None)
    if profile and hasattr(profile, 'context_window'):
        return profile.context_window
    # Try _adapter.profile
    adapter = getattr(model_gateway, '_adapter', None)
    if adapter:
        profile = getattr(adapter, 'profile', None)
        if profile and hasattr(profile, 'context_window'):
            return profile.context_window
    return None

# Content markers
MARKER_TASK_START = "## 原始任务"
MARKER_CURRENT_STEP = "## Current Step"
MARKER_RECENT_OBS = "## Tool Results"
MARKER_SESSION = "## 会话历史记忆"
MARKER_PRIOR_STEPS = "## 已完成步骤的输出"


def smart_truncate(
    context: str,
    max_chars: int | None = None,
    model_gateway: Any | None = None,
) -> str:
    """Intelligently truncate context using priority-ordered budget filling.

    Budget is determined (in order of precedence):
        1. Explicit max_chars argument
        2. Model's context_window × 80% from model_gateway
        3. FALLBACK_CONTEXT_CHARS (32,000)

    Strategy:
        - If under budget, return as-is
        - Fill by priority: CRITICAL → HIGH → MEDIUM → LOW
        - CRITICAL (task goal, current step): always keep full
        - HIGH (recent observations): keep full, truncate per-observation if needed
        - MEDIUM (session history, prior step outputs): compress, keep titles
        - LOW (older content): only include if budget allows
    """
    if max_chars is None:
        max_chars = get_context_budget(model_gateway)

    if len(context) <= max_chars:
        return context

    sections = _split_sections(context)

    # Priority-ordered sections
    critical: list[str] = []
    high: list[str] = []
    medium: list[str] = []
    low: list[str] = []

    for title, content in sections:
        if MARKER_TASK_START in title or MARKER_CURRENT_STEP in title:
            critical.append(f"{title}\n{content}")
        elif MARKER_RECENT_OBS in title:
            high.append(f"{title}\n{content}")
        elif MARKER_SESSION in title or MARKER_PRIOR_STEPS in title:
            medium.append(f"{title}\n{content}")
        else:
            low.append(f"{title}\n{content}")

    # Assemble in priority order
    result_parts: list[str] = []
    budget = max_chars

    # CRITICAL: always include fully (up to budget)
    for part in critical:
        if len(part) <= budget:
            result_parts.append(part)
            budget -= len(part)
        else:
            result_parts.append(part[:budget])
            budget = 0
            break

    # HIGH: include fully if possible, otherwise truncate individual observations
    for part in high:
        if budget <= 200:
            break
        if len(part) <= budget:
            result_parts.append(part)
            budget -= len(part)
        else:
            truncated = _truncate_observations(part, budget - 100)
            result_parts.append(truncated)
            budget = 100

    # MEDIUM: compress by keeping only titles / first lines
    for part in medium:
        if budget <= 100:
            break
        if len(part) <= budget:
            result_parts.append(part)
            budget -= len(part)
        else:
            compressed = _compress_session_memories(part, budget - 100)
            result_parts.append(compressed)
            budget = 100

    # LOW: only included if significant budget remains
    for part in low:
        if budget <= 200:
            break
        if len(part) <= budget:
            result_parts.append(part)
            budget -= len(part)
        else:
            result_parts.append(part[:budget - 50])
            budget = 50
            break

    # Add truncation notice
    original_len = len(context)
    removed = original_len - sum(len(p) for p in result_parts) - budget
    if removed > 0:
        notice = (
            f"\n\n---\n"
            f"[上下文已智能压缩: 原始 {original_len} 字符 → {max_chars - budget} 字符, "
            f"移除 {removed} 字符的旧历史/重复内容]"
        )
        if budget >= len(notice):
            result_parts.append(notice)

    return "\n\n".join(result_parts)


async def llm_summarize_context(
    context: str,
    model_gateway: Any,  # ModelGateway
    task_objective: str,
    max_chars: int | None = None,
) -> str:
    """Use LLM to intelligently summarize overflowing context.

    Budget auto-detected from model_gateway's context window if not explicit.
    Falls back to smart_truncate if LLM is unavailable.
    """
    if max_chars is None:
        max_chars = get_context_budget(model_gateway)

    if len(context) <= max_chars:
        return context

    if not model_gateway or not model_gateway.is_live():
        logger.info("LLM unavailable for summarization, using rule-based truncation")
        return smart_truncate(context, max_chars)

    try:
        # Split context into what to preserve vs. what to summarize
        keep, summarize = _split_for_summarization(context, max_chars // 2)

        if not summarize.strip():
            return smart_truncate(context, max_chars)

        prompt = (
            f"## Task Goal\n{task_objective}\n\n"
            f"## Content to Summarize\n"
            f"The following historical context needs to be condensed to fit within the model's context window.\n"
            f"Extract ONLY the key facts, decisions, and results that are relevant to the task goal above.\n"
            f"Omit redundant information, process descriptions, and irrelevant details.\n"
            f"Target: reduce to ~1/3 of original length.\n\n"
            f"### Historical Context\n{summarize}\n\n"
            f"## Instructions\n"
            f"Output a concise bullet-point summary. Each bullet should be one line. "
            f"Focus on: concrete data, user preferences, tool results, decisions made. "
            f"DO NOT include reasoning about why you chose these points."
        )

        messages = [
            {"role": "system", "content": "You are a context summarizer. Condense historical context into key bullet points."},
            {"role": "user", "content": prompt},
        ]

        # 10 second timeout for summarization
        import asyncio
        summary = await asyncio.wait_for(
            model_gateway._adapter.chat(messages, temperature=0.1, max_tokens=1024),
            timeout=10.0,
        )

        result = (
            f"{keep}\n\n"
            f"## 历史上下文摘要 (LLM压缩)\n{summary.strip()}"
        )

        if len(result) > max_chars:
            result = smart_truncate(result, max_chars)

        logger.info("LLM context summarization: %d → %d chars", len(context), len(result))
        return result

    except Exception as exc:
        logger.warning("LLM summarization failed: %s, using rule-based truncation", exc)
        return smart_truncate(context, max_chars)


# ── Internal Helpers ────────────────────────────────────────


def _split_sections(context: str) -> list[tuple[str, str]]:
    """Split context text into titled sections."""
    sections: list[tuple[str, str]] = []
    lines = context.split("\n")
    current_title = ""
    current_lines: list[str] = []

    for line in lines:
        if line.startswith("## ") and current_title:
            sections.append((current_title, "\n".join(current_lines).strip()))
            current_title = line
            current_lines = []
        elif line.startswith("## "):
            current_title = line
            current_lines = []
        else:
            current_lines.append(line)

    if current_title or current_lines:
        sections.append((current_title, "\n".join(current_lines).strip()))

    return sections


def _truncate_observations(obs_section: str, budget: int) -> str:
    """Truncate tool observation details while keeping status and summaries."""
    lines = obs_section.split("\n")
    result: list[str] = []
    for line in lines:
        if len("\n".join(result)) + len(line) > budget:
            result.append(f"... ({len(lines) - len(result)} more lines truncated)")
            break
        result.append(line)
    return "\n".join(result)


def _compress_session_memories(session_section: str, budget: int) -> str:
    """Compress session memory by extracting only titles and key phrases."""
    lines = session_section.split("\n")
    result: list[str] = []
    result.append("## 会话记忆 (压缩)")
    for line in lines:
        if line.startswith("- **") or line.startswith("##"):
            if len("\n".join(result)) + len(line) > budget:
                break
            result.append(line[:200])
    return "\n".join(result)


def _split_for_summarization(context: str, keep_budget: int) -> tuple[str, str]:
    """Split context into 'keep as-is' and 'needs summarization' portions."""
    sections = _split_sections(context)

    keep_parts: list[str] = []
    summarize_parts: list[str] = []
    kept_chars = 0

    for title, content in sections:
        section_text = f"{title}\n{content}"
        is_critical = (
            MARKER_TASK_START in title or
            MARKER_CURRENT_STEP in title
        )
        is_recent = MARKER_RECENT_OBS in title

        if is_critical or (is_recent and kept_chars < keep_budget):
            keep_parts.append(section_text)
            kept_chars += len(section_text)
        else:
            summarize_parts.append(section_text)

    return "\n\n".join(keep_parts), "\n\n".join(summarize_parts)


# ── Hierarchical Memory Compressor ────────────────────────────

@dataclass
class CompressionLevel:
    """Configuration for one compression tier."""
    name: str              # "recent", "mid", "long"
    turn_range: tuple[int, int | None]  # (start, end) turns, None = unbounded
    strategy: str          # "keep" | "summarize" | "compress"
    target_ratio: float    # Target compression ratio (0.0-1.0, 1.0 = keep all)


DEFAULT_LEVELS = [
    CompressionLevel("recent",  (0, 10),  "keep",      1.0),
    CompressionLevel("mid",     (10, 50), "summarize", 0.3),
    CompressionLevel("long",    (50, None), "compress", 0.1),
]


class HierarchicalMemoryCompressor:
    """Hierarchical (tiered) context compression.

    Split conversation history into tiers by age, apply different
    compression strategies to each tier. Keeps recent context verbatim
    while progressively condensing older information.

    Usage:
        compressor = HierarchicalMemoryCompressor(llm=model_gateway)
        compressed = await compressor.compress(messages, task_objective)
    """

    def __init__(
        self,
        llm: Any = None,  # ModelGateway for LLM-based compression
        levels: list[CompressionLevel] | None = None,
        total_budget: int | None = None,
    ):
        self.llm = llm
        self.levels = levels or DEFAULT_LEVELS
        self.total_budget = total_budget

    async def compress(
        self,
        messages: list[dict],
        task_objective: str = "",
    ) -> list[dict]:
        """Compress conversation messages using hierarchical tiers.

        Args:
            messages: Full conversation history [{role, content}, ...]
            task_objective: Current task goal (preserved in compression)

        Returns:
            Compressed messages suitable for LLM context window.
        """
        if not messages:
            return []

        # Split into tiers by turn index
        tiers = self._split_into_tiers(messages)

        result: list[dict] = []
        for level, tier_msgs in zip(self.levels, tiers):
            if not tier_msgs:
                continue

            if level.strategy == "keep":
                result.extend(tier_msgs)
            elif level.strategy == "summarize":
                summary = await self._summarize_tier(tier_msgs, level, task_objective)
                if summary:
                    result.append({"role": "system", "content": f"[{level.name} summary] {summary}"})
            elif level.strategy == "compress":
                compressed = await self._compress_tier(tier_msgs, level, task_objective)
                if compressed:
                    result.append({"role": "system", "content": f"[{level.name} key points] {compressed}"})

        # If total budget specified, apply length cap
        if self.total_budget:
            result = self._apply_budget(result, self.total_budget)

        return result

    @staticmethod
    def compress_sync(
        messages: list[dict],
        task_objective: str = "",
        total_budget: int | None = None,
    ) -> list[dict]:
        """Synchronous fallback: rule-based tiered compression (no LLM required).

        Uses simple truncation per tier instead of LLM summarization.
        """
        tiers = HierarchicalMemoryCompressor._split_into_tiers_static(messages)
        tier_configs = [
            ("recent",  1.0),   # keep all
            ("mid",     0.3),   # keep 30%
            ("long",    0.1),   # keep 10%
        ]

        result: list[dict] = []
        for (name, ratio), tier_msgs in zip(tier_configs, tiers):
            if not tier_msgs:
                continue
            keep_count = max(1, int(len(tier_msgs) * ratio))
            if name == "recent":
                result.extend(tier_msgs)
            else:
                kept = tier_msgs[-keep_count:]  # Keep most recent within tier
                if kept:
                    # Add a summary marker
                    marker = f"[{name} tier: showing {len(kept)}/{len(tier_msgs)} messages, {int((1-ratio)*100)}% compressed]"
                    result.append({"role": "system", "content": marker})
                    result.extend(kept)

        # Apply budget if specified
        if total_budget:
            result = HierarchicalMemoryCompressor._apply_budget_static(result, total_budget)

        return result

    # ── Internal ──────────────────────────────────────────

    @staticmethod
    def _split_into_tiers(messages: list[dict]) -> list[list[dict]]:
        """Split messages into tiers by turn count."""
        tiers: list[list[dict]] = []
        msg_idx = len(messages) - 1  # Count from newest

        for level in DEFAULT_LEVELS:
            start, end = level.turn_range
            tier: list[dict] = []
            if end is None:
                # Last tier: take all remaining
                for i, msg in enumerate(reversed(messages)):
                    turn_num = i
                    if turn_num >= start:
                        tier.append(msg)
                tier.reverse()  # Restore chronological order
                tiers.append(tier)
            else:
                tier_size = end - start
                for i, msg in enumerate(reversed(messages)):
                    turn_num = i
                    if start <= turn_num < end:
                        tier.append(msg)
                tier.reverse()
                tiers.append(tier)
        return tiers

    @staticmethod
    def _split_into_tiers_static(messages: list[dict]) -> list[list[dict]]:
        """Split messages into 3 tiers: recent(10), mid(40), long(rest)."""
        total = len(messages)
        return [
            messages[-10:] if total >= 10 else messages,                     # recent
            messages[-50:-10] if total > 10 else [],                         # mid
            messages[:-50] if total > 50 else [],                            # long
        ]

    async def _summarize_tier(
        self, messages: list[dict], level: CompressionLevel, task: str
    ) -> str:
        """Summarize a tier using LLM (mid-term compression, ~30% retention)."""
        if not self.llm or not getattr(self.llm, 'is_live', lambda: False)():
            return self._rule_summarize(messages, level)

        text = "\n".join(
            f"[{m.get('role', '?')}]: {str(m.get('content', ''))[:300]}"
            for m in messages
        )
        prompt = (
            f"Task context: {task}\n\n"
            f"Summarize the following conversation segment ({level.name} tier). "
            f"Extract only: key decisions, important facts, user preferences, and task-relevant data. "
            f"Omit: small talk, process details, repeated information.\n\n"
            f"Target: ~{int(len(text) * level.target_ratio)} characters.\n\n"
            f"---\n{text}\n---\n\n"
            f"Bullet-point summary:"
        )
        try:
            result = await self.llm._adapter.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=512,
            )
            return result.strip()
        except Exception as exc:
            logger.debug("LLM tier summarization failed: %s", exc)
            return self._rule_summarize(messages, level)

    async def _compress_tier(
        self, messages: list[dict], level: CompressionLevel, task: str
    ) -> str:
        """Highly compress a tier (~10% retention) — long-term memory."""
        if not self.llm or not getattr(self.llm, 'is_live', lambda: False)():
            return self._rule_compress(messages, level)

        text = "\n".join(
            f"[{m.get('role', '?')}]: {str(m.get('content', ''))[:200]}"
            for m in messages
        )
        prompt = (
            f"Task context: {task}\n\n"
            f"Extract ONLY the most critical decisions and facts from this older conversation. "
            f"Format: 3-5 bullet points of essential information. "
            f"Omit everything that is no longer relevant.\n\n"
            f"---\n{text}\n---\n\n"
            f"Key decisions/facts:"
        )
        try:
            result = await self.llm._adapter.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=256,
            )
            return result.strip()
        except Exception as exc:
            logger.debug("LLM tier compression failed: %s", exc)
            return self._rule_compress(messages, level)

    @staticmethod
    def _rule_summarize(messages: list[dict], level: CompressionLevel) -> str:
        """Rule-based summarization: keep first line of each message."""
        lines = []
        for m in messages:
            content = str(m.get("content", ""))
            first_line = content.split("\n")[0][:200]
            role = m.get("role", "?")
            lines.append(f"[{role}] {first_line}")
        target_len = max(100, int(len("\n".join(lines)) * level.target_ratio))
        result = "\n".join(lines)
        return result[:target_len] + ("..." if len(result) > target_len else "")

    @staticmethod
    def _rule_compress(messages: list[dict], level: CompressionLevel) -> str:
        """Rule-based compression: extract keywords from oldest tier."""
        import re
        all_text = " ".join(str(m.get("content", "")) for m in messages)
        # Simple keyword extraction: capitalized words, quoted phrases, decisions
        keywords = set()
        for word in re.findall(r'\b[A-Z一-鿿]{2,}\b', all_text):
            if len(word) >= 2:
                keywords.add(word)
        phrases = re.findall(r'["""]([^"""]+)["”]', all_text)
        items = list(keywords)[:10] + list(phrases)[:3]
        return "Key terms: " + ", ".join(items[:15]) if items else "(no key terms extracted)"

    @staticmethod
    def _apply_budget(messages: list[dict], budget: int) -> list[dict]:
        """Truncate messages to fit within character budget."""
        result = []
        used = 0
        for msg in messages:
            content = str(msg.get("content", ""))
            msg_len = len(content)
            if used + msg_len <= budget:
                result.append(msg)
                used += msg_len
            else:
                remaining = budget - used
                if remaining > 100:
                    result.append({
                        "role": msg.get("role", "system"),
                        "content": content[:remaining] + "...[truncated]",
                    })
                break
        return result

    @staticmethod
    def _apply_budget_static(messages: list[dict], budget: int) -> list[dict]:
        """Static version of budget application."""
        return HierarchicalMemoryCompressor._apply_budget(messages, budget)
