"""Execution Engine — 真正的 Agent 执行循环.

对每个计划步骤:
1. 构建包含步骤目标 + 可用工具 + 上下文的提示
2. 调用 LLM 决策：使用工具 or 给出最终回答
3. 如果是工具调用：解析工具意图 → 通过 ToolHarness 执行 → 观察结果反馈给 LLM → 循环
4. 如果是最终回答：步骤完成
5. 高风险工具调用走审批流程
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flowcraft_core.domain.enums import PlanMode, RiskLevel, StepStatus, TaskStatus
from flowcraft_core.domain.schemas import (
    ExecutionPlan,
    PlanStep,
    Task,
    TaskBrief,
    ToolIntent,
    ToolObservation,
    TraceEvent,
    now_utc,
)
from flowcraft_core.execution.completion_checker import CompletionChecker
from flowcraft_core.execution.context_compressor import ContextCompressor
from flowcraft_core.execution.failure_handler import (
    FailureInfo, FailureType, StepFailedError,
    classify_exception, retry_with_backoff,
)
from flowcraft_core.logging_config import get_trace_logger, TraceSpan
from flowcraft_core.models.gateway import ModelGateway
from flowcraft_core.observability.events import EventRecorder
from flowcraft_core.policy.engine import PolicyEngine
from flowcraft_core.tools.harness import ToolHarness, ToolRegistry

logger = logging.getLogger(__name__)
trace = get_trace_logger("execution.engine")


class PauseController:
    """任务暂停/取消/恢复控制器 (线程安全)."""

    def __init__(self) -> None:
        self._paused = threading.Event()
        self._cancelled = threading.Event()
        self._paused.set()  # 默认不暂停

    def pause(self) -> None:
        self._paused.clear()

    def resume(self) -> None:
        self._paused.set()

    def cancel(self) -> None:
        self._cancelled.set()
        self._paused.set()

    def check(self) -> None:
        if self._cancelled.is_set():
            raise TaskCancelledError("任务已被取消")
        self._paused.wait()

    @property
    def is_paused(self) -> bool:
        return not self._paused.is_set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def reset(self) -> None:
        self._paused.set()
        self._cancelled.clear()


class TaskCancelledError(Exception):
    pass


# Global pause controllers per task
_pause_controllers: dict[str, PauseController] = {}


def get_pause_controller(task_id: str) -> PauseController:
    if task_id not in _pause_controllers:
        _pause_controllers[task_id] = PauseController()
    return _pause_controllers[task_id]


def remove_pause_controller(task_id: str) -> None:
    _pause_controllers.pop(task_id, None)

# ── LLM Decision Schema ──────────────────────────────────────
# 用于让 LLM 返回结构化的决策：调用工具 or 给出最终回答
STEP_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["tool_call", "final_answer", "ask_user"],
            "description": "Next action to take",
        },
        "reasoning": {
            "type": "string",
            "description": "INTERNAL ONLY: Brief reasoning for YOURSELF (1-2 sentences). NEVER show this to the user.",
        },
        "tool_name": {
            "type": "string",
            "description": "Tool name (only when action=tool_call)",
        },
        "tool_input": {
            "type": "object",
            "description": "Tool parameters (only when action=tool_call)",
        },
        "final_answer": {
            "type": "string",
            "description": "USER-FACING OUTPUT: The actual content to show to the user. When action=final_answer, this is the complete result. When action=ask_user, this is the question to ask. MUST NOT contain meta-reasoning, process descriptions, or 'current step' explanations.",
        },
        "purpose": {
            "type": "string",
            "description": "Purpose of tool call (only when action=tool_call)",
        },
    },
    "required": ["action", "reasoning"],
}


class ExecutionEngine:
    """执行引擎：按计划逐步执行，集成 LLM + 工具 + 审批 + 失败重试 + 完成判定 + 检查点。

    v0.2 additions:
    - ReflectionLoop: quality self-review (generate→evaluate→refine)
    - ReActExecutor: standard Thought→Action→Observation loop
    """

    MAX_TOOL_ROUNDS_PER_STEP = 8
    MAX_STEP_RETRIES = 2
    MAX_TOOL_FAIL_STREAK = 2  # 同一工具连续失败N次后跳过（跨步骤）

    def __init__(
        self,
        model_gateway: ModelGateway,
        tool_registry: ToolRegistry,
        tool_harness: ToolHarness,
        policy_engine: PolicyEngine,
        events: EventRecorder,
        checkpoint_manager: Any | None = None,  # CheckpointManager, lazy import
        enable_reflection: bool = False,
        workspace_dir: Path | None = None,
        skill_registry=None,  # Phase 1: SkillRegistry
        settings=None,  # Settings object (for add_allowed_path)
    ) -> None:
        self.model_gateway = model_gateway
        self.tool_registry = tool_registry
        self.tool_harness = tool_harness
        self.policy_engine = policy_engine
        self.events = events
        self.completion_checker = CompletionChecker()
        self._memory_manager = None  # lazy init from app
        self.checkpoint_manager = checkpoint_manager
        self.workspace_dir = workspace_dir or Path.cwd()
        self.context_compressor = ContextCompressor(model_gateway=model_gateway)
        self._tool_fail_streak: dict[str, int] = {}  # cross-step tool failure tracking
        self._current_thinking_mode: str = "disabled"  # set per-step before LLM call
        self._skill_registry = skill_registry  # Phase 1
        self._settings = settings  # For add_allowed_path on DENIED retry

        # v0.2: Reflection & ReAct
        from flowcraft_core.execution.reflection import ReflectionLoop
        self.reflection = ReflectionLoop(model_gateway) if enable_reflection else None
        from flowcraft_core.execution.react_executor import ReActExecutor
        self.react_executor = ReActExecutor(
            model_gateway, tool_harness, events,
            enable_reflection=enable_reflection,
        )

        # Phase 2: Dynamic Script Executor (lazy init on first use)
        self._dynamic_script_executor = None

    def _identity_block(self) -> str:
        """Build the agent identity section injected into every system prompt."""
        profile = getattr(self.model_gateway, '_profile', None)
        provider = self.model_gateway.provider_name
        model_id = profile.model_id if profile else "unknown"
        display = profile.display_name if profile else model_id

        # Current date/time — critical for any task involving "today", "latest", etc.
        now = datetime.now(timezone.utc)
        now_str = now.strftime("%Y-%m-%d")
        weekday = now.strftime("%A")
        time_str = now.strftime("%H:%M UTC")

        # Summarize available tools by category
        tools = self.tool_registry.list_definitions()
        cats: dict[str, list[str]] = {}
        for t in tools:
            cats.setdefault(t["category"], []).append(t["tool_name"])
        tool_summary = "; ".join(f"{c}({', '.join(v)})" for c, v in sorted(cats.items()))

        return (
            f"## YOUR IDENTITY (CRITICAL — read carefully)\n"
            f"You are FlowCraft Agent v0.1.0, running on **{display}** ({provider}).\n"
            f"When asked about your model/provider, ALWAYS answer: {display} by {provider}.\n"
            f"NEVER claim to be Claude, GPT, Gemini, or any other model.\n"
            f"## TODAY'S DATE (CRITICAL)\n"
            f"Current date: **{now_str}** ({weekday}), time: {time_str}.\n"
            f"ALWAYS use this date for search queries, file names, and time-sensitive tasks.\n"
            f"NEVER use training-data dates (e.g., 2024 or 2025) when searching for 'today' or 'latest'.\n"
            f"You operate on the user's local Windows machine with these tools:\n"
            f"{tool_summary}\n"
            f"**KEY CAPABILITIES**:\n"
            f"- `exec`: Run shell commands (git, pip, python, npm, cargo, etc.). "
            f"Use for: installing packages, running scripts AS FILES (NOT inline -c), "
            f"git operations, building projects, running tests.\n"
            f"- `apply_patch`: Structured multi-file code edits (create/update/delete/patch). "
            f"Use for: making precise code changes with automatic backups.\n"
            f"- `skill.execute`: Run pre-written deterministic scripts (data analysis, file ops, etc.).\n"
            f"- `code.execute`: Execute Python in a SAFE sandbox (no file access, no network). "
            f"Only for pure computation.\n"
            f"- `file.read` / `file.write`: Read and write files.\n"
            f"- `command.run`: Run system commands (legacy, prefer `exec`).\n"
            f"**WHEN TO USE WHICH**:\n"
            f"- Need to install packages or run a script file? → `exec`\n"
            f"- Need to make precise code changes to files? → `apply_patch`\n"
            f"- Need to do pure data analysis/computation? → `code.execute` (sandbox) or `skill.execute`\n"
            f"- NEVER use `exec` with inline eval (python -c, node -e). Write to a file first, then execute.\n"
            f"You CANNOT: access the internet freely (tools only), remember between sessions (yet), "
            f"train/learn from interactions.\n"
        )

    async def execute_plan(
        self,
        task: Task,
        brief: TaskBrief,
        plan: ExecutionPlan,
    ) -> Task:
        """执行完整计划，返回更新后的 Task。

        支持 DIRECT/LINEAR/DAG/ITERATIVE 模式。
        复杂任务自动子任务分解。
        集成 CompletionChecker、FailureHandler、暂停/取消。
        """
        tid = task.task_id
        mode = plan.mode
        pc = get_pause_controller(task.task_id)
        pc.reset()

        t0 = _time.monotonic()
        trace.pipeline_begin(tid, "execute_plan",
                           f"开始执行: mode={mode.value} steps={len(plan.steps)} goal={plan.goal[:80]}")

        # ── 复杂任务自动分解 ──
        if len(plan.steps) >= 6 and mode in (PlanMode.LINEAR, PlanMode.DAG):
            trace.info(tid, "execute_plan.route",
                      f"复杂任务({len(plan.steps)}步) → 分解执行", extra={"steps": len(plan.steps), "mode": mode.value})
            result = await self._execute_decomposed(task, brief, plan, mode)
            trace.pipeline_end(tid, "execute_plan",
                             f"分解执行完成: status={result.status.value}", elapsed_s=_time.monotonic() - t0)
            return result

        if mode == PlanMode.ITERATIVE:
            trace.info(tid, "execute_plan.route", "ITERATIVE模式")
            result = await self._execute_iterative(task, brief, plan)
            trace.pipeline_end(tid, "execute_plan",
                             f"ITERATIVE完成: status={result.status.value}", elapsed_s=_time.monotonic() - t0)
            return result

        if mode == PlanMode.DAG:
            trace.info(tid, "execute_plan.route", f"DAG模式: {len(plan.steps)}步")
            result = await self._execute_dag(task, brief, plan)
            trace.pipeline_end(tid, "execute_plan",
                             f"DAG完成: status={result.status.value}", elapsed_s=_time.monotonic() - t0)
            return result

        self._event(task, "execution.started", "开始执行计划",
                     f"共 {len(plan.steps)} 个步骤，模式: {mode.value}")

        task.status = TaskStatus.EXECUTING
        task.updated_at = now_utc()

        all_tool_observations: list[ToolObservation] = []
        step_outputs: list[str] = []
        step_results: list = []  # for CompletionChecker

        for step in plan.steps:
            # 检查暂停/取消
            try:
                pc.check()
            except TaskCancelledError:
                trace.warn(tid, "execute_plan.cancel", f"步骤{step.index}前检测到取消")
                task.status = TaskStatus.CANCELLED
                task.failed_reason = "任务已被用户取消"
                task.updated_at = now_utc()
                self._event(task, "task.cancelled", "任务已取消",
                            "用户取消了任务执行", severity="WARN")
                remove_pause_controller(task.task_id)
                trace.pipeline_end(tid, "execute_plan", "任务已取消", elapsed_s=_time.monotonic() - t0)
                return task

            if step.status == StepStatus.SKIPPED:
                trace.info(tid, "execute_plan.skip", f"步骤{step.index} {step.title} 已跳过")
                continue

            if step.status == StepStatus.COMPLETED:
                trace.info(tid, "execute_plan.resume_skip",
                          f"步骤{step.index} {step.title} 已执行过，跳过")
                continue

            trace.step_begin(tid, step.index, step.title, step.objective)

            self._event(task, "step.started", f"开始: {step.title}",
                         step.objective, {"step_index": step.index})

            step_t0 = _time.monotonic()
            try:
                observation, step_answer = await self._execute_step(
                    task, brief, step, all_tool_observations,
                    prior_step_outputs=list(step_outputs))
                if observation:
                    all_tool_observations.append(observation)
                if step_answer:
                    step_outputs.append(step_answer)

                step.status = StepStatus.COMPLETED
                step_elapsed = _time.monotonic() - step_t0
                trace.step_end(tid, step.index, step.title, elapsed_s=step_elapsed,
                              result=step_answer or "")
                self._event(task, "step.completed", f"完成: {step.title}",
                             f"步骤 {step.index} 执行成功",
                             {"step_index": step.index, "output": step_answer or ""})

                # Memory: persist step output
                self._remember_step(task, step, step_answer or "")

                # Checkpoint: save progress after each successful step
                if self.checkpoint_manager:
                    try:
                        completed_indices = [s.index for s in plan.steps
                                           if s.status == StepStatus.COMPLETED]
                        summary = self._build_context_summary(all_tool_observations)
                        self.checkpoint_manager.save(
                            task_id=task.task_id,
                            completed_step_indices=completed_indices,
                            current_step_index=step.index + 1,
                            observations=all_tool_observations,
                            context_summary=summary,
                            plan=plan,
                        )
                    except Exception:
                        pass  # checkpoint is best-effort, never block execution

            except TaskCancelledError:
                trace.warn(tid, "execute_plan.cancel", f"步骤{step.index}执行中取消")
                task.status = TaskStatus.CANCELLED
                task.failed_reason = "任务已被用户取消"
                task.updated_at = now_utc()
                self._event(task, "task.cancelled", "任务已取消",
                            "用户取消了任务执行", severity="WARN")
                remove_pause_controller(task.task_id)
                trace.pipeline_end(tid, "execute_plan", "任务已取消", elapsed_s=_time.monotonic() - t0)
                return task

            except ApprovalRequiredError as exc:
                step_elapsed = _time.monotonic() - step_t0
                trace.approval_wait(tid, step.index, exc.tool_name, exc.reason, elapsed_s=step_elapsed)
                self._record_approval_request(task, step, exc)
                task.status = TaskStatus.WAITING_APPROVAL
                task.failed_reason = f"需要用户审批: {exc.tool_name} - {exc.reason}"
                task.updated_at = now_utc()
                self._event(task, "approval.requested", "需要审批",
                             f"工具 {exc.tool_name} 需要用户确认",
                             {"step_index": step.index, "tool_name": exc.tool_name})
                trace.pipeline_end(tid, "execute_plan", f"等待审批: {exc.tool_name}", elapsed_s=_time.monotonic() - t0)
                return task

            except StepFailedError as exc:
                step_elapsed = _time.monotonic() - step_t0
                step.status = StepStatus.FAILED
                fail_info = exc.failure_info if hasattr(exc, 'failure_info') else classify_exception(exc)
                trace.step_failed(tid, step.index, step.title,
                                 f"type={fail_info.failure_type.value} msg={fail_info.user_message}",
                                 elapsed_s=step_elapsed)
                self._event(task, "step.failed", f"失败: {step.title}",
                             fail_info.user_message,
                             {"step_index": step.index, "failure_type": fail_info.failure_type.value},
                             severity="ERROR")

                # REPLANNING: 让 LLM 生成替代计划
                if self.model_gateway.is_live() and not fail_info.is_terminal:
                    trace.info(tid, "execute_plan.replan",
                              f"步骤{step.index}失败，尝试重新规划")
                    try:
                        new_plan = await self._replan(task, brief, plan, step, fail_info.user_message)
                        if new_plan and new_plan.steps:
                            trace.info(tid, "execute_plan.replanned",
                                      f"重新规划: 新计划{len(new_plan.steps)}步")
                            self._event(task, "plan.replanned", "已重新规划",
                                         f"步骤 {step.index} 失败，生成新计划含 {len(new_plan.steps)} 步",
                                         {"failed_step": step.index})
                            for new_step in new_plan.steps:
                                if new_step.index <= step.index:
                                    new_step.status = StepStatus.SKIPPED
                                    continue
                                try:
                                    pc.check()
                                except TaskCancelledError:
                                    return self._cancel_task(task)
                                self._event(task, "step.started", f"重试: {new_step.title}",
                                             new_step.objective, {"step_index": new_step.index})
                                try:
                                    obs = await self._execute_step(
                                        task, brief, new_step, all_tool_observations,
                                        prior_step_outputs=list(step_outputs))
                                    if obs[0]:
                                        all_tool_observations.append(obs[0])
                                    new_step.status = StepStatus.COMPLETED
                                    self._event(task, "step.completed", f"重试完成: {new_step.title}",
                                                 f"步骤 {new_step.index} 重试成功",
                                                 {"step_index": new_step.index})
                                except (ApprovalRequiredError, StepFailedError, TaskCancelledError):
                                    if isinstance(_, TaskCancelledError):
                                        return self._cancel_task(task)
                                    task.status = TaskStatus.FAILED
                                    task.failed_reason = f"重试步骤 {new_step.index} 失败"
                                    task.updated_at = now_utc()
                                    remove_pause_controller(task.task_id)
                                    trace.pipeline_end(tid, "execute_plan", f"重试失败", elapsed_s=_time.monotonic() - t0)
                                    return task
                            break
                    except Exception as replan_exc:
                        trace.warn(tid, "execute_plan.replan_error", f"重新规划异常: {replan_exc}")

                task.status = TaskStatus.FAILED
                task.failed_reason = fail_info.user_message
                task.updated_at = now_utc()
                remove_pause_controller(task.task_id)
                trace.pipeline_end(tid, "execute_plan", f"执行失败: {fail_info.user_message}", elapsed_s=_time.monotonic() - t0)
                return task

        # CompletionChecker: 任务级完成判定
        final_output = "\n\n".join(step_outputs) if step_outputs else "(无文本输出)"

        # Defense-in-depth: detect if critical generation steps only produced fallback/error
        fallback_count = sum(
            1 for out in step_outputs
            if self._is_likely_fallback(out)
        )
        total_steps = len(step_outputs)
        # If >50% of step outputs are fallback messages, the task didn't produce real work
        if total_steps > 0 and fallback_count / total_steps > 0.5:
            task.status = TaskStatus.FAILED
            task.failed_reason = (
                f"任务未能生成有效内容：{fallback_count}/{total_steps} 个步骤"
                f"因LLM调用失败仅返回错误提示"
            )
            task.updated_at = now_utc()
            self._event(task, "task.failed", "LLM调用失败导致任务无效",
                         task.failed_reason,
                         {"fallback_steps": fallback_count, "total_steps": total_steps},
                         severity="ERROR")
            remove_pause_controller(task.task_id)
            trace.pipeline_end(tid, "execute_plan",
                             f"任务失败: {task.failed_reason}",
                             elapsed_s=_time.monotonic() - t0)
            return task

        self._event(task, "task.completed", "任务已完成",
                     final_output,
                     {"steps_completed": len(plan.steps),
                      "tool_calls": len(all_tool_observations),
                      "output": final_output})

        task.status = TaskStatus.COMPLETED
        task.completed_at = now_utc()
        task.updated_at = now_utc()
        remove_pause_controller(task.task_id)
        total_elapsed = _time.monotonic() - t0
        trace.pipeline_end(tid, "execute_plan",
                         f"全部完成: {len(plan.steps)}步, {len(all_tool_observations)}次工具调用",
                         elapsed_s=total_elapsed)
        return task

    def _cancel_task(self, task: Task) -> Task:
        task.status = TaskStatus.CANCELLED
        task.failed_reason = "任务已被用户取消"
        task.updated_at = now_utc()
        self._event(task, "task.cancelled", "任务已取消",
                    "用户取消了任务执行", severity="WARN")
        remove_pause_controller(task.task_id)
        return task

    async def _execute_decomposed(
        self,
        task: Task,
        brief: TaskBrief,
        plan: ExecutionPlan,
        mode: PlanMode,
    ) -> Task:
        """复杂任务子任务分解执行。

        将大计划拆分为 2-3 个子任务组，每组独立执行后汇总。
        每个子任务有独立的上下文窗口，避免信息过载。
        """
        steps = plan.steps
        total = len(steps)
        # 拆分为 2-3 组
        group_size = max(3, total // 3 + 1)
        groups: list[list[PlanStep]] = []
        for i in range(0, total, group_size):
            groups.append(steps[i:i + group_size])

        self._event(task, "execution.started",
                    f"复杂任务分解执行: {total} 步 → {len(groups)} 组",
                    f"每组 {group_size} 步",
                    {"total_steps": total, "groups": len(groups)})

        task.status = TaskStatus.EXECUTING
        task.updated_at = now_utc()
        all_observations: list[ToolObservation] = []
        all_outputs: list[str] = []
        pc = get_pause_controller(task.task_id)

        for gi, group in enumerate(groups):
            pc.check()
            group_label = f"组 {gi + 1}/{len(groups)}"
            first = group[0].index
            last = group[-1].index

            self._event(task, "dag.parallel",
                        f"执行 {group_label}: 步骤 {first}-{last}",
                        f"共 {len(group)} 步",
                        {"group": gi + 1, "total_groups": len(groups),
                         "step_range": [first, last]})

            # 构建子计划
            from copy import deepcopy
            sub_steps = [deepcopy(s) for s in group]
            # 调整索引（从1开始）
            for i, s in enumerate(sub_steps):
                s.index = i + 1
                s.depends_on = [d - first + 1 for d in s.depends_on
                               if first <= d <= last]

            sub_plan = ExecutionPlan(
                task_id=plan.task_id,
                mode=mode,
                goal=f"{plan.goal} — {group_label} (步骤{first}-{last})",
                steps=sub_steps,
                constraints=plan.constraints,
                success_criteria=plan.success_criteria,
            )

            # 注入前序组的输出作为上下文
            prior_context = "\n".join(all_outputs[-3:]) if all_outputs else ""
            if prior_context:
                self._event(task, "progress.update",
                            f"{group_label} 上下文",
                            f"前序组输出 {len(prior_context)} 字符",
                            {"group": gi + 1})

            # 执行子计划
            if mode == PlanMode.DAG:
                try:
                    # 为子计划设置正确的 depends_on
                    for s in sub_steps:
                        s.status = StepStatus.PENDING
                    # 使用简化的 DAG 执行
                    for s in sub_steps:
                        s.status = StepStatus.RUNNING
                        self._event(task, "step.started",
                                    f"{group_label}: {s.title}",
                                    s.objective, {"step_index": s.index, "group": gi + 1})
                        try:
                            obs, ans = await self._execute_step(
                                task, brief, s, all_observations,
                                prior_step_outputs=list(all_outputs))
                            if obs:
                                all_observations.append(obs)
                            if ans:
                                all_outputs.append(f"[{group_label} 步骤{s.index}] {ans}")
                            s.status = StepStatus.COMPLETED
                            self._remember_step(task, s, ans or "")
                            self._event(task, "step.completed",
                                        f"{group_label}: {s.title}",
                                        f"组 {gi + 1} 步骤 {s.index} 完成",
                                        {"group": gi + 1, "step_index": s.index})
                        except ApprovalRequiredError as exc:
                            self._record_approval_request(task, s, exc)
                            task.status = TaskStatus.WAITING_APPROVAL
                            task.failed_reason = f"工具调用需要用户审批: {exc.tool_name}"
                            task.updated_at = now_utc()
                            return task
                        except StepFailedError as exc:
                            s.status = StepStatus.FAILED
                            self._event(task, "step.failed",
                                        f"{group_label}: {s.title}",
                                        str(exc),
                                        {"group": gi + 1, "step_index": s.index},
                                        severity="ERROR")
                            all_outputs.append(
                                f"[{group_label} 步骤{s.index} 失败] {s.title}")
                        except TaskCancelledError:
                            return self._cancel_task(task)
                except Exception as exc:
                    logger.warning("Sub-plan group %d failed: %s", gi + 1, exc)
            else:
                # LINEAR mode for sub-plan
                for s in sub_steps:
                    s.status = StepStatus.RUNNING
                    self._event(task, "step.started",
                                f"{group_label}: {s.title}",
                                s.objective, {"step_index": s.index, "group": gi + 1})
                    try:
                        obs, ans = await self._execute_step(
                            task, brief, s, all_observations,
                            prior_step_outputs=list(all_outputs))
                        if obs:
                            all_observations.append(obs)
                        if ans:
                            all_outputs.append(f"[{group_label} 步骤{s.index}] {ans}")
                        s.status = StepStatus.COMPLETED
                        self._remember_step(task, s, ans or "")
                        self._event(task, "step.completed",
                                    f"{group_label}: {s.title}",
                                    f"组 {gi + 1} 步骤 {s.index} 完成",
                                    {"group": gi + 1, "step_index": s.index})
                    except ApprovalRequiredError as exc:
                        self._record_approval_request(task, s, exc)
                        task.status = TaskStatus.WAITING_APPROVAL
                        task.failed_reason = f"工具调用需要用户审批: {exc.tool_name}"
                        task.updated_at = now_utc()
                        return task
                    except StepFailedError as exc:
                        s.status = StepStatus.FAILED
                        self._event(task, "step.failed",
                                    f"{group_label}: {s.title}",
                                    str(exc),
                                    {"group": gi + 1, "step_index": s.index},
                                    severity="ERROR")
                        all_outputs.append(
                            f"[{group_label} 步骤{s.index} 失败] {s.title}")
                    except TaskCancelledError:
                        return self._cancel_task(task)

            self._event(task, "dag.parallel",
                        f"{group_label} 完成",
                        f"步骤 {first}-{last} 执行完毕",
                        {"group": gi + 1})

        # 汇总
        final = "\n\n".join(all_outputs) if all_outputs else "(无文本输出)"
        self._event(task, "task.completed",
                    f"复杂任务完成 ({len(groups)} 组/{total} 步骤)",
                    final,
                    {"groups": len(groups), "total_steps": total,
                     "tool_calls": len(all_observations), "output": final})
        task.status = TaskStatus.COMPLETED
        task.completed_at = now_utc()
        task.updated_at = now_utc()
        return task

    async def _execute_dag(
        self,
        task: Task,
        brief: TaskBrief,
        plan: ExecutionPlan,
    ) -> Task:
        """DAG 模式：按依赖拓扑排序，独立步骤并行执行。"""
        self._event(task, "execution.started", "开始 DAG 执行",
                     f"共 {len(plan.steps)} 个步骤")

        task.status = TaskStatus.EXECUTING
        task.updated_at = now_utc()
        all_observations: list[ToolObservation] = []
        step_outputs: list[str] = []
        completed: set[int] = set()

        steps_by_index = {s.index: s for s in plan.steps}
        remaining = set(steps_by_index.keys())

        while remaining:
            # 找出所有依赖已满足的步骤
            ready = [
                idx for idx in remaining
                if all(dep in completed for dep in steps_by_index[idx].depends_on)
            ]

            if not ready:
                # 死锁或全部完成
                break

            self._event(task, "dag.parallel", f"并行执行 {len(ready)} 个步骤",
                         f"步骤: {ready}",
                         {"parallel_steps": ready})

            # 并行执行 — 每个步骤接收当前已完成的文本输出作为上下文
            async def _run_step(idx: int) -> tuple[int, ToolObservation | None, str]:
                step = steps_by_index[idx]
                self._event(task, "step.started", f"DAG: {step.title}",
                             step.objective, {"step_index": idx})
                try:
                    obs, ans = await self._execute_step(
                        task, brief, step, all_observations,
                        prior_step_outputs=list(step_outputs))
                    step.status = StepStatus.COMPLETED
                    self._remember_step(task, step, ans or "")
                    self._event(task, "step.completed", f"DAG完成: {step.title}",
                                 f"步骤 {idx} 完成", {"step_index": idx})
                    return idx, obs, ans
                except StepFailedError as exc:
                    step.status = StepStatus.FAILED
                    self._event(task, "step.failed", f"DAG失败: {step.title}",
                                 str(exc), {"step_index": idx}, severity="ERROR")
                    return idx, None, ""
                except ApprovalRequiredError as exc:
                    self._record_approval_request(task, step, exc)
                    return idx, None, "__APPROVAL__"

            results = await asyncio.gather(
                *[_run_step(idx) for idx in ready],
                return_exceptions=True,
            )

            for result in results:
                if isinstance(result, Exception):
                    continue
                idx, obs, ans = result
                if ans == "__APPROVAL__":
                    task.status = TaskStatus.WAITING_APPROVAL
                    task.failed_reason = "工具调用需要用户审批"
                    task.updated_at = now_utc()
                    return task
                if obs:
                    all_observations.append(obs)
                # 添加输出（即使步骤失败也记录，供后续步骤参考）
                if ans:
                    step_outputs.append(f"[步骤{idx}] {ans}")

                # ── DAG 部分失败恢复 ──
                step_obj = steps_by_index[idx]
                if step_obj.status == StepStatus.FAILED:
                    # 标记已处理（释放依赖它的步骤）
                    step_outputs.append(
                        f"[步骤{idx} 失败] {step_obj.title}: "
                        f"该步骤已失败，后续依赖此步骤的步骤可能无法正常执行")
                    # 跳过依赖此失败步骤的后续步骤
                    for remaining_idx in list(remaining):
                        rs = steps_by_index.get(remaining_idx)
                        if rs and idx in rs.depends_on:
                            rs.status = StepStatus.SKIPPED
                            step_outputs.append(
                                f"[步骤{remaining_idx} 跳过] {rs.title}: "
                                f"因为依赖的步骤 {idx} 失败")
                            remaining.discard(remaining_idx)
                            self._event(task, "step.skipped",
                                        f"DAG跳过: {rs.title}",
                                        f"依赖步骤 {idx} 失败",
                                        {"step_index": remaining_idx,
                                         "failed_dependency": idx})

                completed.add(idx)
                remaining.discard(idx)

        # 统计
        succeeded = sum(1 for s in plan.steps if s.status == StepStatus.COMPLETED)
        failed = sum(1 for s in plan.steps if s.status == StepStatus.FAILED)
        skipped = sum(1 for s in plan.steps if s.status == StepStatus.SKIPPED)

        final = "\n\n".join(step_outputs) if step_outputs else "(无文本输出)"
        if failed > 0:
            self._event(task, "task.completed",
                        f"DAG完成（部分失败: {failed}失败/{skipped}跳过/{succeeded}成功）",
                        final,
                        {"succeeded": succeeded, "failed": failed,
                         "skipped": skipped, "output": final})
            task.status = TaskStatus.COMPLETED  # 部分完成也算完成
            task.failed_reason = None  # 清除可能的失败原因
        else:
            self._event(task, "task.completed", "DAG任务完成", final,
                         {"steps_completed": len(completed),
                          "tool_calls": len(all_observations),
                          "output": final})
            task.status = TaskStatus.COMPLETED
        task.completed_at = now_utc()
        task.updated_at = now_utc()
        return task

    async def _execute_iterative(
        self,
        task: Task,
        brief: TaskBrief,
        plan: ExecutionPlan,
    ) -> Task:
        """ITERATIVE 模式：重复执行步骤直到满足停止条件或达到最大迭代次数。"""
        max_iter = getattr(plan, 'max_iterations', None) or 5
        stop_cond = getattr(plan, 'stop_condition', None) or "任务目标达成"

        self._event(task, "execution.started", "开始迭代执行",
                     f"最大 {max_iter} 轮，停止条件: {stop_cond}")

        task.status = TaskStatus.EXECUTING
        task.updated_at = now_utc()
        all_observations: list[ToolObservation] = []
        step_outputs: list[str] = []

        for iteration in range(1, max_iter + 1):
            self._event(task, "iteration.started", f"第 {iteration}/{max_iter} 轮迭代",
                         f"目标: {plan.goal}",
                         {"iteration": iteration, "max": max_iter})

            for step in plan.steps:
                if step.status == StepStatus.SKIPPED:
                    continue

                # 每轮重新设置步骤状态
                step.status = StepStatus.RUNNING
                self._event(task, "step.started", f"迭代{iteration}: {step.title}",
                             step.objective,
                             {"iteration": iteration, "step_index": step.index})

                try:
                    obs, answer = await self._execute_step(
                        task, brief, step, all_observations,
                        prior_step_outputs=list(step_outputs))
                    if obs:
                        all_observations.append(obs)
                    if answer:
                        step_outputs.append(answer)
                    step.status = StepStatus.COMPLETED
                    self._remember_step(task, step, answer or "")
                    self._event(task, "step.completed", f"迭代{iteration}: {step.title}",
                                 f"轮次 {iteration} 步骤 {step.index} 完成",
                                 {"iteration": iteration, "step_index": step.index})
                except StepFailedError as exc:
                    self._event(task, "step.failed", f"失败: {step.title}",
                                 str(exc), {"iteration": iteration}, severity="WARN")
                    # 单步骤失败不中断整个迭代，继续下一步
                    continue
                except ApprovalRequiredError as exc:
                    self._record_approval_request(task, step, exc)
                    task.status = TaskStatus.WAITING_APPROVAL
                    task.failed_reason = f"需要用户审批: {exc.tool_name} - {exc.reason}"
                    task.updated_at = now_utc()
                    self._event(task, "approval.requested", "需要审批",
                                 f"工具 {exc.tool_name} 需要用户确认",
                                 {"step_index": step.index, "tool_name": exc.tool_name})
                    return task

            # 检查停止条件：用 LLM 判断是否应该停止
            should_stop = await self._check_iteration_stop(
                task, brief, plan, all_observations, iteration)
            if should_stop:
                self._event(task, "iteration.completed", "迭代完成",
                             f"停止条件满足，共 {iteration} 轮")
                break

        final = "\n\n".join(step_outputs[-3:]) if step_outputs else "(无文本输出)"
        self._event(task, "task.completed", "任务已完成", final,
                     {"iterations": iteration, "tool_calls": len(all_observations),
                      "output": final})
        task.status = TaskStatus.COMPLETED
        task.completed_at = now_utc()
        task.updated_at = now_utc()
        return task

    async def _check_iteration_stop(
        self,
        task: Task,
        brief: TaskBrief,
        plan: ExecutionPlan,
        observations: list[ToolObservation],
        iteration: int,
    ) -> bool:
        """用 LLM 判断 ITERATIVE 模式是否应停止。"""
        if not self.model_gateway.is_live():
            return iteration >= 3  # dev 模式默认 3 轮

        obs_summary = "\n".join(
            f"- {o.output_summary}" for o in observations[-4:])
        prompt = f"""任务: {task.objective}
