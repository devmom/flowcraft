from __future__ import annotations

import asyncio
import logging
import threading
import time as _time
from datetime import datetime, timezone

from flowcraft_core.approval.manager import ApprovalManager
from flowcraft_core.domain.enums import PolicyDecisionValue, TaskStatus
from flowcraft_core.domain.schemas import AgentRequest, Task, TraceEvent, now_utc
from flowcraft_core.execution.engine import ExecutionEngine
from flowcraft_core.intent.engine import IntentEngine
from flowcraft_core.logging_config import get_trace_logger
from flowcraft_core.observability.events import EventRecorder
from flowcraft_core.planning.planner import PlanValidator, Planner
from flowcraft_core.policy.engine import PolicyEngine
from flowcraft_core.runtime.task_store import TaskStore

logger = logging.getLogger(__name__)
trace = get_trace_logger("runtime.engine")

# Default task execution timeout (seconds) — increased for long-running tasks
TASK_TIMEOUT = 600  # 10 minutes

# Track active task threads for health monitoring and force-kill
_active_tasks: dict[str, dict] = {}
_active_lock = threading.Lock()


def get_active_tasks() -> dict[str, dict]:
    """Return snapshot of currently executing tasks."""
    with _active_lock:
        return dict(_active_tasks)


class RuntimeEngine:
    def __init__(
        self,
        task_store: TaskStore,
        events: EventRecorder,
        intent_engine: IntentEngine,
        planner: Planner,
        plan_validator: PlanValidator,
        policy_engine: PolicyEngine,
        approval_manager: ApprovalManager,
        execution_engine: ExecutionEngine | None = None,
        workflow_builder=None,
    ) -> None:
        self.task_store = task_store
        self.events = events
        self.intent_engine = intent_engine
        self.planner = planner
        self.plan_validator = plan_validator
        self.policy_engine = policy_engine
        self.approval_manager = approval_manager
        self.execution_engine = execution_engine
        self.workflow_builder = workflow_builder

    async def create_task_async(self, request: AgentRequest) -> Task:
        """创建任务并立即返回，后台异步执行 pipeline。

        空输入或纯空白输入直接返回 FAILED 状态的任务。
        """
        raw = request.raw_input.strip()
        if not raw:
            task = Task(
                session_id=request.session_id,
                user_id=request.user_id,
                title="空输入",
                objective="(empty)",
                status=TaskStatus.FAILED,
                failed_reason="输入为空，请提供有效的任务描述",
            )
            self.task_store.save_task(task)
            self.events.record(TraceEvent(
                task_id=task.task_id, session_id=task.session_id,
                event_type="task.failed", title="空输入被拒绝",
                message="输入不能为空", severity="WARN",
            ))
            return task

        task = Task(
            session_id=request.session_id,
            user_id=request.user_id,
            title=self._make_title(request.raw_input),
            objective=request.raw_input,
        )
        self.task_store.save_task(task)
        self.events.record(
            TraceEvent(
                task_id=task.task_id,
                session_id=task.session_id,
                event_type="task.created",
                title="任务已创建",
                message=task.objective,
            )
        )

        # Register SSE streaming listener for this task
        from flowcraft_core.observability.events import sse_listener_factory
        listener = sse_listener_factory(task.task_id)
        self.events.subscribe(listener)

        # 注册活跃任务（用于监控和强制终止）
        tid = task.task_id
        thread_control = {"cancel_event": threading.Event()}
        with _active_lock:
            _active_tasks[tid] = {
                "task_id": tid,
                "title": task.title,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "thread": None,
                "cancel_event": thread_control["cancel_event"],
            }

        # 后台线程执行完整 pipeline（带超时）
        def _run_pipeline() -> None:
            t0 = _time.monotonic()
            trace.pipeline_begin(tid, "pipeline", f"后台线程启动: title={task.title[:60]}")
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                # 任务级超时：最多执行 TASK_TIMEOUT 秒
                coro = self._execute_pipeline(task, request)
                loop.run_until_complete(
                    asyncio.wait_for(coro, timeout=TASK_TIMEOUT)
                )
                elapsed = _time.monotonic() - t0
                trace.pipeline_end(tid, "pipeline",
                                 f"Pipeline完成 ({elapsed:.1f}s): status={task.status.value}",
                                 elapsed_s=elapsed)
            except asyncio.TimeoutError:
                elapsed = _time.monotonic() - t0
                trace.error(tid, "pipeline.timeout",
                           f"任务超时 ({elapsed:.1f}s > {TASK_TIMEOUT}s)! 可能存在死循环或模型响应过慢",
                           elapsed_s=elapsed)
                logger.warning("Task %s timed out after %ds", tid[:12], TASK_TIMEOUT)
                try:
                    task.status = TaskStatus.FAILED
                    task.failed_reason = f"任务执行超时（{TASK_TIMEOUT}秒），可能存在死循环或模型服务响应过慢"
                    task.updated_at = now_utc()
                    self.task_store.update_task(task)
                    self.events.record(TraceEvent(
                        task_id=tid, event_type="task.failed",
                        title="任务超时", message=task.failed_reason, severity="ERROR",
                    ))
                except Exception:
                    pass
            except Exception as exc:
                elapsed = _time.monotonic() - t0
                trace.error(tid, "pipeline.exception",
                           f"Pipeline异常 ({elapsed:.1f}s): {type(exc).__name__}: {exc}",
                           elapsed_s=elapsed)
                logger.exception("Background pipeline failed: %s", exc)
                try:
                    task.status = TaskStatus.FAILED
                    task.failed_reason = f"执行异常: {str(exc)[:200]}"
                    self.task_store.update_task(task)
                    self.events.record(TraceEvent(
                        task_id=tid, event_type="task.failed",
                        title="任务执行失败", message=str(exc)[:200], severity="ERROR",
                    ))
                except Exception:
                    pass
            finally:
                # 清理活跃任务记录
                with _active_lock:
                    _active_tasks.pop(tid, None)
                from flowcraft_core.observability.events import remove_sse_queue
                remove_sse_queue(tid)

        t = threading.Thread(target=_run_pipeline, daemon=True, name=f"task-{tid[:12]}")
        with _active_lock:
            if tid in _active_tasks:
                _active_tasks[tid]["thread"] = t
        t.start()
        return task

    async def _execute_pipeline(self, task: Task, request: AgentRequest) -> None:
        """完整执行 pipeline：意图 → [工作流分支/规划] → 策略 → 执行。"""
        tid = task.task_id
        t0 = _time.monotonic()

        # 1. 意图识别 - 输出进度
        trace.pipeline_begin(tid, "intent", "开始意图识别")
        self._emit_progress(task, "正在分析你的任务意图...")
        brief = await self.intent_engine.recognize(task.task_id, request)
        task.status = TaskStatus.INTENT_RECOGNIZED
        task.task_type = brief.task_type
        task.risk_level = brief.risk_level
        task.success_criteria = brief.success_criteria
        task.updated_at = now_utc()
        self.task_store.update_task(task)
        self.task_store.save_brief(brief)
        # 用户可见：意图识别结果
        intent_label = {"QA": "问答", "FILE_TASK": "文件操作", "BROWSER_TASK": "网页浏览",
                        "LOCAL_OPERATION": "本地命令", "DOCUMENT_SUMMARY": "文档处理",
                        "WORKFLOW_AUTOMATION": "工作流创建"}.get(brief.task_type, brief.task_type)
        self.events.record(
            TraceEvent(
                task_id=task.task_id, session_id=task.session_id,
                event_type="intent.recognized", title="已识别任务意图",
                message=f"类型：{brief.task_type}，风险：{brief.risk_level}",
                payload=brief.model_dump(mode="json"),
            )
        )
        self._emit_progress(task, f"已识别意图：{intent_label}（风险：{brief.risk_level}）")
        trace.pipeline_end(tid, "intent",
                          f"意图识别完成: type={brief.task_type} risk={brief.risk_level}",
                          elapsed_s=_time.monotonic() - t0)

        # ── WORKFLOW_AUTOMATION: route to Workflow Builder ──
        if brief.task_type == "WORKFLOW_AUTOMATION":
            await self._activate_workflow_builder(task, request)
            return

        # 2. 生成执行计划 - 输出进度
        self._emit_progress(task, "正在制定执行计划...")
        trace.pipeline_begin(tid, "plan", "开始规划")
        plan = await self.planner.create_plan(brief)
        errors = self.plan_validator.validate(plan)
        if errors:
            trace.pipeline_error(tid, "plan", f"计划校验失败: {'; '.join(errors)}")
            task.status = TaskStatus.FAILED
            task.failed_reason = "；".join(errors)
            task.updated_at = now_utc()
            self.task_store.update_task(task)
            self.events.record(
                TraceEvent(
                    task_id=task.task_id, session_id=task.session_id,
                    event_type="task.failed", title="计划校验失败",
                    message=task.failed_reason, severity="ERROR",
                )
            )
            return

        self.task_store.save_plan(plan)
        task.current_plan_id = plan.plan_id
        task.status = TaskStatus.PLANNED
        task.updated_at = now_utc()
        self.task_store.update_task(task)
        self.events.record(
            TraceEvent(
                task_id=task.task_id, session_id=task.session_id,
                event_type="plan.created", title="已生成执行计划",
                message=f"计划包含 {len(plan.steps)} 个步骤。",
                payload=plan.model_dump(mode="json"),
            )
        )
        # 用户可见：计划摘要
        step_preview = " → ".join(s.title for s in plan.steps[:4])
        if len(plan.steps) > 4:
            step_preview += f" ... 共{len(plan.steps)}步"
        self._emit_progress(task,
            f"执行计划（{plan.mode.value}模式）：{len(plan.steps)}个步骤 → {step_preview}")
        trace.pipeline_end(tid, "plan",
                          f"规划完成: {len(plan.steps)}步 {plan.mode.value}",
                          elapsed_s=_time.monotonic() - t0)

        # 3. 策略检查 + 执行（包裹在 try 中防止 PLANNED 卡死）
        self._emit_progress(task, "正在检查安全策略...")
        trace.pipeline_begin(tid, "execute", "开始执行阶段")
        try:
            await self._execute_post_plan(task, brief, plan)
            trace.pipeline_end(tid, "execute",
                             f"执行阶段结束: status={task.status.value}",
                             elapsed_s=_time.monotonic() - t0)
        except Exception as exc:
            logger.exception("Post-plan execution failed: %s", exc)
            trace.pipeline_error(tid, "execute", f"执行异常: {exc}")
            task.status = TaskStatus.FAILED
            task.failed_reason = f"执行阶段异常: {str(exc)[:200]}"
            task.updated_at = now_utc()
            self.task_store.update_task(task)
            self.events.record(
                TraceEvent(
                    task_id=task.task_id, session_id=task.session_id,
                    event_type="task.failed", title="执行阶段异常",
                    message=str(exc)[:200], severity="ERROR",
                )
            )

    async def _activate_workflow_builder(self, task: Task, request: AgentRequest) -> None:
        """Route WORKFLOW_AUTOMATION intent to the Workflow Builder.

        Instead of planning and executing, we:
        1. Start a workflow builder session from the user's input
        2. Emit a special event telling the frontend to enter builder mode
        3. Mark the task as COMPLETED (the builder takes over)
        """
        tid = task.task_id
        logger.info("Routing task %s to Workflow Builder (WORKFLOW_AUTOMATION)", tid[:12])

        if not self.workflow_builder:
            logger.warning("WorkflowBuilder not available, falling back to normal execution")
            task.status = TaskStatus.FAILED
            task.failed_reason = "工作流创建功能暂不可用（WorkflowBuilder 未初始化）"
            task.updated_at = now_utc()
            self.task_store.update_task(task)
            self.events.record(TraceEvent(
                task_id=tid, event_type="task.failed",
                title="工作流创建失败", message=task.failed_reason, severity="ERROR",
            ))
            return

        try:
            # Start the builder session
            builder_result = self.workflow_builder.start(
                user_input=request.raw_input,
            )

            # Emit special event that tells frontend to enter builder mode
            self.events.record(TraceEvent(
                task_id=tid,
                session_id=task.session_id,
                event_type="workflow.builder.activated",
                title="已激活工作流构建模式",
                message=builder_result["agent_message"],
                payload={
                    "builder_session_id": builder_result["session_id"],
                    "stage": builder_result["stage"],
                    "agent_message": builder_result["agent_message"],
                },
            ))

            # Mark task as completed (the builder handles the rest via its own API)
            task.status = TaskStatus.COMPLETED
            task.updated_at = now_utc()
            task.completed_at = now_utc()
            self.task_store.update_task(task)

            self.events.record(TraceEvent(
                task_id=tid,
                session_id=task.session_id,
                event_type="task.completed",
                title="已切换至工作流构建模式",
                message="请在下方继续与工作流构建助手对话。",
            ))

            logger.info("Workflow builder activated for task %s, session=%s",
                       tid[:12], builder_result["session_id"])

        except Exception as exc:
            logger.exception("Failed to activate workflow builder: %s", exc)
            task.status = TaskStatus.FAILED
            task.failed_reason = f"工作流构建启动失败: {str(exc)[:200]}"
            task.updated_at = now_utc()
            self.task_store.update_task(task)
            self.events.record(TraceEvent(
                task_id=tid, event_type="task.failed",
                title="工作流构建失败", message=str(exc)[:200], severity="ERROR",
            ))

    async def _execute_post_plan(self, task: Task, brief, plan) -> None:
        """策略检查 + 执行（从 _execute_pipeline 中提取，确保异常可被上层捕获）"""
        # 策略检查
        decision = self.policy_engine.check_plan(task.task_id, plan)
        self.events.record(
            TraceEvent(
                task_id=task.task_id, session_id=task.session_id,
                event_type="policy.checked", title="已完成计划策略检查",
                message=decision.reason,
                payload=decision.model_dump(mode="json"),
            )
        )
        if decision.decision == PolicyDecisionValue.REQUIRE_APPROVAL:
            task.status = TaskStatus.WAITING_APPROVAL
            task.updated_at = now_utc()
            self.task_store.update_task(task)
            approval = self.approval_manager.create_from_policy(
                decision, "确认高风险计划", "该计划包含高风险步骤，需要你确认后再继续。",
            )
            self.events.record(
                TraceEvent(
                    task_id=task.task_id, session_id=task.session_id,
                    event_type="approval.requested", title="需要用户确认",
                    message=approval.action_description,
                    payload=approval.model_dump(mode="json"),
                )
            )
            return

        # 4. 真正执行
        if self.execution_engine:
            result = await self.execution_engine.execute_plan(task, brief, plan)
            self.task_store.update_task(result)
        else:
            task.status = TaskStatus.COMPLETED
            task.completed_at = now_utc()
            task.updated_at = now_utc()
            self.task_store.update_task(task)
            self.events.record(
                TraceEvent(
                    task_id=task.task_id, session_id=task.session_id,
                    event_type="task.completed", title="任务已完成",
                    message="意图识别和规划已完成。",
                )
            )

    async def start_task(self, request: AgentRequest) -> Task:
        task = Task(
            session_id=request.session_id,
            user_id=request.user_id,
            title=self._make_title(request.raw_input),
            objective=request.raw_input,
        )
        self.task_store.save_task(task)
        self.events.record(
            TraceEvent(
                task_id=task.task_id,
                session_id=task.session_id,
                event_type="task.created",
                title="任务已创建",
                message=task.objective,
            )
        )

        try:
            # 1. 意图识别
            brief = await self.intent_engine.recognize(task.task_id, request)
            task.status = TaskStatus.INTENT_RECOGNIZED
            task.task_type = brief.task_type
            task.risk_level = brief.risk_level
            task.success_criteria = brief.success_criteria
            task.updated_at = now_utc()
            self.task_store.update_task(task)
            self.task_store.save_brief(brief)
            self.events.record(
                TraceEvent(
                    task_id=task.task_id,
                    session_id=task.session_id,
                    event_type="intent.recognized",
                    title="已识别任务意图",
                    message=f"类型：{brief.task_type}，风险：{brief.risk_level}",
                    payload=brief.model_dump(mode="json"),
                )
            )

            # 2. 生成执行计划
            plan = await self.planner.create_plan(brief)
            errors = self.plan_validator.validate(plan)
            if errors:
                task.status = TaskStatus.FAILED
                task.failed_reason = "；".join(errors)
                task.updated_at = now_utc()
                self.task_store.update_task(task)
                self.events.record(
                    TraceEvent(
                        task_id=task.task_id,
                        session_id=task.session_id,
                        event_type="task.failed",
                        title="计划校验失败",
                        message=task.failed_reason,
                        severity="ERROR",
                    )
                )
                return task

            self.task_store.save_plan(plan)
            task.current_plan_id = plan.plan_id
            task.status = TaskStatus.PLANNED
            task.updated_at = now_utc()
            self.task_store.update_task(task)
            self.events.record(
                TraceEvent(
                    task_id=task.task_id,
                    session_id=task.session_id,
                    event_type="plan.created",
                    title="已生成执行计划",
                    message=f"计划包含 {len(plan.steps)} 个步骤。",
                    payload=plan.model_dump(mode="json"),
                )
            )

            # 3. 策略检查
            decision = self.policy_engine.check_plan(task.task_id, plan)
            self.events.record(
                TraceEvent(
                    task_id=task.task_id,
                    session_id=task.session_id,
                    event_type="policy.checked",
                    title="已完成计划策略检查",
                    message=decision.reason,
                    payload=decision.model_dump(mode="json"),
                )
            )
            if decision.decision == PolicyDecisionValue.REQUIRE_APPROVAL:
                approval = self.approval_manager.create_from_policy(
                    decision,
                    "确认高风险计划",
                    "该计划包含高风险步骤，需要你确认后再继续。",
                )
                task.status = TaskStatus.WAITING_APPROVAL
                task.updated_at = now_utc()
                self.task_store.update_task(task)
                self.events.record(
                    TraceEvent(
                        task_id=task.task_id,
                        session_id=task.session_id,
                        event_type="approval.requested",
                        title="需要用户确认",
                        message=approval.action_description,
                        payload=approval.model_dump(mode="json"),
                    )
                )
                return task

            # 4. 真正执行
            if self.execution_engine:
                try:
                    task = await self.execution_engine.execute_plan(task, brief, plan)
                except Exception as exc:
                    logger.exception("Execution failed")
                    task.status = TaskStatus.FAILED
                    task.failed_reason = str(exc)
                    task.updated_at = now_utc()
                    self.events.record(
                        TraceEvent(
                            task_id=task.task_id,
                            session_id=task.session_id,
                            event_type="task.failed",
                            title="任务执行失败",
                            message=str(exc),
                            severity="ERROR",
                        )
                    )
            else:
                # 没有执行引擎 → 标记完成（无操作模式）
                task.status = TaskStatus.COMPLETED
                task.completed_at = now_utc()
                task.updated_at = now_utc()
                self.events.record(
                    TraceEvent(
                        task_id=task.task_id,
                        session_id=task.session_id,
                        event_type="task.completed",
                        title="任务已完成",
                        message="意图识别和规划已完成。接入执行引擎后可执行具体步骤。",
                    )
                )

            self.task_store.update_task(task)
            return task

        except Exception as exc:
            logger.exception("Task pipeline failed")
            task.status = TaskStatus.FAILED
            task.failed_reason = str(exc)
            task.updated_at = now_utc()
            self.task_store.update_task(task)
            self.events.record(
                TraceEvent(
                    task_id=task.task_id,
                    session_id=task.session_id,
                    event_type="task.failed",
                    title="任务执行失败",
                    message=str(exc),
                    severity="ERROR",
                )
            )
            return task

    @staticmethod
    def _make_title(text: str) -> str:
        stripped = text.strip().replace("\n", " ")
        return stripped[:40] or "新任务"

    def _emit_progress(self, task: Task, message: str) -> None:
        """发送用户可见的进度事件（流式推送）."""
        self.events.record(TraceEvent(
            task_id=task.task_id, session_id=task.session_id,
            event_type="progress.update", title="进度",
            message=message, severity="INFO",
        ))

