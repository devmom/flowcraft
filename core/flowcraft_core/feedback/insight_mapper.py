"""InsightMapper — maps structured vent feedback to actionable failure types.

Takes the filled vent template + pain_points and maps them to:
    1. FailureType classification
    2. Immediate correction hint for the Agent
    3. Calls FeedbackMemoryIntegrator for persistence

Phase 2 adds LLM-based analysis for precise failure classification
and correction hint generation from user's natural language complaint.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from flowcraft_core.execution.failure_handler import FailureType

if TYPE_CHECKING:
    from flowcraft_core.models.gateway import ModelGateway

logger = logging.getLogger(__name__)

LLM_INSIGHT_TIMEOUT = 3.0  # seconds


# ── Pain direction -> FailureType mapping ────────────────────

PAIN_TO_FAILURE: dict[str, FailureType] = {
    "file_operation": FailureType.TOOL_ERROR,
    "intent_understanding": FailureType.MODEL_PARSE_ERROR,
    "execution_quality": FailureType.TOOL_ERROR,
    "speed_performance": FailureType.TIMEOUT,
    "repetition_loop": FailureType.UNKNOWN,  # Requires context compression analysis
    "permission_issue": FailureType.PERMISSION_DENIED,
    "general": FailureType.UNKNOWN,
}

# ── FailureType -> Correction hint ───────────────────────────

CORRECTION_HINTS: dict[FailureType, str] = {
    FailureType.TOOL_ERROR:
        "在使用工具前，先验证输入参数和前置条件。对于文件操作，先用 file_search 确认路径存在。",
    FailureType.MODEL_PARSE_ERROR:
        "请重新解析用户意图。使用更明确的分步骤描述，避免模糊表述。",
    FailureType.TIMEOUT:
        "请缩小任务范围或建议用户使用更轻量的模型。对于大文件操作，先检查文件大小。",
    FailureType.PERMISSION_DENIED:
        "检查 settings.allowed_paths 配置或安全策略设置。如果需要访问新路径，请提示用户添加。",
    FailureType.UNKNOWN:
        "请重新审视执行步骤，检查是否有遗漏的前置条件或更优的实现方式。",
}

# ── Keyword -> FailureType mapping (for text analysis) ───────

KEYWORD_MAP: dict[str, tuple[FailureType, str]] = {
    "读错文件": (FailureType.TOOL_ERROR, "使用 file_search 工具先确认目标文件是否存在和路径是否正确"),
    "wrong file": (FailureType.TOOL_ERROR, "Use file_search tool to verify the file path exists before reading"),
    "听不懂": (FailureType.MODEL_PARSE_ERROR, "切换为更明确的指令，使用分步骤描述替代模糊的整体需求"),
    "don't understand": (FailureType.MODEL_PARSE_ERROR, "Switch to clearer instructions with step-by-step breakdown"),
    "太慢": (FailureType.TIMEOUT, "缩小任务范围或切换到更轻量的模型"),
    "too slow": (FailureType.TIMEOUT, "Reduce task scope or switch to a lighter model"),
    "重复": (FailureType.UNKNOWN, "启用更积极的上下文压缩策略，避免重复生成相同内容"),
    "repeating": (FailureType.UNKNOWN, "Enable more aggressive context compression to avoid repetition"),
    "权限": (FailureType.PERMISSION_DENIED, "检查 settings.allowed_paths 和安全策略配置"),
    "permission": (FailureType.PERMISSION_DENIED, "Check allowed_paths configuration and security policies"),
}


@dataclass
class VentInsight:
    """Result of mapping vent feedback to actionable insight."""
    failure_type: FailureType
    correction_hint: str
    pain_directions: list[str] = field(default_factory=list)
    key_findings: list[str] = field(default_factory=list)
    severity: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_type": self.failure_type.value,
            "correction_hint": self.correction_hint,
            "pain_directions": self.pain_directions,
            "key_findings": self.key_findings,
            "severity": self.severity,
        }


class InsightMapper:
    """Maps structured vent feedback to system-actionable insights.

    Phase 1: Keyword-based mapping (fast, no LLM)
    Phase 2: LLM-based analysis for precise classification
    """

    def __init__(self, model_gateway: "ModelGateway | None" = None) -> None:
        self._model_gateway = model_gateway

    def set_model_gateway(self, gateway: "ModelGateway") -> None:
        """Inject ModelGateway for LLM-based analysis."""
        self._model_gateway = gateway

    def map_from_pain_direction(
        self, pain_direction: str, severity: int = 1
    ) -> VentInsight:
        """Map a single pain_direction to failure type and correction hint."""
        failure_type = PAIN_TO_FAILURE.get(pain_direction, FailureType.UNKNOWN)
        hint = CORRECTION_HINTS.get(failure_type, CORRECTION_HINTS[FailureType.UNKNOWN])
        return VentInsight(
            failure_type=failure_type,
            correction_hint=hint,
            pain_directions=[pain_direction],
            severity=severity,
        )

    def map_from_pain_points(
        self, pain_points: list[str], severity: int = 1
    ) -> VentInsight:
        """Map multiple pain_points to a consolidated insight."""
        if not pain_points:
            return VentInsight(
                failure_type=FailureType.UNKNOWN,
                correction_hint=CORRECTION_HINTS[FailureType.UNKNOWN],
                severity=severity,
            )

        # Deduplicate failure types
        failure_types: list[FailureType] = []
        seen: set[str] = set()
        for pp in pain_points:
            ft = PAIN_TO_FAILURE.get(pp, FailureType.UNKNOWN)
            if ft.value not in seen:
                failure_types.append(ft)
                seen.add(ft.value)

        # Use the first (most relevant) failure type
        primary_ft = failure_types[0] if failure_types else FailureType.UNKNOWN
        hint = CORRECTION_HINTS.get(primary_ft, CORRECTION_HINTS[FailureType.UNKNOWN])

        return VentInsight(
            failure_type=primary_ft,
            correction_hint=hint,
            pain_directions=pain_points,
            severity=severity,
        )

    def map_from_text(self, user_text: str) -> VentInsight:
        """Map raw user complaint text to insight (keyword-based)."""
        text_lower = user_text.lower()
        found_ft: FailureType | None = None
        found_hint = ""
        key_findings: list[str] = []

        for keyword, (ft, hint) in KEYWORD_MAP.items():
            if keyword.lower() in text_lower:
                if found_ft is None:
                    found_ft = ft
                    found_hint = hint
                key_findings.append(keyword)

        if found_ft is None:
            found_ft = FailureType.UNKNOWN
            found_hint = CORRECTION_HINTS[FailureType.UNKNOWN]

        return VentInsight(
            failure_type=found_ft,
            correction_hint=found_hint,
            key_findings=key_findings,
        )

    # ── Phase 2: LLM-based analysis ─────────────────────────

    async def map_with_llm(
        self,
        user_complaint: str,
        pain_points: list[str] | None = None,
        task_objective: str = "",
        severity: int = 1,
    ) -> VentInsight:
        """LLM-based insight mapping for precise failure classification.

        Uses the ModelGateway to analyze the user's natural language
        complaint and generate:
        1. Precise FailureType classification
        2. Actionable correction hint tailored to the specific situation
        3. Key findings extracted from the complaint

        Falls back to keyword-based mapping on timeout/error.
        """
        # Start with keyword-based insight as fallback
        keyword_insight = self.map_from_text(user_complaint)
        if pain_points:
            pp_insight = self.map_from_pain_points(pain_points, severity)
            if pp_insight.failure_type != FailureType.UNKNOWN:
                keyword_insight = pp_insight

        if not self._model_gateway or not self._model_gateway.is_live():
            return keyword_insight

        try:
            messages = [
                {"role": "system", "content": (
                    "You are a failure analysis engine for an AI agent system. "
                    "Analyze user complaints and classify them into failure types. "
                    "Generate specific, actionable correction hints.\n\n"
                    "Failure types:\n"
                    "- TOOL_ERROR: File operations, tool execution failures\n"
                    "- MODEL_PARSE_ERROR: Misunderstanding user intent\n"
                    "- TIMEOUT: Too slow, timeout issues\n"
                    "- PERMISSION_DENIED: Permission/access issues\n"
                    "- UNKNOWN: Cannot classify or general complaint\n\n"
                    "The correction hint MUST be specific and actionable. "
                    "Respond in JSON."
                )},
                {"role": "user", "content": (
                    f"User complaint: \"{user_complaint}\"\n"
                    f"Task context: {task_objective or 'unknown'}\n"
                    f"Keyword analysis suggests: {keyword_insight.failure_type.value} "
                    f"({', '.join(pain_points or [])})\n\n"
                    f"Classify the failure type and provide a SPECIFIC correction hint. "
                    f"The hint should be an instruction the agent can follow "
                    f"to avoid this mistake in the future."
                )},
            ]

            schema = {
                "type": "object",
                "properties": {
                    "failure_type": {
                        "type": "string",
                        "enum": ["TOOL_ERROR", "MODEL_PARSE_ERROR", "TIMEOUT",
                                 "PERMISSION_DENIED", "UNKNOWN"],
                    },
                    "correction_hint": {"type": "string"},
                    "key_findings": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["failure_type", "correction_hint"],
            }

            result = await asyncio.wait_for(
                self._model_gateway._adapter.structured_chat(
                    messages, schema, temperature=0.2, max_tokens=512,
                ),
                timeout=LLM_INSIGHT_TIMEOUT,
            )

            ft_str = result.get("failure_type", keyword_insight.failure_type.value)
            try:
                failure_type = FailureType(ft_str)
            except ValueError:
                failure_type = keyword_insight.failure_type

            return VentInsight(
                failure_type=failure_type,
                correction_hint=result.get("correction_hint", keyword_insight.correction_hint),
                pain_directions=pain_points or [],
                key_findings=result.get("key_findings", []),
                severity=severity,
            )

        except asyncio.TimeoutError:
            logger.warning("LLM insight mapping timed out, using keyword result")
        except Exception as exc:
            logger.warning("LLM insight mapping failed: %s, using keyword result", exc)

        return keyword_insight

    def generate_correction_for_agent(self, insight: VentInsight) -> str:
        """Generate a concise correction message for the Agent."""
        return (
            f"[反馈修正] 检测到问题类型: {insight.failure_type.value}。"
            f"建议: {insight.correction_hint}"
        )