停止条件: {getattr(plan, 'stop_condition', '目标达成')}
已完成 {iteration} 轮迭代，最近观察:
{obs_summary}

请判断是否应该停止迭代。只回答 YES 或 NO。"""

        try:
            result = await self.model_gateway.generate_text(prompt)
            return "YES" in result.upper()
        except Exception:
            return False

    async def _execute_step(
        self,
        task: Task,
        brief: TaskBrief,
        step: PlanStep,
        prior_observations: list[ToolObservation],
        prior_step_outputs: list[str] | None = None,
    ) -> tuple[ToolObservation | None, str]:
        """执行单个步骤的 Agent 循环。带失败分类、智能重试、暂停/取消支持。

        返回 (observation, step_answer)。
        """
        tid = task.task_id
        pc = get_pause_controller(task.task_id)

        # 步骤级重试循环
        last_error: FailureInfo | None = None
        for step_attempt in range(self.MAX_STEP_RETRIES + 1):
            if step_attempt > 0:
                msg = last_error.user_message if last_error else "unknown"
                trace.retry(tid, step.index, step_attempt, self.MAX_STEP_RETRIES, msg[:120])
                self._event(task, "step.retry",
                            f"步骤 {step.index} 重试 ({step_attempt}/{self.MAX_STEP_RETRIES})",
                            f"上次失败: {msg}",
                            {"step_index": step.index, "attempt": step_attempt})
                step.retry_count = step_attempt

            try:
                obs, answer = await self._execute_step_once(
                    task, brief, step, prior_observations, pc, prior_step_outputs)
                if step_attempt > 0:
                    trace.info(tid, f"_execute_step.retry_ok",
                              f"步骤{step.index}重试{step_attempt}成功")
                return obs, answer
            except ApprovalRequiredError:
                # 审批需求是终端错误，不重试，直接向上传播
                raise
            except TaskCancelledError:
                raise
            except StepFailedError as exc:
                last_error = exc.failure_info if hasattr(exc, 'failure_info') else classify_exception(exc)
                trace.warn(tid, "_execute_step.failed",
                          f"步骤{step.index} attempt={step_attempt}: type={last_error.failure_type.value} terminal={last_error.is_terminal}")
                if last_error.is_terminal:
                    raise
                if last_error.can_fallback_model and step_attempt == self.MAX_STEP_RETRIES - 1:
                    self._event(task, "step.fallback",
                                f"步骤 {step.index} 使用回退",
                                "LLM 不可用，使用 dev 回退",
                                {"step_index": step.index})
                    fallback = self._dev_fallback(step.objective)
                    result = self.completion_checker.check_step(step, fallback)
                    if result.is_complete:
                        return None, fallback
                continue
            except Exception as exc:
                last_error = classify_exception(exc, f"step {step.index}")
                trace.warn(tid, "_execute_step.error",
                          f"步骤{step.index} attempt={step_attempt}: {type(exc).__name__}: {exc}")
                if step_attempt >= self.MAX_STEP_RETRIES:
                    raise StepFailedError(last_error)
                continue

        raise StepFailedError(
            FailureInfo(FailureType.STEP_LIMIT,
                        f"步骤 {step.index} 重试耗尽 ({self.MAX_STEP_RETRIES}次)"))

    async def _execute_step_once(
        self,
        task: Task,
        brief: TaskBrief,
        step: PlanStep,
        prior_observations: list[ToolObservation],
        pc: PauseController,
        prior_step_outputs: list[str] | None = None,
    ) -> tuple[ToolObservation | None, str]:
        """单次步聚执行 (不含步骤级重试). 含死循环检测."""
        tid = task.task_id
        tools_def = self._build_tools_prompt()
        last_observation: ToolObservation | None = None
        step_answer = ""
        all_observations: list[ToolObservation] = list(prior_observations)

        trace.debug(tid, "_execute_step_once",
                   f"步骤{step.index}开始, action_type={step.action_type}, execution_mode={getattr(step, 'execution_mode', 'tool')}, max_rounds={self.MAX_TOOL_ROUNDS_PER_STEP}")

        # ── Phase 1-2: Execution Mode Routing ──────────────────
        execution_mode = getattr(step, "execution_mode", "tool")

        # Mode: skill — execute deterministic script directly (no LLM loop)
        if execution_mode == "skill" and self._skill_registry:
            skill_name = getattr(step, "skill_name", None)
            skill_params = getattr(step, "skill_params", {})
            if skill_name:
                return await self._execute_skill_step(
                    task, brief, step, skill_name, skill_params)

        # Mode: dynamic_script — Mini ReAct: generate→check→execute→validate→retry
        if execution_mode == "dynamic_script":
            return await self._execute_dynamic_script_step(
                task, brief, step, prior_step_outputs)

        # ── 死循环检测 (增强版) ──
        _last_tool = ""; _last_input_hash = 0; _last_status = ""
        _same_failures = 0; _MAX_SAME_FAILURES = 3
        # Use instance-level tracking for cross-step failure detection
        _tool_fail_streak = self._tool_fail_streak

        round_times: list[float] = []

        # ── Evaluate thinking mode for this step ────────────
        from flowcraft_core.intent.thinking_evaluator import evaluate_step_thinking
        self._current_thinking_mode = evaluate_step_thinking(step, getattr(brief, 'thinking_mode', 'disabled'))
        trace.debug(tid, "_execute_step_once.thinking",
                   f"步骤{step.index} thinking_mode={self._current_thinking_mode} (task={getattr(brief, 'thinking_mode', 'disabled')})")

        for round_idx in range(self.MAX_TOOL_ROUNDS_PER_STEP):
            round_t0 = _time.monotonic()
            # 检查暂停/取消
            pc.check()

            trace.debug(tid, "_execute_step_once.round",
                       f"步骤{step.index} 轮次{round_idx+1}/{self.MAX_TOOL_ROUNDS_PER_STEP}")

            context = self._build_context(task, brief, step, all_observations, prior_step_outputs)
            prompt = self._build_step_prompt(context, tools_def, round_idx, step=step)

            trace.debug(tid, "_execute_step_once.context",
                       f"步骤{step.index} 上下文长度: {len(context)}字符, prompt长度: {len(prompt)}字符")

            # LLM 决策 (带 MODEL_ERROR 重试)
            decision = await self._llm_decide_with_retry(task, prompt)

            round_llm_elapsed = _time.monotonic() - round_t0

            action = decision.get("action", "final_answer")

            if action == "final_answer":
                answer_text = decision.get("final_answer", "")
                if not answer_text or len(answer_text.strip()) < 5:
                    answer_text = decision.get("reasoning", "")
                # Strip meta-reasoning from output
                answer_text = self._sanitize_output(answer_text)
                step_answer = answer_text
                self._event(task, "step.answer", f"步骤输出: {step.title}",
                             answer_text,
                             {"round": round_idx, "decision": action})
                trace.info(tid, "_execute_step_once.final_answer",
                          f"步骤{step.index} round{round_idx} → final_answer ({round_llm_elapsed:.2f}s LLM)",
                          extra={"answer_len": len(answer_text), "round": round_idx})

                # CompletionChecker: 验证输出质量
                # Skip quality re-loop for known fallback/error messages — retrying won't help
                if not self._is_likely_fallback(answer_text):
                    result = self.completion_checker.check_step(step, answer_text)
                    if result.needs_replan:
                        self._event(task, "step.incomplete",
                                    f"步骤 {step.index} 输出质量不足",
                                    f"质量分: {result.quality_score:.2f}",
                                    {"step_index": step.index, "quality": result.quality_score})
                        trace.warn(tid, "_execute_step_once.incomplete",
                                  f"步骤{step.index} 输出质量不足: score={result.quality_score:.2f}")
                        if round_idx < self.MAX_TOOL_ROUNDS_PER_STEP - 1:
                            continue  # 再给一次机会
                return last_observation, step_answer

            if action == "ask_user":
                question = decision.get("final_answer", "")
                if not question or len(question.strip()) < 5:
                    reasoning = decision.get("reasoning", "")
                    question = self._make_clarification_question(
                        step.objective, reasoning, task.objective)
                else:
                    question = self._sanitize_output(question)
                step_answer = question
                self._event(task, "step.answer", "LLM 需要澄清",
                             question, {"round": round_idx})
                trace.info(tid, "_execute_step_once.ask_user",
                          f"步骤{step.index} round{round_idx} → ask_user ({round_llm_elapsed:.2f}s LLM)")
                return last_observation, step_answer

            if action == "tool_call":
                tool_name = decision.get("tool_name", "")
                raw_input = decision.get("tool_input", {})
                purpose = decision.get("purpose", f"调用 {tool_name}")

                trace.tool_call(tid, step.index, round_idx, tool_name, purpose,
                               extra={"raw_input_keys": list(raw_input.keys()) if raw_input else []})

                if not tool_name:
                    trace.warn(tid, "_execute_step_once.no_tool",
                              f"步骤{step.index} round{round_idx} 缺少tool_name")
                    self._event(task, "step.reasoning", "LLM 决策不完整",
                                 f"缺少 tool_name，跳过", {"round": round_idx},
                                 severity="WARN")
                    continue

                # ── 工具级连续失败检测（跨步骤）：同一工具连续失败N次 → 强制跳过 ──
                fail_streak = _tool_fail_streak.get(tool_name, 0)
                if fail_streak >= self.MAX_TOOL_FAIL_STREAK:
                    trace.warn(tid, "tool.blocked",
                              f"工具 {tool_name} 已连续失败 {fail_streak} 次，本轮强制跳过",
                              extra={"tool": tool_name, "fail_streak": fail_streak})
                    self._event(task, "step.reasoning",
                               f"工具 {tool_name} 连续失败 {fail_streak} 次，已跳过。请尝试其他方法。",
                               f"tool={tool_name} streak={fail_streak}",
                               {"round": round_idx}, severity="WARN")
                    continue

                # ── 死循环检测：同一工具+同一参数+连续失败 ──
                _hash = hash(str(sorted(raw_input.items())) if raw_input else "")
                if tool_name == _last_tool and _hash == _last_input_hash and _last_status == "FAILED":
                    _same_failures += 1
                    trace.loop_detect(tid, step.index, round_idx, tool_name, _same_failures)
                    if _same_failures >= _MAX_SAME_FAILURES:
                        self._event(task, "step.reasoning", "检测到死循环",
                                     f"工具 {tool_name} 连续失败 {_same_failures} 次，强制终止",
                                     {"round": round_idx}, severity="ERROR")
                        return last_observation, (
                            f"工具 {tool_name} 连续失败 {_same_failures} 次，已自动终止。\n"
                            + "\n".join(f"- {o.output_summary[:200]}"
                                        for o in all_observations[-3:]))
                else:
                    _same_failures = 0
                _last_tool = tool_name
                _last_input_hash = _hash

                tool_input = self._resolve_tool_input(
                    tool_name, raw_input, task.objective, brief)

                self._event(task, "tool.requested", f"请求工具: {tool_name}",
                             purpose,
                             {"round": round_idx, "tool_name": tool_name,
                              "tool_input": tool_input})

                intent = ToolIntent(
                    task_id=task.task_id,
                    step_id=step.step_id,
                    tool_name=tool_name,
                    purpose=purpose,
                    input_summary=f"{tool_name}: {purpose}",
                    input_payload=tool_input,
                    expected_result=decision.get("reasoning", f"Execute {tool_name}"),
                    risk_level=self._tool_risk(tool_name),
                    requires_approval=self._tool_needs_approval(tool_name),
                )

                # 工具调用 (带 TOOL_ERROR 重试)
                tool_t0 = _time.monotonic()
                try:
                    observation = await self._tool_invoke_with_retry(intent, task)
                except StepFailedError:
                    observation = ToolObservation(
                        tool_intent_id=intent.tool_intent_id,
                        task_id=task.task_id,
                        step_id=step.step_id,
                        status="FAILED",
                        output_summary=f"工具 {tool_name} 执行失败",
                        error_message="工具重试耗尽",
                    )
                tool_elapsed = _time.monotonic() - tool_t0

                trace.tool_result(tid, step.index, round_idx, tool_name,
                                 observation.status,
                                 observation.output_summary[:120],
                                 elapsed_s=tool_elapsed,
                                 extra={"tool_elapsed": tool_elapsed})

                # 记录工具状态用于死循环检测
                _last_status = observation.status

                # ── 工具级连续失败跟踪 ──
                if observation.status == "FAILED":
                    _tool_fail_streak[tool_name] = _tool_fail_streak.get(tool_name, 0) + 1
                elif observation.status == "SUCCESS":
                    _tool_fail_streak[tool_name] = 0  # 成功则重置

                # 处理审批
                if observation.status == "WAITING_APPROVAL":
                    trace.approval_wait(tid, step.index, tool_name, observation.output_summary[:120])
                    raise ApprovalRequiredError(tool_name, observation.output_summary)

                self._event(task, "tool.completed", f"工具完成: {tool_name}",
                             observation.output_summary,
                             {"round": round_idx,
                              "tool_name": tool_name,
                              "status": observation.status,
                              "output": observation.output_payload})

                last_observation = observation

                if observation.status == "FAILED":
                    self._event(task, "step.reasoning", "工具失败，反馈LLM重试",
                                 observation.error_message or observation.output_summary,
                                 {"round": round_idx}, severity="WARN")
                    continue

                if observation.status == "DENIED":
                    # 权限不足 — 尝试自动授权路径，否则请求用户
                    denied_path = observation.output_payload.get("denied_path", "")
                    action = observation.output_payload.get("action", "")

                    # Auto-grant: if denied_path is in workspace vicinity, auto-add
                    if denied_path and self._settings:
                        try:
                            denied = Path(denied_path)
                            if denied.exists() or denied.parent.exists():
                                added = self._settings.add_allowed_path(
                                    denied if denied.is_dir() else denied.parent)
                                if added:
                                    self._event(task, "permission.granted",
                                                 f"Auto-granted access to: {denied.parent if denied.is_file() else denied}",
                                                 f"Path added to allowed_paths",
                                                 {"path": str(denied), "round": round_idx})
                                    # Retry the tool immediately
                                    continue
                        except Exception:
                            pass

                    # If not auto-granted, ask user
                    self._event(task, "step.reasoning", "权限不足，请求用户授权",
                                 observation.output_summary, {"round": round_idx}, severity="WARN")
                    question = self._make_permission_request(
                        tool_name, observation.output_summary, task.objective)
                    step_answer = question
                    self._event(task, "step.answer", "需要用户授权",
                                 question, {"round": round_idx})
                    return last_observation, step_answer

                all_observations.append(observation)
                round_times.append(_time.monotonic() - round_t0)
                continue

        # 超过最大轮数
        trace.error(tid, "_execute_step_once.limit",
                   f"步骤{step.index} 超过最大轮数({self.MAX_TOOL_ROUNDS_PER_STEP})! 各轮耗时: {[f'{t:.2f}s' for t in round_times]}")
        raise StepFailedError(
            FailureInfo(FailureType.STEP_LIMIT,
                        f"步骤超过最大工具调用轮数 ({self.MAX_TOOL_ROUNDS_PER_STEP})"))

    # ══════════════════════════════════════════════════════════
    # Phase 1: Skill Execution
    # ══════════════════════════════════════════════════════════

    async def _execute_skill_step(
        self,
        task: Task,
        brief: TaskBrief,
        step: PlanStep,
        skill_name: str,
        skill_params: dict,
    ) -> tuple[ToolObservation | None, str]:
        """Execute a deterministic skill script directly (no LLM loop).

        Skills are pre-written, tested scripts — they run deterministically.
        The agent context from SKILL.md is injected into the step context
        for the LLM to understand the result, but no LLM decision loop is needed.
        """
        tid = task.task_id
        self._event(task, "step.started",
                     f"技能执行: {skill_name}",
                     f"参数: {skill_params}",
                     {"step_index": step.index, "skill": skill_name})

        try:
            # Resolve skill name (support qualified, category.name, and simple names)
            manifest = self._skill_registry.resolve_skill(skill_name)
            resolved_name = manifest.definition.qualified_name if manifest else skill_name

            result = await self._skill_registry.execute_skill(
                skill_name, params=skill_params)

            if result.is_success:
                # Get agent context for this skill to help LLM understand results
                skill_context = self._skill_registry.get_agent_context(resolved_name) or ""
                step_answer = (
                    f"## 技能执行结果: {resolved_name}\n"
                    f"耗时: {result.elapsed_seconds:.2f}s\n"
                    f"状态: {result.status}\n\n"
                    f"{result.output}\n\n"
                )
                if skill_context:
                    step_answer += f"\n## 技能说明\n{skill_context[:1000]}"

                self._event(task, "step.completed",
                             f"技能完成: {resolved_name}",
                             step_answer,
                             {"step_index": step.index, "skill": resolved_name,
                              "elapsed": result.elapsed_seconds})

                return None, step_answer
            else:
                error_msg = result.error or "Skill execution failed"
                self._event(task, "step.failed",
                             f"技能失败: {resolved_name}",
                             error_msg,
                             {"step_index": step.index}, severity="ERROR")
                return None, f"技能 {resolved_name} 执行失败: {error_msg}"

        except Exception as exc:
            self._event(task, "step.failed",
                         f"技能异常: {skill_name}",
                         str(exc),
                         {"step_index": step.index}, severity="ERROR")
            raise StepFailedError(
                FailureInfo(FailureType.TOOL_ERROR,
                            f"Skill {skill_name} failed: {exc}"))

    # ══════════════════════════════════════════════════════════
    # Phase 2: Dynamic Script Execution (Mini ReAct)
    # ══════════════════════════════════════════════════════════

    async def _execute_dynamic_script_step(
        self,
        task: Task,
        brief: TaskBrief,
        step: PlanStep,
        prior_step_outputs: list[str] | None = None,
    ) -> tuple[ToolObservation | None, str]:
        """Execute a dynamic (LLM-generated) script with Mini ReAct loop.

        Flow: LLM-generate → safety-check → sandbox-execute → validate → retry×3.
        On success, the script is auto-saved as an agent_generated skill (Phase 3).
        """
        tid = task.task_id
        self._event(task, "step.started",
                     f"动态脚本执行: {step.title}",
                     step.objective,
                     {"step_index": step.index, "mode": "dynamic_script"})

        # Lazy init DynamicScriptExecutor
        if self._dynamic_script_executor is None:
            from flowcraft_core.skills.dynamic_executor import DynamicScriptExecutor
            self._dynamic_script_executor = DynamicScriptExecutor(
                model_gateway=self.model_gateway,
                skill_registry=self._skill_registry,
            )

        # Build prior context from completed steps
        prior_context = ""
        if prior_step_outputs:
            prior_context = "\n".join(prior_step_outputs[-3:])

        result = await self._dynamic_script_executor.execute(
            task_id=task.task_id,
            step_id=step.step_id,
            step=step,
            prior_context=prior_context,
        )

        if result.status == "SUCCESS":
            step_answer = (
                f"## 动态脚本执行结果\n"
                f"尝试次数: {result.attempts}\n"
                f"总耗时: {result.total_elapsed:.2f}s\n\n"
                f"{result.output}\n"
            )
            if result.saved_as_skill:
                step_answer += f"\n> 💡 此脚本已自动保存为技能: `{result.saved_as_skill}`，"
                step_answer += "下次可直接使用 skill 模式执行。"

            self._event(task, "step.completed",
                         f"动态脚本完成: {step.title}",
                         step_answer,
                         {"step_index": step.index, "attempts": result.attempts,
                          "elapsed": result.total_elapsed,
                          "saved_as_skill": result.saved_as_skill})

            return None, step_answer
        else:
            error_msg = result.error or "Dynamic script execution failed"
            if result.status == "MAX_RETRIES":
                error_msg = f"超过最大重试次数 ({result.attempts}次): {error_msg}"
            elif result.status == "SAFETY_DENIED":
                error_msg = f"安全策略拒绝: {error_msg}"

            self._event(task, "step.failed",
                         f"动态脚本失败: {step.title}",
                         error_msg,
                         {"step_index": step.index, "status": result.status},
                         severity="ERROR")

            raise StepFailedError(
                FailureInfo(FailureType.TOOL_ERROR, error_msg))

    async def _llm_decide_with_retry(self, task: Task, prompt: str) -> dict[str, Any]:
        """LLM decision with MODEL_ERROR retry."""
        tid = task.task_id
        return await retry_with_backoff(
            self._llm_decide, prompt,
            failure_info=FailureInfo(FailureType.MODEL_ERROR, "LLM call"),
            on_retry=lambda fi: [
                trace.warn(tid, "llm.retry",
                          f"LLM retry {fi.retry_count}: {fi.message[:100]}"),
                self._event(
                    task, "model.retry", "Model retry",
                    f"Retry {fi.retry_count}: {fi.message}",
                    {"retry_count": fi.retry_count}, severity="WARN"),
            ],
        )


    async def _tool_invoke_with_retry(
        self, intent: ToolIntent, task: Task
    ) -> ToolObservation:
        """Tool invocation with TOOL_ERROR auto-retry."""
        tid = task.task_id
        trace.debug(tid, "tool.invoke_begin",
                   f"Preparing: {intent.tool_name}", extra={"tool_name": intent.tool_name})
        return await retry_with_backoff(
            self.tool_harness.invoke, intent,
            session_id=task.session_id,
            failure_info=FailureInfo(
                FailureType.TOOL_ERROR, f"Tool call {intent.tool_name}"),
            on_retry=lambda fi: [
                trace.warn(tid, "tool.retry",
                          f"Tool retry {fi.retry_count}: {intent.tool_name} - {fi.message[:100]}"),
                self._event(
                    task, "tool.retry", "Tool retry",
                    f"Retry {fi.retry_count}: {fi.message}",
                    {"retry_count": fi.retry_count}, severity="WARN"),
            ],
        )

    async def _llm_decide(self, prompt: str) -> dict[str, Any]:
        """Call LLM for structured decision (with 30s timeout)."""
        import asyncio as _asyncio
        if not self.model_gateway.is_live():
            return {"action": "final_answer",
                    "reasoning": "Dev mode fallback",
                    "final_answer": self._dev_fallback(prompt)}

        # Extract task_id from prompt if possible (for trace correlation)
        tid_hint = ""
        try:
            import re as _re
            m = _re.search(r'task_([a-f0-9]{8})', prompt[:500])
            if m:
                tid_hint = f"task_{m.group(1)}"
        except Exception:
            pass

        # Adaptive timeout: larger prompts get more time
        prompt_size = len(prompt)
        if prompt_size > 8000:
            llm_timeout = 90.0
        elif prompt_size > 4000:
            llm_timeout = 60.0
        else:
            llm_timeout = 45.0

        # Output token budget: DeepSeek V4 Pro max_output_tokens = 384,000.
        # Previously limited to 1024–8192, which caused JSON truncation for
        # creative/writing steps where final_answer needs thousands of characters.
        # This is a ceiling, not a target — the model only generates what it needs.
        response_max_tokens = 384000  # DeepSeek V4 Pro max output

        t0 = _time.monotonic()
        trace.llm_call(tid_hint, "_llm_decide",
                      f"LLM decision start (timeout={llm_timeout}s, max_tokens={response_max_tokens}, prompt_len={prompt_size})")

        try:
            system_content = (
                self._identity_block() + "\n"
                "## DECISION RULES\n"
                "TASK FOCUS (MOST IMPORTANT): Your ONLY job is to complete the current step "
                "to advance the ORIGINAL TASK GOAL shown in the context. "
                "Do NOT explore unrelated files or directories. "
                "If the task is about creating a workflow/automation, design and explain the workflow - "
                "do NOT treat it as a file-exploration task.\n\n"
                "1. 'reasoning' is YOUR internal thought - NEVER expose it to the user.\n"
                "2. 'final_answer' is what the USER sees - output the actual result directly.\n"
                "   Do NOT write 'The current step is...' or 'Based on history...'.\n"
                "3. When action=ask_user: output a clear question to the user.\n"
                "4. If you have tool results above, summarize them in final_answer NOW.\n"
                "Respond ONLY with valid JSON matching the schema."
            )
            messages = [
                {"role": "system", "content": system_content},
                {"role": "user", "content": prompt},
            ]
            trace.debug(tid_hint, "_llm_decide.call",
                       f"Calling adapter.structured_chat, prompt_len={prompt_size}, thinking={self._current_thinking_mode}")

            result = await _asyncio.wait_for(
                self.model_gateway._adapter.structured_chat(
                    messages, STEP_DECISION_SCHEMA,
                    temperature=0.1, max_tokens=response_max_tokens,
                    thinking={"type": self._current_thinking_mode},
                ),
                timeout=llm_timeout,
            )
            elapsed = _time.monotonic() - t0
            trace.llm_result(tid_hint, "_llm_decide",
                           f"LLM returned ({elapsed:.2f}s): action={result.get('action')}",
                           elapsed_s=elapsed)
            return result
        except _asyncio.TimeoutError:
            elapsed = _time.monotonic() - t0
            trace.llm_timeout(tid_hint, "_llm_decide",
                            f"LLM decision timeout ({elapsed:.2f}s > {llm_timeout}s), propagating for retry",
                            elapsed_s=elapsed)
            logger.warning("LLM decision timed out after %.1fs (prompt=%d chars)", elapsed, prompt_size)
            raise  # Propagate → retry_with_backoff will retry, not swallow as success
        except Exception as exc:
            elapsed = _time.monotonic() - t0
            trace.error(tid_hint, "_llm_decide.error",
                       f"LLM decision failed ({elapsed:.2f}s): {type(exc).__name__}: {exc}",
                       elapsed_s=elapsed)
            logger.warning("LLM decision failed: %s", exc)
            raise  # Propagate → retry_with_backoff will retry, not swallow as success

    # -- Replanning --

    async def _replan(
        self,
        task: Task,
        brief: TaskBrief,
        original_plan: ExecutionPlan,
        failed_step: PlanStep,
        error_msg: str,
    ) -> ExecutionPlan | None:
        """Generate alternative plan when a step fails."""
        prompt = f"""## Original Task
