"""FrustrationDetector — multi-level sentiment detection for user frustration.

Implements a four-level funnel filter to minimize LLM call overhead:
    Filter 0: Cooldown check (skip if recent vent in this session)
    Filter 1: Message type pre-check (skip code blocks, short confirms)
    Filter 2: Keyword + pattern matching (regex word lists, <1ms)
    Filter 3: LLM-based classification (refines Filter 2 results, deferred)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Pattern

if TYPE_CHECKING:
    from flowcraft_core.models.gateway import ModelGateway

logger = logging.getLogger(__name__)

COOLDOWN_TURNS = 5
LLM_FILTER_TIMEOUT = 3.0  # seconds — Filter 3 timeout, falls back to Filter 2 result

# ── Filter 1 patterns ────────────────────────────────────────

SHORT_CONFIRM_RE = re.compile(
    r"^(?:ok|好的|好|嗯|对|是|行|可以|继续|go on|yes|no|yep|nope|right|correct|"
    r"thanks|谢|谢谢|thank you|got it|明白了|知道了|收到|了解|嗯嗯|还行|差不多|"
    r"试试|来吧|go ahead|sure|fine|alright|cool|nice|great|perfect|excellent|"
    r"不确定|不知道|随便|都可以)$",
    re.IGNORECASE,
)

CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```", re.DOTALL)
HEAVY_CODE_RE = re.compile(
    r"^\s*(?:import |from |def |class |const |let |var |function "
    r"|#include |SELECT |INSERT |UPDATE |DELETE )",
    re.MULTILINE,
)
FILE_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|/[\w.-]+/|\.\./)[\w.\\/-]+")

# ── Filter 2: Chinese patterns ───────────────────────────────

CN_FRUSTRATION_PATTERNS: list[tuple[Pattern, int]] = [
    # Heavy (score 4)
    (re.compile(r"你(?:他妈|踏马|tm|tmd|卧槽|wocao|艹|操|fuck|shit)"), 4),
    (re.compile(r"你[怎么为啥][又还老总]"), 3),
    (re.compile(r"你[是真就].{0,3}(?:没用|废物|垃圾|不行|差劲|烂)"), 4),
    (re.compile(r"(?:妈的|操|卧槽|艹|尼玛|草).*(?:你|这个)"), 4),
    (re.compile(r"(?:服了|无语|崩溃|受不了|想打人|心态炸了|气死)"), 3),
    (re.compile(r"(?:别|不要|禁止|停止).{0,4}(?:说了|做了|搞了|瞎搞|乱来)"), 3),
    # Medium (score 2-3)
    (re.compile(r"你.{0,4}(?:到底|究竟|确定|真的).{0,4}(?:懂|理解|明白|知道)"), 2),
    (re.compile(r"你.{0,4}(?:又|还是|仍然|老是|总是).{0,4}(?:错|不对|不行)"), 3),
    (re.compile(r"(?:你怎么|你为什么).{0,10}(?:这样|那样|回事)"), 2),
    (re.compile(r"你.{0,2}(?:搞笑|逗|玩|开玩笑).{0,2}(?:呢|吧|吗)"), 2),
    (re.compile(r"(?:能不能|可不可以|会不会).{0,4}(?:认真|仔细|正经)"), 2),
    (re.compile(r"(?:又说|还说|总是|老是).{0,4}(?:一样|重复|同样)"), 2),
    (re.compile(r"你.{0,3}(?:听不懂|不理解|搞不清|不知道)"), 2),
    # Light (score 1-2)
    (re.compile(r"(?:不太对|不对哦|不对吧|不对劲|有问题|奇怪|反了)"), 1),
    (re.compile(r"(?:不是.{0,4}意思|我说.{0,2}不是|没让你|不是让你)"), 1),
    (re.compile(r"你.{0,4}(?:理解.{0,2}(?:错|歪|偏)|会错意)"), 2),
    (re.compile(r"(?:这不是|那不对|搞错了|弄错了|理解错了)"), 1),
    # Passive-aggressive ("阴阳怪气") — score 2-3
    (re.compile(r"你.{0,2}(?:可真行|真厉害|真有你的|真棒).{0,2}(?:哦|啊|哈|呢)"), 2),
    (re.compile(r"(?:您.{0,4}可.{0,4}真.{0,4}(?:行|棒|厉害|优秀))"), 3),
    (re.compile(r"(?:我.{0,2}服了.{0,2}(?:你|您))"), 2),
    (re.compile(r"(?:你.{0,2}开心就好|你.{0,2}说.{0,2}都对)"), 2),
    (re.compile(r"(?:给你.{0,3}鼓个掌|为你.{0,3}鼓掌|太.{0,2}精彩了)"), 2),
]

# ── Filter 2: English patterns ───────────────────────────────

EN_FRUSTRATION_PATTERNS: list[tuple[Pattern, int]] = [
    # Heavy
    (re.compile(r"you (?:are|'re) (?:so |really |fucking |totally )?(?:useless|stupid|dumb|broken|garbage|trash|terrible|awful|wrong)"), 4),
    (re.compile(r"(?:what the |wtf|fuck|shit|goddamn|damn).*(?:you|this|wrong)"), 4),
    (re.compile(r"you (?:keep|always|never|constantly)"), 3),
    (re.compile(r"(?:I'?m|I am) (?:so |really |fucking )?(?:frustrated|angry|pissed|annoyed|done|fed up)"), 3),
    # Medium
    (re.compile(r"you (?:don'?t|do not) (?:understand|get|know|listen)"), 2),
    (re.compile(r"(?:that'?s|that is) (?:wrong|incorrect|not right|not what I)"), 2),
    (re.compile(r"(?:are you|you) (?:serious|kidding|joking)"), 2),
    (re.compile(r"you'?re (?:doing|making|going).{0,10}(?:wrong|mistake|error)"), 2),
    # Light / sarcasm
    (re.compile(r"(?:great|wonderful|fantastic|brilliant|amazing).{0,20}(?:sarcasm|not|just what)"), 2),
    (re.compile(r"(?:congratulations|congrats|well done|good job|nice work).{0,10}(?:you|sarcasm)"), 2),
    (re.compile(r"(?:I asked|I said|I wanted|I meant).{0,20}(?:not|instead|different)"), 1),
    (re.compile(r"that'?s (?:helpful|useful).{0,10}(?:not|sarcasm)"), 2),
    (re.compile(r"how (?:many times|often|hard is it)"), 2),
]

AGENT_TARGET_RE = re.compile(
    r"(?:你|you|u)\s*(?:又|还|还是|仍然|老是|总是|keep|always|never|still|again)|"
    r"(?:你这|你个|your|you're|you are)",
    re.IGNORECASE,
)

# Keyword -> pain_direction mapping
PAIN_KEYWORD_MAP: dict[str, str] = {
    "文件": "file_operation", "路径": "file_operation", "读错": "file_operation",
    "找不到": "file_operation", "听不懂": "intent_understanding",
    "不理解": "intent_understanding", "理解错": "intent_understanding",
    "会错意": "intent_understanding", "答非所问": "intent_understanding",
    "太慢": "speed_performance", "卡": "speed_performance",
    "超时": "speed_performance", "等太久": "speed_performance",
    "重复": "repetition_loop", "又说": "repetition_loop",
    "又说一遍": "repetition_loop", "权限": "permission_issue",
    "不允许": "permission_issue", "拒绝": "permission_issue",
    "操作不了": "permission_issue",
    "wrong file": "file_operation", "wrong path": "file_operation",
    "file not found": "file_operation", "don't understand": "intent_understanding",
    "misunderstand": "intent_understanding", "not what i": "intent_understanding",
    "too slow": "speed_performance", "timeout": "speed_performance",
    "taking forever": "speed_performance", "repeating": "repetition_loop",
    "said that already": "repetition_loop", "permission": "permission_issue",
    "blocked": "permission_issue",
}


@dataclass
class FrustrationAssessment:
    """Result of frustration detection."""
    is_frustrated: bool
    severity: int  # 0-5
    target: str = "other"  # "agent" | "task_output" | "other"
    pain_points: list[str] = field(default_factory=list)
    original_input: str = ""
    confidence: float = 0.0
    detection_method: str = "none"  # "none" | "keyword" | "llm"
    filter_level: int = 0

    def should_trigger_vent(self) -> bool:
        """Whether to trigger the Vent flow."""
        return (
            self.is_frustrated
            and self.severity >= 1
            and self.target == "agent"
            and self.confidence >= 0.5
        )

    def vent_severity_level(self) -> str:
        """Map severity to vent response level."""
        if self.severity <= 1:
            return "light"
        elif self.severity <= 3:
            return "medium"
        else:
            return "heavy"


class FrustrationDetector:
    """Multi-level frustration detector with four-level funnel filtering.

    Phase 2 adds Filter 3 — LLM-based refinement of keyword results.
    The LLM is called DEFERRED: only when the user actually interacts with
    the Vent Panel, not on every message.
    """

    def __init__(self, model_gateway: "ModelGateway | None" = None) -> None:
        self._model_gateway = model_gateway
        self._cooldowns: dict[str, tuple[int, int]] = {}
        self._message_counts: dict[str, int] = {}

    def set_model_gateway(self, gateway: "ModelGateway") -> None:
        """Inject ModelGateway for Filter 3 LLM classification."""
        self._model_gateway = gateway

    def detect(
        self, user_input: str, session_id: str = "default", task_id: str = ""
    ) -> FrustrationAssessment:
        """Run full detection funnel (Filters 0-2) on a user message.

        Filter 3 (LLM) is deferred — call refine_with_llm() separately
        when the user interacts with the Vent Panel.
        """
        input_stripped = user_input.strip()
        if not input_stripped:
            return self._no_frustration("", "empty")

        self._message_counts[session_id] = self._message_counts.get(session_id, 0) + 1
        turn = self._message_counts[session_id]

        if self._check_cooldown(session_id, turn):
            return self._no_frustration(input_stripped, "cooldown")
        if self._is_technical_content(input_stripped):
            return self._no_frustration(input_stripped, "technical")
        if self._is_short_confirm(input_stripped):
            return self._no_frustration(input_stripped, "short_confirm")

        keyword_result = self._keyword_detect(input_stripped)
        if keyword_result.is_frustrated:
            return keyword_result

        return self._no_frustration(input_stripped, "clean")

    # ── Filter 3: LLM-based refinement (Phase 2) ──────────────

    async def refine_with_llm(
        self, assessment: FrustrationAssessment
    ) -> FrustrationAssessment:
        """Refine a keyword-based assessment using LLM (Filter 3).

        Called DEFERRED — only when the user actually interacts with the
        Vent Panel (selects a phrase or starts filling the template).
        This avoids wasting LLM calls on false positives.

        Falls back to the original keyword assessment on timeout/error.
        """
        if not self._model_gateway or not self._model_gateway.is_live():
            return assessment

        if not assessment.is_frustrated or not assessment.original_input:
            return assessment

        try:
            messages = [
                {"role": "system", "content": (
                    "You are a sentiment analysis engine for an AI Agent system. "
                    "Analyze whether the user's message expresses frustration "
                    "directed at the AI assistant (not the task result itself). "
                    "Be precise: distinguish between 'I am frustrated because the "
                    "task is hard' (target=task_output) vs 'You are useless, you "
                    "keep making mistakes' (target=agent). "
                    "Identify specific pain points from the user's complaint. "
                    "Respond in JSON only."
                )},
                {"role": "user", "content": (
                    f"User message: \"{assessment.original_input}\"\n\n"
                    f"Keyword analysis already detected: "
                    f"severity={assessment.severity}/5, "
                    f"pain_points={assessment.pain_points}. "
                    f"Refine this assessment. Is the user frustrated AT THE AGENT? "
                    f"What specific pain points are they complaining about?"
                )},
            ]

            schema = {
                "type": "object",
                "properties": {
                    "is_frustrated": {"type": "boolean"},
                    "severity": {"type": "integer", "minimum": 0, "maximum": 5},
                    "target": {"type": "string", "enum": ["agent", "task_output", "other"]},
                    "pain_points": {
                        "type": "array",
                        "items": {"type": "string", "enum": [
                            "file_operation", "intent_understanding",
                            "execution_quality", "speed_performance",
                            "repetition_loop", "permission_issue", "general",
                        ]},
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["is_frustrated", "severity", "target"],
            }

            result = await asyncio.wait_for(
                self._model_gateway._adapter.structured_chat(
                    messages, schema, temperature=0.1, max_tokens=256,
                ),
                timeout=LLM_FILTER_TIMEOUT,
            )

            refined = FrustrationAssessment(
                is_frustrated=result.get("is_frustrated", assessment.is_frustrated),
                severity=min(5, max(0, result.get("severity", assessment.severity))),
                target=result.get("target", assessment.target),
                pain_points=result.get("pain_points", assessment.pain_points),
                original_input=assessment.original_input,
                confidence=result.get("confidence", assessment.confidence),
                detection_method="llm",
                filter_level=3,
            )
            logger.info(
                "Filter 3 LLM refinement: sev %d→%d, target %s→%s, pain=%s→%s",
                assessment.severity, refined.severity,
                assessment.target, refined.target,
                assessment.pain_points, refined.pain_points,
            )
            return refined

        except asyncio.TimeoutError:
            logger.warning("Filter 3 LLM timed out after %.1fs, using Filter 2 result",
                          LLM_FILTER_TIMEOUT)
        except Exception as exc:
            logger.warning("Filter 3 LLM failed: %s, using Filter 2 result", exc)

        # Fallback: return original keyword assessment
        return assessment

    def record_vent_occurred(self, session_id: str) -> None:
        """Call after vent session closes to start cooldown."""
        turn = self._message_counts.get(session_id, 0)
        self._cooldowns[session_id] = (turn, COOLDOWN_TURNS)

    def reset_session(self, session_id: str) -> None:
        """Clear tracking for a session."""
        self._cooldowns.pop(session_id, None)
        self._message_counts.pop(session_id, None)

    def _check_cooldown(self, session_id: str, current_turn: int) -> bool:
        if session_id not in self._cooldowns:
            return False
        last_vent_turn, cooldown_turns = self._cooldowns[session_id]
        if current_turn - last_vent_turn <= cooldown_turns:
            return True
        del self._cooldowns[session_id]
        return False

    @staticmethod
    def _is_short_confirm(user_input: str) -> bool:
        cleaned = user_input.strip().lower().rstrip("!.?,;:。！？，；：")
        return len(cleaned) <= 6 and bool(SHORT_CONFIRM_RE.match(cleaned))

    @staticmethod
    def _is_technical_content(user_input: str) -> bool:
        code_blocks = CODE_BLOCK_RE.findall(user_input)
        if code_blocks:
            total_code_len = sum(len(b) for b in code_blocks)
            if total_code_len > len(user_input) * 0.5:
                return True
        if HEAVY_CODE_RE.match(user_input.strip()):
            return True
        if FILE_PATH_RE.fullmatch(user_input.strip()):
            return True
        return False

    def _keyword_detect(self, user_input: str) -> FrustrationAssessment:
        input_lower = user_input.lower()
        total_score = 0
        matched_pain_keywords: set[str] = set()

        for pattern, score in CN_FRUSTRATION_PATTERNS:
            if pattern.search(user_input):
                total_score += score
        for pattern, score in EN_FRUSTRATION_PATTERNS:
            if pattern.search(input_lower):
                total_score += score
        if AGENT_TARGET_RE.search(user_input):
            total_score += 1

        for keyword, direction in PAIN_KEYWORD_MAP.items():
            if keyword.lower() in input_lower:
                matched_pain_keywords.add(direction)

        if total_score >= 8:
            severity, confidence = 5, 0.9
        elif total_score >= 6:
            severity, confidence = 4, 0.85
        elif total_score >= 4:
            severity, confidence = 3, 0.75
        elif total_score >= 2:
            severity, confidence = 2, 0.6
        elif total_score >= 1:
            severity, confidence = 1, 0.45
        else:
            severity, confidence = 0, 0.0

        is_frustrated = severity >= 1 and confidence >= 0.5

        if is_frustrated:
            has_agent_target = bool(AGENT_TARGET_RE.search(user_input))
            has_heavy = any(p.search(user_input) for p, _ in CN_FRUSTRATION_PATTERNS[:8])
            if has_agent_target or has_heavy:
                target = "agent"
            elif matched_pain_keywords:
                target = "task_output"
            else:
                target = "other"
        else:
            target = "other"

        return FrustrationAssessment(
            is_frustrated=is_frustrated, severity=severity, target=target,
            pain_points=sorted(matched_pain_keywords) if matched_pain_keywords else [],
            original_input=user_input, confidence=confidence,
            detection_method="keyword" if is_frustrated else "none", filter_level=2,
        )

    @staticmethod
    def _no_frustration(original: str, reason: str) -> FrustrationAssessment:
        fl = {"cooldown": 0, "technical": 1, "short_confirm": 1, "clean": 2, "empty": 1}.get(reason, 0)
        return FrustrationAssessment(
            is_frustrated=False, severity=0, target="other",
            original_input=original, confidence=0.0,
            detection_method=reason, filter_level=fl,
        )
