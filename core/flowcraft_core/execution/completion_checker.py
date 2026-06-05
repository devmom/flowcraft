"""Completion Checker — 步骤和任务完成判定逻辑.

完成判定层次:
    1. 步骤级: LLM 声明 final_answer → 验证输出质量
    2. 任务级: 所有步骤完成 → 验证最终输出是否满足 success_criteria
    3. 停止条件: 检查是否达到 stop_conditions
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from flowcraft_core.domain.enums import StepStatus, TaskStatus
from flowcraft_core.domain.schemas import ExecutionPlan, PlanStep, Task, TaskBrief

logger = logging.getLogger(__name__)


@dataclass
class StepCompletionResult:
    """步骤完成判定结果."""
    step: PlanStep
    is_complete: bool
    output: str
    quality_score: float  # 0.0 ~ 1.0
    issues: list[str] = field(default_factory=list)
    needs_replan: bool = False
    needs_clarification: str = ""


@dataclass
class TaskCompletionResult:
    """任务完成判定结果."""
    is_complete: bool
    final_output: str
    completed_steps: int
    total_steps: int
    issues: list[str] = field(default_factory=list)
    needs_replan: bool = False
    needs_clarification: str = ""


class CompletionChecker:
    """完成判定器：检查步骤输出是否满足预期，任务是否已完成."""

    MIN_ANSWER_LENGTH = 10        # 最小回答长度（字符）
    MIN_QUALITY_SCORE = 0.3       # 最低质量分

    # ── 步骤级判定 ──────────────────────────────────────────

    def check_step(self, step: PlanStep, step_output: str) -> StepCompletionResult:
        """检查单个步骤是否已完成且输出质量合格."""
        issues: list[str] = []
        quality = 1.0

        # 1. 空输出检查
        if not step_output or len(step_output.strip()) < self.MIN_ANSWER_LENGTH:
            issues.append(f"步骤输出过短 ({len(step_output)} 字符)")
            quality -= 0.5

        # 2. 元推理检测：检测 LLM 是否输出了内部思考过程而非用户可读结果
        meta_reasoning_keywords = [
            "当前步骤是", "根据会话历史", "根据任务要求",
            "在生成", "之前", "需要先", "尚未提供", "未指定",
            "The current step is", "Based on session", "I need to first",
            "Step 1 has", "Step 2 has",
        ]
        output_stripped = step_output.strip()
        for kw in meta_reasoning_keywords:
            if output_stripped.startswith(kw) or (kw in output_stripped[:80]):
                quality -= 0.4
                issues.append(f"输出包含元推理而非实际结果: '{kw}'")
                break  # One detection is enough for heavy penalty

        # 3. 错误关键词检测
        error_keywords = [
            ("抱歉", 0.1), ("I'm sorry", 0.1), ("无法", 0.2), ("cannot", 0.2),
            ("错误", 0.15), ("error", 0.15), ("失败", 0.2), ("failed", 0.2),
            ("不知道", 0.3), ("don't know", 0.3),
        ]
        output_lower = step_output.lower()
        for kw, penalty in error_keywords:
            if kw in output_lower:
                quality -= penalty
                issues.append(f"输出包含负面关键词: '{kw}'")

        # 3. 长度检查（相对预期输出）
        if step.expected_output and len(step_output) < 20:
            issues.append("步骤输出可能不完整")

        # 4. 循环检测（是否有重复语句）
        if len(step_output) > 50:
            half = len(step_output) // 2
            if step_output[:half].strip() == step_output[half:].strip():
                issues.append("检测到重复输出（可能的循环）")
                quality -= 0.3

        # 5. 计算质量分
        quality = max(0.0, min(1.0, quality))

        is_complete = len(issues) == 0 or quality >= self.MIN_QUALITY_SCORE

        if issues and quality < self.MIN_QUALITY_SCORE:
            logger.warning("Step %d quality low: %.2f, issues=%s",
                           step.index, quality, issues)

        return StepCompletionResult(
            step=step,
            is_complete=is_complete,
            output=step_output,
            quality_score=quality,
            issues=issues,
            needs_replan=(quality < 0.3),
        )

    # ── 任务级判定 ──────────────────────────────────────────

    def check_task(
        self,
        task: Task,
        plan: ExecutionPlan,
        step_results: list[StepCompletionResult],
    ) -> TaskCompletionResult:
        """检查整个任务是否完成."""
        total = len(plan.steps)
        completed = sum(1 for s in step_results if s.is_complete)
        all_outputs = "\n\n".join(s.output for s in step_results if s.output)

        issues: list[str] = []
        needs_replan = False

        # 1. 所有步骤完成？
        if completed < total:
            issues.append(f"仅完成 {completed}/{total} 个步骤")

        # 2. 汇总质量问题
        for sr in step_results:
            if sr.issues:
                issues.extend(f"步骤{sr.step.index}: {i}" for i in sr.issues)

        # 3. 最后的步骤是否失败？
        last_complete = next((s for s in reversed(step_results) if s.is_complete), None)
        if not last_complete:
            issues.append("没有任何步骤成功完成")
            needs_replan = True

        # 4. 停止条件检查
        if plan.stop_conditions:
            for cond in plan.stop_conditions:
                if cond.lower() in all_outputs.lower():
                    issues.append(f"触发停止条件: {cond}")
                    return TaskCompletionResult(
                        is_complete=True,
                        final_output=all_outputs,
                        completed_steps=completed,
                        total_steps=total,
                        issues=issues,
                    )

        is_complete = completed >= total and not needs_replan

        return TaskCompletionResult(
            is_complete=is_complete,
            final_output=all_outputs,
            completed_steps=completed,
            total_steps=total,
            issues=issues,
            needs_replan=needs_replan,
        )

    # ── 快速检查 ──────────────────────────────────────────

    @staticmethod
    def is_likely_complete(step_output: str) -> bool:
        """快速启发式判断：LLM 输出看起来像完成回答."""
        if not step_output:
            return False
        return len(step_output) >= CompletionChecker.MIN_ANSWER_LENGTH

    @staticmethod
    def needs_more_info(step_output: str) -> bool:
        """LLM 是否在请求更多信息."""
        ask_keywords = ["请问", "请提供", "需要更多", "不确定", "需要知道",
                        "could you", "please provide", "need more", "need to know"]
        return any(kw in step_output.lower() for kw in ask_keywords)