Goal: {task.objective}
Type: {brief.task_type}

## Failure Info
Failed step: {failed_step.title} (step {failed_step.index})
Failure reason: {error_msg}
Original plan: {json.dumps(original_plan.model_dump(mode='json'), ensure_ascii=False)[:1000]}

## Instructions
This step failed. Generate a new execution plan to solve this.
The new plan should:
1. Skip the failed step, use an alternative approach
2. Each step must have title, objective, action_type, expected_output, risk_level
3. No more than 4 steps

Return in JSON format matching ExecutionPlan schema."""

        try:
            result = await self.model_gateway._adapter.structured_chat(
                [
                    {"role": "system", "content": self._identity_block() + "\nYou are a planning agent. Generate alternative execution plans."},
                    {"role": "user", "content": prompt},
                ],
                {
                    "type": "object",
                    "properties": {
                        "mode": {"type": "string", "enum": ["DIRECT", "LINEAR"]},
                        "goal": {"type": "string"},
                        "steps": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "index": {"type": "integer"},
                                    "title": {"type": "string"},
                                    "objective": {"type": "string"},
                                    "action_type": {"type": "string", "enum": ["MODEL_ANSWER", "PREPARE", "TOOL", "OBSERVE", "FINALIZE"]},
                                    "required_tools": {"type": "array", "items": {"type": "string"}},
                                    "expected_output": {"type": "string"},
                                    "risk_level": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"]},
                                    "approval_required": {"type": "boolean"},
                                    "execution_mode": {"type": "string", "enum": ["tool", "skill", "dynamic_script", "model_answer"], "description": "How this step should be executed"},
                                    "skill_name": {"type": "string", "description": "Skill name for skill mode"},
                                    "skill_params": {"type": "object", "description": "Parameters for skill script"},
                                },
                                "required": ["index", "title", "objective", "action_type", "expected_output", "risk_level"],
                            },
                        },
                    },
                    "required": ["mode", "goal", "steps"],
                },
                temperature=0.2, max_tokens=2048,
            )
            steps = []
            for raw_step in result.get("steps", []):
                raw_step.setdefault("execution_mode", "tool")
                raw_step.setdefault("skill_name", None)
                raw_step.setdefault("skill_params", {})
                steps.append(PlanStep(**raw_step))
            return ExecutionPlan(
                task_id=task.task_id,
                mode=PlanMode(result.get("mode", "LINEAR")),
                goal=result.get("goal", task.objective),
                steps=steps,
            )
        except Exception as exc:
            logger.warning("Replan failed: %s", exc)
            return None

    # -- Tool Input Resolution --

    def _resolve_tool_input(
        self,
        tool_name: str,
        raw_input: dict,
        objective: str,
        brief: TaskBrief,
    ) -> dict:
        """Smart tool parameter completion: relative path resolution, defaults."""
        resolved = dict(raw_input)

        for key in ("path", "file_path", "cwd"):
            if key in resolved and resolved[key]:
                p = Path(resolved[key])
                if not p.is_absolute():
                    base_dir = self._extract_dir_from_text(objective)
                    if base_dir:
                        resolved[key] = str(base_dir / p)
                    else:
                        # Resolve against workspace, not cwd (source dir)
                        resolved[key] = str(self.workspace_dir / p)

        if tool_name == "file.read" and "path" in resolved:
            path_val = resolved["path"]
            if not os.path.isabs(path_val) and "/" not in path_val.replace("\\", "/").lstrip("./"):
                base_dir = self._extract_dir_from_text(objective)
                if base_dir:
                    resolved["path"] = str(base_dir / path_val)

        if tool_name == "file.write" and "content" not in resolved:
            content = self._extract_quoted(objective)
            if content:
                resolved["content"] = content

        return resolved

    @staticmethod
    def _extract_dir_from_text(text: str) -> Path | None:
        """Extract directory path from text (e.g. D:/work/FlowCraft)."""
        import re
        m = re.search(r'([A-Za-z]:[/\\][^\s,.;]+)', text)
        if m:
            p = Path(m.group(1))
            return p.parent if p.suffix else p
        return None

    @staticmethod
    def _extract_quoted(text: str) -> str | None:
        """Extract quoted content from text."""
        import re
        m = re.search(r'[""]([^""]+)[""]', text)
        if m:
            return m.group(1)
        return None

    # -- Multi-turn Memory --

    def _build_memory_context(
        self,
        task: Task,
        brief: TaskBrief,
        all_observations: list[ToolObservation],
    ) -> str:
        """Build cross-step dialogue memory summary."""
        parts: list[str] = []

        success_criteria = getattr(brief, 'success_criteria', None) or []
        constraints = getattr(brief, 'constraints', None) or []
        sc_text = "\n".join(f"- {c}" for c in success_criteria[:5]) if success_criteria else "(none)"
        ct_text = "\n".join(f"- {c}" for c in constraints[:5]) if constraints else "(none)"
        parts.append(
            f"## Original Task\n"
            f"**User request**: {task.title}\n"
            f"**Goal**: {task.objective}\n"
            f"**Type**: {brief.task_type}\n"
            f"**Success criteria**:\n{sc_text}\n"
            f"**Constraints**:\n{ct_text}"
        )

        if all_observations:
            parts.append("\n## Completed step tool results")
            for i, obs in enumerate(all_observations[-6:]):
                payload_str = json.dumps(obs.output_payload, ensure_ascii=False)
                if len(payload_str) > 1500:
                    payload_str = payload_str[:1500] + "...(truncated)"
                parts.append(
                    f"### Step{i+1} result [{obs.status}]\n"
                    f"Summary: {obs.output_summary}\n"
                    f"Content: {payload_str}"
                )

        return "\n".join(parts)

    # -- Prompt Building --

    def _build_step_prompt(
        self, context: str, tools_def: str, round_idx: int,
        step: PlanStep | None = None,
    ) -> str:
        rules = ""
        if round_idx > 0:
            rules = """
