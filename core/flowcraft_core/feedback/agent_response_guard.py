"""AgentResponseSanitizer — ensures one-way emotional channel.

Users can express frustration freely. Agent responses must always
be professional, empathetic, and constructive. This component filters
Agent output to prevent:
    1. Judging user emotions
    2. Sarcasm / passive aggression
    3. Shifting blame to the user

Phase 2 adds LLM-based second check for ambiguous/complex responses.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from flowcraft_core.models.gateway import ModelGateway

logger = logging.getLogger(__name__)

LLM_CHECK_TIMEOUT = 2.0  # seconds


class AgentResponseSanitizer:
    """Filter for Agent responses in Vent context.

    Phase 1: Rule-based pattern matching (<1ms)
    Phase 2: LLM-based second check for ambiguous responses (~100 tokens)

    Usage:
        sanitizer = AgentResponseSanitizer(model_gateway)
        safe_response = sanitizer.sanitize(agent_raw_response)
        # For ambiguous responses, use:
        is_ok = await sanitizer.llm_check(response)
    """

    def __init__(self, model_gateway: "ModelGateway | None" = None) -> None:
        self._model_gateway = model_gateway

    def set_model_gateway(self, gateway: "ModelGateway") -> None:
        """Inject ModelGateway for LLM-based second check."""
        self._model_gateway = gateway

    # ── Forbidden patterns (Rule-based layer) ─────────────────

    FORBIDDEN_PATTERNS: ClassVar[list[tuple[re.Pattern, str, str]]] = [
        # (pattern, violation_type, replacement)
        #
        # 1. Judging user emotions
        (re.compile(r"(?:你|您)\s*(?:很|非常|太|真|好)\s*(?:生气|愤怒|激动|暴躁|急躁|情绪化)", re.IGNORECASE),
         "judging_emotion",
         "我理解这个结果让你不满意"),
        (re.compile(r"(?:冷静|别急|别生气|消消气|别激动|relax|calm down|chill|don't be)", re.IGNORECASE),
         "judging_emotion",
         "我们先看看具体哪里出了问题"),
        (re.compile(r"(?:你的情绪|你的心情|你现在的状态)", re.IGNORECASE),
         "judging_emotion",
         "我注意到了你的反馈"),
        # 2. Sarcasm / passive aggression
        (re.compile(r"(?:看来|显然|很明显).{0,10}(?:对于.{0,5}(?:你|用户)).{0,10}(?:太|很|有点)(?:难|困难|复杂)", re.IGNORECASE),
         "sarcasm",
         "这个任务确实有一些挑战，让我换一种方式试试"),
        (re.compile(r"(?:其他用户|别人|一般用户).{0,10}(?:都|就|可以|能)", re.IGNORECASE),
         "comparison",
         "这是个值得注意的问题"),
        (re.compile(r"(?:你.{0,5}应该|你.{0,5}本可以|你.{0,5}早该).{0,10}(?:知道|了解|明白|清楚)", re.IGNORECASE),
         "blame_shifting",
         "我可能没有完全理解你的意图，能再说明一下吗？"),
        (re.compile(r"you (?:should|ought to|could) have", re.IGNORECASE),
         "blame_shifting",
         "I may have misunderstood your intent. Could you clarify?"),
        (re.compile(r"(?:这不怪我|不是我的问题|这是.{0,5}的错)", re.IGNORECASE),
         "blame_shifting",
         "这是我的责任，让我重新处理"),
        (re.compile(r"(?:你给的|你的).{0,4}(?:指令|描述|要求|说明).{0,4}(?:不清|不够|模糊|错误|有问题)", re.IGNORECASE),
         "blame_shifting",
         "我可能没有完全理解你的意图，能再说明一下吗？"),
        (re.compile(r"you (?:didn'?t|gave).{0,10}(?:clear|specific|correct|proper)", re.IGNORECASE),
         "blame_shifting",
         "I may have misunderstood your instructions. Let me try again."),
        # 3. Minimizing / patronizing
        (re.compile(r"(?:这没什么|小事|别在意|无所谓|没什么大不了)", re.IGNORECASE),
         "minimizing",
         "我理解这个问题对你造成了困扰"),
        (re.compile(r"you'?re (?:overreacting|being dramatic|too sensitive)", re.IGNORECASE),
         "minimizing",
         "I take your concern seriously."),
    ]

    # ── Vent ending template (must be appended) ───────────────

    VENT_CLOSING_ZH = (
        "我记录了你的反馈。{issue_summary}。"
        "你想让我重新试一次，还是换个方式来做？"
    )
    VENT_CLOSING_EN = (
        "I've recorded your feedback. {issue_summary}. "
        "Would you like me to try again, or try a different approach?"
    )

    # ── Default safe responses by severity ────────────────────

    DEFAULT_RESPONSES: ClassVar[dict[str, dict[str, str]]] = {
        "zh": {
            "light": "我注意到你可能不太满意，方便告诉我具体哪里出了问题吗？",
            "medium": "我理解这个结果让你失望了。下面是大家面对类似情况时的表达方式，你可以选择一条，或者直接告诉我具体哪里不对。",
            "heavy": "我理解你的感受，这确实不应该发生。我已经暂停了当前任务。请告诉我具体哪里出了问题，我会认真记录并改进。",
        },
        "en": {
            "light": "I notice you might not be satisfied. Could you tell me what went wrong?",
            "medium": "I understand this is frustrating. Here are some ways others have expressed similar situations — feel free to pick one or tell me directly.",
            "heavy": "I understand how you feel, and this shouldn't have happened. I've paused the current task. Please tell me what went wrong and I'll make sure it's recorded and addressed.",
        },
    }

    # ── Public API ────────────────────────────────────────────

    def sanitize(self, raw_response: str, lang: str = "zh") -> str:
        """Filter a raw Agent response.

        Checks for forbidden patterns and replaces them with safe alternatives.
        Returns the sanitized text.
        """
        text = raw_response

        for pattern, vtype, replacement in self.FORBIDDEN_PATTERNS:
            if pattern.search(text):
                logger.warning(
                    "AgentResponseSanitizer: blocked pattern type=%s in response",
                    vtype,
                )
                text = pattern.sub(replacement, text)

        return text

    def get_default_response(self, severity_level: str, lang: str = "zh") -> str:
        """Get a safe default response for a given severity level."""
        lang_key = "zh" if lang.startswith("zh") else "en"
        return self.DEFAULT_RESPONSES.get(lang_key, self.DEFAULT_RESPONSES["zh"]).get(
            severity_level, self.DEFAULT_RESPONSES["zh"]["light"]
        )

    def build_closing(self, issue_summary: str, lang: str = "zh") -> str:
        """Build a safe vent closing message."""
        template = self.VENT_CLOSING_ZH if lang.startswith("zh") else self.VENT_CLOSING_EN
        summary = issue_summary or "你的反馈已帮助我们识别改进方向"
        return template.format(issue_summary=summary)

    # ── Phase 2: LLM-based second check ─────────────────────

    async def llm_check(self, response: str) -> bool:
        """LLM-based check for ambiguous/complex responses.

        Only called when:
        - The rule-based check passes (no pattern match)
        - The response is long/complex enough to warrant deeper analysis
        - The response is in a vent context

        Returns True if the response is SAFE, False if it contains
        hidden sarcasm, judgment, or blame-shifting.

        Cost: ~100 tokens per call. Falls back to True (safe) on timeout.
        """
        if not self._model_gateway or not self._model_gateway.is_live():
            return True  # Can't check, assume safe

        # Skip short/trivial responses
        if len(response) < 20:
            return True

        try:
            messages = [
                {"role": "system", "content": (
                    "You are a content safety checker for an AI assistant. "
                    "Check if the assistant's response contains any of: "
                    "1) Judging or labeling the user's emotions "
                    "2) Sarcasm, passive-aggression, or ironic tone "
                    "3) Blame-shifting or implying the user is at fault "
                    "Answer YES if the response is SAFE (none of the above). "
                    "Answer NO if the response contains ANY of the above. "
                    "Respond with only YES or NO."
                )},
                {"role": "user", "content": (
                    f"Assistant response to a frustrated user:\n"
                    f"\"{response}\"\n\n"
                    f"Does this response contain any sarcasm, judgment, "
                    f"or blame-shifting? Answer YES if SAFE, NO if problematic."
                )},
            ]

            result = await asyncio.wait_for(
                self._model_gateway.generate_text(
                    messages[-1]["content"],
                    system=messages[0]["content"],
                    max_tokens=8,
                ),
                timeout=LLM_CHECK_TIMEOUT,
            )

            is_safe = result.strip().upper().startswith("YES")
            if not is_safe:
                logger.warning(
                    "AgentResponseSanitizer LLM check: flagged as UNSAFE"
                )
            return is_safe

        except asyncio.TimeoutError:
            logger.warning("LLM safety check timed out, assuming safe")
        except Exception as exc:
            logger.warning("LLM safety check failed: %s, assuming safe", exc)

        return True  # Fallback: assume safe

    async def sanitize_with_llm(self, raw_response: str, lang: str = "zh") -> str:
        """Full sanitize pipeline: rule-based + LLM second check.

        If rule-based check catches anything, replace immediately.
        If rule-based passes but LLM check flags it, rewrite using
        a safe template.
        """
        # Step 1: Rule-based check
        text = self.sanitize(raw_response, lang)

        # Step 2: LLM second check (only if rule-based found nothing)
        if text == raw_response and len(raw_response) > 20:
            is_safe = await self.llm_check(text)
            if not is_safe:
                logger.info("LLM check flagged response, rewriting with safe template")
                text = self.get_default_response("medium", lang)

        return text

    def is_safe(self, response: str) -> bool:
        """Check if a response passes all rule-based checks."""
        for pattern, _, _ in self.FORBIDDEN_PATTERNS:
            if pattern.search(response):
                return False
        return True
