"""Thinking Mode Evaluator — 根据任务意图评估 DeepSeek V4 推理深度.

DeepSeek V4 Pro 支持三种推理模式:
    disabled  — 非思考模式: 不生成推理 token, 最快最省 (默认)
    high      — 中等推理: 适合创意写作 / 复杂分析 / 多步研究
    max       — 最大推理: 适合数学证明 / 深度学术研究 (最慢最贵)

评估策略:
    1. 任务类型  — MULTI_STEP_RESEARCH / SPREADSHEET_ANALYSIS -> high
    2. 目标关键词 — 小说/创作/分析/设计/研究等多命中 -> high
    3. 风险等级  — HIGH/CRITICAL + 关键词命中 -> high
    4. 步骤类型覆盖 — TOOL步骤 -> disabled, MODEL_ANSWER步骤继承任务模式
"""

from __future__ import annotations

import logging

from flowcraft_core.domain.schemas import PlanStep, TaskBrief

logger = logging.getLogger(__name__)

# Task types that always benefit from deeper thinking
_HIGH_THINKING_TASK_TYPES: frozenset[str] = frozenset({
    "MULTI_STEP_RESEARCH",
    "SPREADSHEET_ANALYSIS",
})

# Objective keywords indicating creative/analytical work
_HIGH_THINKING_KEYWORDS: list[str] = [
    "小说", "创作", "创意", "故事", "剧本", "novel", "story", "creative", "write a",
    "分析", "研究", "评估", "诊断", "analysis", "research", "evaluate", "investigate",
    "设计", "架构", "方案", "design", "architecture", "solution",
    "复杂", "深入", "深度", "complex", "deep", "thorough",
    "万字", "千字", "长文", "篇幅", "长篇",
]


def evaluate_task_thinking(brief: TaskBrief) -> str:
    """Evaluate recommended thinking mode for a task.

    Returns one of: "disabled", "high", "max"

    Currently only uses "disabled" and "high" because "max" is reserved
    for explicit opt-in (user-facing setting in the future).
    """
    # Explicit override via task type
    if brief.task_type in _HIGH_THINKING_TASK_TYPES:
        logger.info(
            "Thinking mode 'high' for task %s: task_type=%s",
            brief.task_id, brief.task_type,
        )
        return "high"

    # Objective keyword analysis
    obj_lower = brief.objective.lower()
    keyword_hits = [kw for kw in _HIGH_THINKING_KEYWORDS if kw in obj_lower]

    if len(keyword_hits) >= 2:
        logger.info(
            "Thinking mode 'high' for task %s: %d keyword hits (%s)",
            brief.task_id, len(keyword_hits), ", ".join(keyword_hits[:5]),
        )
        return "high"

    if len(keyword_hits) >= 1 and str(brief.risk_level) in ("HIGH", "CRITICAL"):
        logger.info(
            "Thinking mode 'high' for task %s: keyword + high risk (%s)",
            brief.task_id, brief.risk_level,
        )
        return "high"

    return "disabled"


def evaluate_step_thinking(step: PlanStep, task_thinking: str) -> str:
    """Evaluate per-step thinking mode override.

    Rules:
        - TOOL steps: always "disabled" (tool calls don't benefit from reasoning)
        - MODEL_ANSWER steps: inherit task mode
        - Step-level override (step.thinking_mode) takes precedence
    """
    if step.thinking_mode is not None:
        return step.thinking_mode

    if step.action_type in ("TOOL", "PREPARE", "OBSERVE"):
        return "disabled"

    return task_thinking