## Critical Rules (must follow)
- If a tool returned DENIED above: **immediately use ask_user to request permission**, directly ask "May I access directory XX?". Do NOT use final_answer to report failure. After user grants permission, you can continue.
- The tool results above already contain the **complete output** (file contents, command outputs, etc.).
- **You must immediately use final_answer to summarize these results and complete the task.**
- **DO NOT** call the same tool again - you already have the results!
- **DO NOT** call tools to "get more info" - all content has been provided above.
"""

        # Large output strategy: for steps that need to generate substantial text
        # (novels, reports, long code, etc.), route content through file tools
        # instead of stuffing everything into the JSON final_answer field.
        large_output_hint = ""
        if step and step.action_type == "MODEL_ANSWER":
            expected = step.expected_output or ""
            objective = step.objective or ""
            combined = (expected + objective).lower()
            long_form_keywords = [
                "小说", "文章", "报告", "novel", "article", "report",
                "代码", "脚本", "code", "script",
                "总结", "摘要", "summary",
                "万字", "千字", "字以", "words", "characters",
            ]
            is_long_form = any(kw in combined for kw in long_form_keywords)
            if is_long_form:
                large_output_hint = """
## LARGE OUTPUT STRATEGY (critical for this step)
This step requires generating a large amount of text that may exceed the JSON response limit.
**IMPORTANT — do NOT try to put all content in final_answer.** Instead:
1. Use **file.write** or **file.append** to save the FULL content to a file (e.g., `novel_output.md`)
2. Then use **final_answer** to give ONLY a brief summary (2-3 sentences) + the file path
3. The user will read the complete content from the saved file
4. Each file.write call can handle large content — break into multiple calls if needed
"""

        # Inject blocked tools into context so LLM avoids them
        blocked_tools = ""
        if self._tool_fail_streak:
            blocked = [name for name, count in self._tool_fail_streak.items()
                      if count >= self.MAX_TOOL_FAIL_STREAK]
            if blocked:
                blocked_tools = "\n## BLOCKED TOOLS (do NOT call these - they repeatedly failed)\n"
                blocked_tools += "\n".join(f"- **{t}**: failed {self._tool_fail_streak[t]} times, network may be unavailable"
                                          for t in blocked)
                blocked_tools += "\nUse your own knowledge or alternative tools instead.\n"

        return f"""## Task Context
{context}

## Available Tools
{tools_def}
{large_output_hint}
{blocked_tools}
{rules}
## Decision
Round {round_idx + 1} (max {self.MAX_TOOL_ROUNDS_PER_STEP}). Output your next action in JSON format:
- tool_call: call a tool (only when truly needed and results not already present above)
- final_answer: output the final user-facing result directly (DO NOT expose meta-reasoning like "The current step is...")
- ask_user: ask the user a clear question (DO NOT write "need clarification...", ask directly)
  IMPORTANT: When a tool returns DENIED, you MUST use ask_user to request permission, not final_answer to report failure

**CRITICAL - Remember the original task goal** (described in the task summary above).
The current step is part of the original task. Your output must serve the original task goal.

Note: 'reasoning' is your internal thought, 'final_answer' is user-facing output. Keep them strictly separate."""

    def _build_tools_prompt(self) -> str:
        defs = self.tool_registry.list_definitions()
        if not defs:
            return "(No tools available)"
        lines = []
        for d in defs:
            lines.append(f"- **{d['tool_name']}** ({d.get('risk_level', 'LOW')}): {d.get('description', '')}")
            lines.append(f"  Permissions: {', '.join(d.get('permissions', []))}")
        return "\n".join(lines)

    def _build_context(
        self,
        task: Task,
        brief: TaskBrief,
        step: PlanStep,
        previous_observations: list[ToolObservation],
        previous_step_outputs: list[str] | None = None,
    ) -> str:
        session_context = self._get_session_memory_context(task)

        compressed = self.context_compressor.compress(
            task_id=task.task_id,
            step_objective=f"Step {step.index}: {step.title}\nGoal: {step.objective}\nExpected output: {step.expected_output}",
            observations=previous_observations,
            previous_step_outputs=previous_step_outputs,
        )

        prior_text = ""
        if previous_step_outputs:
            # 保留完整的前序步骤输出，交给 smart_truncate 按优先级智能压缩
            prior_text = "## 已完成步骤的输出\n" + "\n".join(
                f"### 步骤 {i+1}\n{s}" for i, s in enumerate(previous_step_outputs[-8:])
            )

        # Always inject current date into context for time-sensitive steps
        now = datetime.now(timezone.utc)
        date_line = f"**Current date: {now.strftime('%Y-%m-%d')} ({now.strftime('%A')})**"

        parts = [
            session_context,
            self._build_memory_context(task, brief, previous_observations),
            date_line,
            prior_text,
            compressed.context_text,
        ]

        context = "\n".join(p for p in parts if p)

        from flowcraft_core.memory.context_summarizer import smart_truncate, get_context_budget
        budget = get_context_budget(self.model_gateway)
        if len(context) > budget:
            try:
                context = smart_truncate(context, max_chars=budget)
            except Exception:
                context = context[:budget] + "\n\n[...truncated]"
        return context

    # -- Helpers --

    def _build_final_answer(
        self, task: Task, observations: list[ToolObservation]
    ) -> str:
        if not observations:
            return f"Task '{task.objective}' completed (no tools called)."
        lines = [f"Task '{task.objective}' completed."]
        for obs in observations:
            lines.append(f"- {obs.tool_intent_id}: {obs.output_summary}")
        return "\n".join(lines)

    def _tool_risk(self, tool_name: str) -> RiskLevel:
        tool = self.tool_registry.get(tool_name)
        if tool:
            return tool.definition.risk_level
        return RiskLevel.LOW

    def _tool_needs_approval(self, tool_name: str) -> bool:
        tool = self.tool_registry.get(tool_name)
        if tool:
            return tool.definition.requires_approval_by_default
        return False

    def _dev_fallback(self, prompt: str) -> str:
        """Fallback response when LLM is unavailable or fails.

        Only checks the TASK OBJECTIVE portion of the prompt, not the
        tools definition section (which always contains 'file.read' etc.).
        """
        # Extract just the task context (before "## Available Tools")
        task_section = prompt.split("## Available Tools")[0] if "## Available Tools" in prompt else prompt
        task_lower = task_section.lower()

        # Check if the actual task involves file operations (not tool definitions)
        has_file_task = (
            ("file" in task_lower and ("read" in task_lower or "reading" in task_lower or "write" in task_lower))
            or "读取" in task_section or "写入" in task_section or "文件" in task_section
            or "保存" in task_section
        )

        if has_file_task:
            return (
                "⚠️ LLM 调用失败（超时或网络问题），当前步骤涉及文件操作。\n"
                "请检查：1) 网络连接 2) API 额度 3) 尝试切换 DeepSeek V4 Flash"
            )
        return (
            "⚠️ LLM 调用失败（超时或网络问题）。\n"
            "常见原因：网络延迟高、API 限流、或 prompt 过大导致超时。\n"
            "建议：1) 检查网络 2) 在设置页切换为 DeepSeek V4 Flash 3) 简化任务描述"
        )

    @staticmethod
    def _is_likely_fallback(text: str) -> bool:
        """Check if text is a dev fallback / LLM error message.

        These messages are produced when the LLM is unavailable; retrying
        the same step won't produce better results.
        """
        fallback_markers = [
            "⚠️ LLM 调用失败",
            "LLM response timeout",
            "LLM call failed",
        ]
        return any(marker in text for marker in fallback_markers)

    @staticmethod
    def _sanitize_output(text: str) -> str:
        """Clean LLM output of meta-reasoning / process descriptions."""
        import re
        if not text or len(text) < 20:
            return text

        meta_patterns = [
            r'^The current step is[^.]*\.\s*',
            r'^Based on (the )?session history[^.]*\.\s*',
            r'^I need to first[^.]*\.\s*',
            r'^Let me[^.]*\.\s*',
            r'^Step \d+ has[^.]*\.\s*',
        ]

        cleaned = text.strip()
        for pattern in meta_patterns:
            cleaned = re.sub(pattern, '', cleaned, flags=re.IGNORECASE).strip()

        if not cleaned or len(cleaned) < 10:
            lines = text.split('\n')
            for line in lines:
                line = line.strip()
                if len(line) > 15 and not any(
                    kw in line for kw in ['current step', 'session history', 'Step', 'need to first']
                ):
                    return line
            return text

        return cleaned

    @staticmethod
    def _make_permission_request(
        tool_name: str, denial_message: str, task_objective: str
    ) -> str:
        """Generate user-understandable permission request when tool returns DENIED."""
        import re
        path_match = re.search(r'[/\\][^\s,.;]+', denial_message)
        path_info = path_match.group(0) if path_match else "specified directory"

        if "read" in tool_name or "file" in tool_name:
            return (
                f"I need to access {path_info} to complete task '{task_objective[:60]}'.\n\n"
                f"This directory is not in the current authorization scope. Allow me to read files there?"
            )
        if "write" in tool_name:
            return (
                f"I need to write to {path_info} to complete the task.\n\n"
                f"This directory is not in the current authorization scope. Allow me to write?"
            )
        if "command" in tool_name:
            return (
                f"I need to run a command in {path_info} to complete task '{task_objective[:60]}'.\n\n"
                f"This involves system command execution and requires your authorization. Allow?"
            )
        return (
            f"To complete task '{task_objective[:60]}', I need to execute {tool_name}.\n\n"
            f"{denial_message}\n\nGrant permission?"
        )

    @staticmethod
    def _make_clarification_question(
        step_objective: str, reasoning: str, task_objective: str
    ) -> str:
        """Generate a clear clarification question when LLM decides ask_user without a good question."""
        import re
        q_patterns = [r'[?]$', r'could you', r'please provide', r'can you tell']
        for pat in q_patterns:
            if re.search(pat, reasoning, re.IGNORECASE):
                return reasoning

        if 'topic' in step_objective.lower():
            return f"About '{task_objective}', please confirm:\n- What specific topic to focus on?\n- Any special requirements?"
        return f"About '{task_objective}', I need more information to continue. {reasoning[:100]}"

    # -- Memory Integration --

    def _record_approval_request(
        self, task: Task, step: PlanStep, exc: ApprovalRequiredError
    ) -> None:
        """Create tool-level approval request record in approval_requests table."""
        import json as _json
        from uuid import uuid4
        approval_id = f"approval_{uuid4().hex[:16]}"
        now = datetime.now(timezone.utc).isoformat()
        data = _json.dumps({
            "tool_name": exc.tool_name,
            "reason": exc.reason,
            "step_index": step.index,
            "step_title": step.title,
            "task_objective": task.objective[:200],
        }, ensure_ascii=False)
        try:
            self.events.db.insert_json("approval_requests", {
                "id": approval_id,
                "task_id": task.task_id,
                "step_id": step.step_id,
                "status": "PENDING",
                "data_json": data,
                "created_at": now,
                "resolved_at": None,
            })
            logger.info(
                "Created approval request %s for tool %s (step %d)",
                approval_id[:20], exc.tool_name, step.index)
        except Exception as e:
            logger.warning("Failed to create approval request record: %s", e)

    def _remember_step(self, task: Task, step: PlanStep, output: str) -> None:
        """Write step output to session memory (with decay TTL) and index to vector store."""
        if not self._memory_manager:
            return
        try:
            from flowcraft_core.memory.manager import MemoryEntry
            from flowcraft_core.memory.vector_store import get_vector_store, IndexedMemory
            entry = MemoryEntry(
                memory_type="SESSION",
                scope_id=task.session_id,
                title=f"Step {step.index}: {step.title}",
                content=output[:2000],
                source_type="step",
                source_id=f"{task.task_id}:{step.step_id}",
            )
            self._memory_manager.write_memory(entry)

            try:
                vs = get_vector_store()
                im = IndexedMemory(
                    memory_id=entry.memory_id,
                    memory_type="SESSION",
                    scope_id=task.session_id,
                    title=entry.title,
                    content=entry.content,
                    created_at=entry.created_at,
                    confidence=entry.confidence,
                )
                vs.index(im)
            except Exception:
                pass
        except Exception:
            pass

    def _get_session_memory_context(self, task: Task) -> str:
        """Get session memory summary for LLM context injection."""
        if not self._memory_manager:
            return ""
        try:
            memories = self._memory_manager.get_session_memories(
                task.session_id, max_count=20, apply_decay=True)
            if not memories:
                memories = []

            semantic_memories: list[dict] = []
            try:
                semantic_memories = self._memory_manager.get_session_memories_semantic(
                    task.session_id, task.objective, top_k=5)
            except Exception:
                pass

            cross_task = self._memory_manager.get_cross_task_context(
                task.session_id, task.task_id)

            parts: list[str] = []

            if cross_task:
                parts.append(cross_task)

            if semantic_memories:
                lines = ["## Semantic related memories"]
                for m in semantic_memories[:5]:
                    score = m.get("_score", 0)
                    title = m.get("title", "")[:60]
                    content = m.get("content", "")[:200]
                    lines.append(f"- [{score:.2f}] **{title}**: {content}")
                parts.append("\n".join(lines))

            if memories:
                lines = ["## Session history (time-decayed)"]
                for m in memories[:10]:
                    conf = m.get("confidence", 0)
                    if conf < 0.15:
                        continue
                    title = m.get("title", "")[:60]
                    content = m.get("content", "")[:250]
                    decay = m.get("_decay_factor", 1.0)
                    decay_note = f" [freshness:{decay:.0%}]" if decay < 0.8 else ""
                    lines.append(f"- **{title}**{decay_note}: {content}")
                parts.append("\n".join(lines))

            return "\n".join(parts)
        except Exception:
            return ""

    @staticmethod
    def _build_context_summary(observations: list[ToolObservation]) -> str:
        """Build compressed summary from tool observations for checkpoint."""
        if not observations:
            return ""
        parts = []
        for obs in observations[-3:]:
            parts.append(f"[{obs.status}] {obs.output_summary[:200]}")
        return " | ".join(parts)

    def _event(
        self,
        task: Task,
        event_type: str,
        title: str,
        message: str,
        payload: dict | None = None,
        severity: str = "INFO",
    ) -> None:
        self.events.record(
            TraceEvent(
                task_id=task.task_id,
                session_id=task.session_id,
                event_type=event_type,
                title=title,
                message=message,
                payload=payload or {},
                severity=severity,
            )
        )


class ApprovalRequiredError(Exception):
    def __init__(self, tool_name: str, reason: str) -> None:
        self.tool_name = tool_name
        self.reason = reason
        super().__init__(f"审批需要: {tool_name} - {reason}")


class StepFailedError(Exception):
    pass
