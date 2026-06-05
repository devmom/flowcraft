from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from flowcraft_core.approval.manager import ApprovalManager
from flowcraft_core.config.settings import Settings
from flowcraft_core.domain.schemas import TraceEvent
from flowcraft_core.execution.checkpoint import CheckpointManager
from flowcraft_core.execution.engine import ExecutionEngine
from flowcraft_core.execution.subtask import TaskSpawner
from flowcraft_core.intent.engine import IntentEngine
from flowcraft_core.memory.manager import MemoryManager
from flowcraft_core.models.adapters.agnes import AGNES_2_FLASH_PROFILE, AgnesTextAdapter
from flowcraft_core.models.adapters.openai_compatible import OpenAICompatibleAdapter
from flowcraft_core.models.gateway import DEFAULT_DEEPSEEK_PROFILE, ModelGateway
from flowcraft_core.observability.events import EventRecorder
from flowcraft_core.planning.planner import PlanValidator, Planner
from flowcraft_core.policy.engine import PolicyEngine
from flowcraft_core.runtime.engine import RuntimeEngine
from flowcraft_core.runtime.task_store import TaskStore
from flowcraft_core.security.secrets import SecretStore
from flowcraft_core.storage.database import Database
from flowcraft_core.tools.browser import BrowserReadTool, BrowserScreenshotTool
from flowcraft_core.tools.builtin import CommandRunTool, FileReadTool, FileWriteTool
from flowcraft_core.tools.document import DocxReadTool, ExcelReadTool, PdfReadTool
from flowcraft_core.tools.harness import ToolHarness, ToolRegistry
from flowcraft_core.tools.filesystem import FileDeleteTool, FileListTool, FileMetaTool, FileSearchTool
from flowcraft_core.tools.knowledge import KnowledgeSearchTool
from flowcraft_core.tools.network import HttpDownloadTool, HttpRequestTool, WebSearchTool
from flowcraft_core.tools.playwright_tools import (
    BrowserNavigateTool, BrowserClickTool, BrowserFillTool, BrowserScreenshotFullTool,
)
from flowcraft_core.tools.plugin_registry import PluginRegistry
from flowcraft_core.tools.sandbox import CodeExecuteTool
from flowcraft_core.tools.workflow_tools import WorkflowSearchTool, WorkflowExecuteTool
from flowcraft_core.tools.tool_factory import (
    ToolCreateMetaTool, ToolDeleteMetaTool, ToolFactory, ToolListDynamicMetaTool,
)
from flowcraft_core.workflows.builder import WorkflowBuilder

logger = logging.getLogger(__name__)


class FlowCraftApp:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # Automatically allow access to uploads directory so uploaded files
        # can be read by the agent without manual permission requests.
        uploads_dir = settings.temp_dir / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        if uploads_dir.resolve() not in [p.resolve() for p in settings.allowed_paths]:
            settings.allowed_paths.append(uploads_dir)

        self.db = Database(settings.database_path)
        self.db.initialize()
        self.events = EventRecorder(self.db)
        self.secrets = SecretStore(self.db)
        self.memory = MemoryManager(self.db)
        self.model_gateway = ModelGateway()
        self._auto_configure_model()
        self.intent_engine = IntentEngine(self.model_gateway)
        self.tool_registry = ToolRegistry()
        self._register_builtin_tools(settings.allowed_paths)
        self.planner = Planner(self.model_gateway, self.tool_registry, self.memory)
        self.plan_validator = PlanValidator()
        self.policy_engine = PolicyEngine()
        self.approval_manager = ApprovalManager()
        self.task_store = TaskStore(self.db)
        self.checkpoint_manager = CheckpointManager(self.db)
        self.tool_harness = ToolHarness(self.tool_registry, self.policy_engine)
        self.execution_engine = ExecutionEngine(
            model_gateway=self.model_gateway,
            tool_registry=self.tool_registry,
            tool_harness=self.tool_harness,
            policy_engine=self.policy_engine,
            events=self.events,
            checkpoint_manager=self.checkpoint_manager,
        )
        # Wire memory and knowledge into execution engine
        self.execution_engine._memory_manager = self.memory

        # Subtask spawner for complex task decomposition
        self.task_spawner = TaskSpawner(self.model_gateway)

        # Workflow builder (guided workflow creation)
        self.workflow_builder = WorkflowBuilder(self.model_gateway, self.tool_registry)

        # Dynamic tool factory (Phase 3)
        self.tool_factory = ToolFactory(self.tool_registry)
        self.tool_registry.register(ToolCreateMetaTool(self.tool_factory))
        self.tool_registry.register(ToolDeleteMetaTool(self.tool_factory))
        self.tool_registry.register(ToolListDynamicMetaTool(self.tool_factory))

        # Plugin system
        self.plugin_registry = PluginRegistry(settings.plugins_dir)
        self.plugin_registry.load_state()
        plugin_tools = self.plugin_registry.load_tools(self.tool_registry)
        if plugin_tools > 0:
            logger.info("Loaded %d plugin tools", plugin_tools)

        # Long-term memory and knowledge base
        from flowcraft_core.memory.long_term import LongTermMemory
        self.long_term_memory = LongTermMemory(self.db, self.memory)
        from flowcraft_core.memory.knowledge_base import KnowledgeBase
        self.knowledge_base = KnowledgeBase(self.db, settings.knowledge_dir)
        from flowcraft_core.observability.replay import TaskReplay
        self.task_replay = TaskReplay(self.db, self.events)
        from flowcraft_core.config.i18n import I18n
        self.i18n = I18n()

        # 启动卡死任务看门狗
        self._start_watchdog()

        # 记忆系统启动维护：清理过期记忆 + 重建向量索引
        try:
            result = self.memory.startup_maintenance()
            logger.info("Memory maintenance: purged=%d indexed=%d",
                       result.get("purged", 0), result.get("indexed", 0))
        except Exception as exc:
            logger.warning("Memory startup maintenance failed: %s", exc)

        # 启动记忆衰减/过期定期清理线程
        self._start_memory_decay_thread()

        self.runtime = RuntimeEngine(
            task_store=self.task_store,
            events=self.events,
            intent_engine=self.intent_engine,
            planner=self.planner,
            plan_validator=self.plan_validator,
            policy_engine=self.policy_engine,
            approval_manager=self.approval_manager,
            execution_engine=self.execution_engine,
            workflow_builder=self.workflow_builder,
        )
        # Wire runtime into WorkflowExecuteTool (lazy init at tool registration time)
        for tool_name in ("workflow_execute",):
            t = self.tool_registry.get(tool_name)
            if t and hasattr(t, '_runtime'):
                t._runtime = self.runtime
        self._recover_in_progress_tasks()

    def _auto_configure_model(self) -> None:
        """自动从环境变量或 settings 表加载模型配置。

        优先级：
        1. FLOWCRAFT_DEEPSEEK_API_KEY → DeepSeek V4 Pro
        2. AGNES_API_KEY → Agnes 2.0 Flash (免费)
        3. settings 表中存储的 api_key
        4. Ollama 本地模型
        5. deterministic-dev 模式
        """
        # ── 1. DeepSeek ──
        api_key = os.environ.get("FLOWCRAFT_DEEPSEEK_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            api_key = self.secrets.get("model:deepseek:deepseek-v4-pro:api_key") or self.secrets.get("model:deepseek:deepseek-chat:api_key")

        if api_key:
            try:
                adapter = OpenAICompatibleAdapter(DEFAULT_DEEPSEEK_PROFILE, api_key=api_key)
                self.model_gateway.configure(adapter, DEFAULT_DEEPSEEK_PROFILE)
                logger.info("Auto-configured DeepSeek model")
                return
            except Exception as exc:
                logger.warning("Failed to auto-configure DeepSeek: %s", exc)

        # ── 2. Agnes AI (free tier) ──
        agnes_key = os.environ.get("AGNES_API_KEY")
        if not agnes_key:
            agnes_key = self.secrets.get("model:agnes:agnes-2.0-flash:api_key")

        if agnes_key:
            try:
                adapter = AgnesTextAdapter(AGNES_2_FLASH_PROFILE, api_key=agnes_key)
                self.model_gateway.configure(adapter, AGNES_2_FLASH_PROFILE)
                logger.info("Auto-configured Agnes AI model (free tier)")
                return
            except Exception as exc:
                logger.warning("Failed to auto-configure Agnes AI: %s", exc)

        # ── 3. Ollama local model ──
        ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        try:
            from flowcraft_core.models.adapters.ollama import OllamaAdapter
            models = __import__("asyncio").run(OllamaAdapter.list_local_models(ollama_url))
            if models:
                model_name = models[0].get("name", "qwen3")
                qwen = next((m for m in models if "qwen" in m.get("name", "").lower()), None)
                if qwen:
                    model_name = qwen["name"]
                adapter = OllamaAdapter.from_model_name(model_name, base_url=ollama_url + "/v1")
                self.model_gateway.configure(adapter, adapter.profile)
                logger.info("Auto-configured Ollama local model: %s", model_name)
                return
        except Exception:
            pass

        logger.info("No API key or local model found, using deterministic-dev mode. "
                    "Set FLOWCRAFT_DEEPSEEK_API_KEY or AGNES_API_KEY env var, or install Ollama.")

    def _start_watchdog(self) -> None:
        """启动卡死任务看门狗：每30秒扫描一次，将超时任务标记FAILED."""
        import threading
        def _watchdog() -> None:
            import time as _t
            while True:
                _t.sleep(30)
                try:
                    self._sweep_stuck_tasks()
                except Exception:
                    pass
        t = threading.Thread(target=_watchdog, daemon=True, name="watchdog")
        t.start()
        logger.info("Watchdog thread started")

    def _start_memory_decay_thread(self) -> None:
        """启动记忆衰减/过期定期清理线程：每5分钟清理过期记忆，应用衰减."""
        import threading
        def _decay_loop() -> None:
            import time as _t
            while True:
                _t.sleep(300)  # 每5分钟
                try:
                    purged = self.memory.purge_expired_memories()
                    if purged > 0:
                        logger.debug("Memory decay: purged %d expired", purged)
                    # 对向量存储也应用衰减
                    from flowcraft_core.memory.vector_store import get_vector_store
                    vs = get_vector_store()
                    pruned = vs.apply_decay_all()
                    if pruned > 0:
                        logger.debug("Vector store: pruned %d decayed", pruned)
                except Exception:
                    pass
        t = threading.Thread(target=_decay_loop, daemon=True, name="memory-decay")
        t.start()
        logger.info("Memory decay thread started")

    def _sweep_stuck_tasks(self) -> None:
        """扫描并处理卡死的任务。

        阈值提升以支持长周期任务:
            CREATED/INTENT_RECOGNIZED/PLANNED: 300秒无进度 → FAILED
            EXECUTING: 600秒无进度 + 无活跃线程 → FAILED
            EXECUTING + 有检查点: 不杀（长任务正在正常推进）
        """
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        now = _dt.now(_tz.utc)

        stuck_thresholds = {
            "CREATED": 300,
            "INTENT_RECOGNIZED": 300,
            "PLANNED": 300,
            "EXECUTING": 600,
        }

        for status, threshold in stuck_thresholds.items():
            rows = self.db.fetch_all(
                "SELECT id, title, updated_at FROM tasks WHERE status = ?", (status,))
            for row in rows:
                r = dict(row)
                try:
                    updated = _dt.fromisoformat(r["updated_at"])
                    age = (now - updated).total_seconds()
                    if age > threshold:
                        # Check for active thread
                        from flowcraft_core.runtime.engine import get_active_tasks
                        active = get_active_tasks()
                        if r["id"] in active:
                            # Has active thread → keep alive (long-running task)
                            logger.debug("Watchdog: task %s has active thread, keeping alive (%.0fs)",
                                       r["id"][:12], age)
                            continue

                        # Check for checkpoints → if has recent checkpoint, keep alive
                        ckpt = self.checkpoint_manager.load_latest(r["id"])
                        if ckpt:
                            ckpt_time = _dt.fromisoformat(
                                ckpt.observation_snapshot[-1].get("created_at",
                                    ckpt.observation_snapshot[-1].get("timestamp", ""))
                            ) if ckpt.observation_snapshot else None
                            # Keep alive if checkpoint was created recently
                            if ckpt_time and (now - ckpt_time).total_seconds() < threshold:
                                logger.debug("Watchdog: task %s has recent checkpoint, keeping alive",
                                           r["id"][:12])
                                continue

                        self.db.update("tasks", "id", r["id"], {
                            "status": "FAILED",
                            "failed_reason": f"任务在 {status} 状态停滞超过 {age:.0f} 秒，自动终止",
                            "updated_at": now.isoformat(),
                        })
                        from flowcraft_core.domain.schemas import TraceEvent
                        self.events.record(TraceEvent(
                            task_id=r["id"],
                            event_type="task.failed",
                            title="任务超时自动终止",
                            message=f"停滞在 {status} 超过 {age:.0f} 秒",
                            severity="ERROR",
                        ))
                        logger.warning("Watchdog: marked stuck task %s (%s, %.0fs) as FAILED",
                                       r["id"][:12], status, age)
                except Exception:
                    pass

    def force_kill_task(self, task_id: str) -> bool:
        """强制终止正在执行的任务（标记CANCELLED + 设置cancel_event）."""
        from flowcraft_core.runtime.engine import get_active_tasks, _active_lock
        from flowcraft_core.execution.engine import get_pause_controller
        try:
            # Cancel via PauseController
            pc = get_pause_controller(task_id)
            pc.cancel()
        except Exception:
            pass
        # Update DB
        self.db.update("tasks", "id", task_id, {
            "status": "CANCELLED",
            "failed_reason": "任务被用户强制终止",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        with _active_lock:
            _active_tasks.pop(task_id, None)
        logger.info("Force-killed task %s", task_id[:12])
        return True

    def _recover_in_progress_tasks(self) -> None:
        """启动恢复：处理异常状态的任务。

        1. CREATED/PLANNED/EXECUTING: 有检查点→保留，无检查点→标记FAILED
        2. WAITING_APPROVAL: 有审批记录→保留，无审批记录→自动取消（孤儿任务）
        """
        # Phase 1: Recoverable statuses (checkpoint-based)
        recoverable_statuses = ("CREATED", "INTENT_RECOGNIZED", "PLANNED", "EXECUTING")
        recovered = 0
        failed = 0
        for status in recoverable_statuses:
            rows = self.db.fetch_all(
                "SELECT id FROM tasks WHERE status = ?", (status,))
            for row in rows:
                task_id = dict(row)["id"]
                ckpt = self.checkpoint_manager.load_latest(task_id)
                if ckpt:
                    recovered += 1
                    logger.info("Recovery: task %s has checkpoint #%d, keeping alive",
                                task_id[:12], ckpt.checkpoint_idx)
                else:
                    self.db.update("tasks", "id", task_id, {
                        "status": "FAILED",
                        "failed_reason": "服务重启，任务中断（无检查点可恢复）",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    })
                    failed += 1

        # Phase 2: Orphaned WAITING_APPROVAL tasks (missing approval request records)
        orphan_cancelled = 0
        orphan_kept = 0
        rows = self.db.fetch_all(
            "SELECT id, created_at FROM tasks WHERE status = 'WAITING_APPROVAL'")
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        for row in rows:
            task_id = dict(row)["id"]
            # Check if approval request exists
            ar = self.db.fetch_one(
                "SELECT id FROM approval_requests WHERE task_id = ? AND status = 'PENDING'",
                (task_id,))
            if ar:
                orphan_kept += 1
                logger.info("Recovery: task %s has pending approval, keeping", task_id[:12])
            else:
                # Orphan: no approval request → auto-cancel
                self.db.update("tasks", "id", task_id, {
                    "status": "CANCELLED",
                    "failed_reason": "审批记录缺失（孤儿任务），已自动取消。请重试。",
                    "updated_at": now.isoformat(),
                })
                self.events.record(TraceEvent(
                    task_id=task_id,
                    event_type="task.cancelled",
                    title="孤儿审批任务已自动取消",
                    message="该任务缺少审批记录，无法继续。已自动取消。请重试。",
                    severity="WARN",
                ))
                orphan_cancelled += 1
                logger.warning("Recovery: orphan WAITING_APPROVAL task %s cancelled", task_id[:12])

        if recovered > 0:
            logger.info("Startup recovery: %d task(s) with checkpoints kept", recovered)
        if failed > 0:
            logger.info("Startup recovery: %d task(s) without checkpoints marked FAILED", failed)
        if orphan_cancelled > 0:
            logger.warning("Startup recovery: %d orphan WAITING_APPROVAL task(s) cancelled", orphan_cancelled)
        if orphan_kept > 0:
            logger.info("Startup recovery: %d WAITING_APPROVAL task(s) with valid approvals kept", orphan_kept)

    def _register_builtin_tools(self, allowed_paths: list[Path]) -> None:
        self.tool_registry.register(FileReadTool(allowed_paths))
        self.tool_registry.register(FileWriteTool(allowed_paths))
        self.tool_registry.register(CommandRunTool(allowed_paths))
        self.tool_registry.register(BrowserReadTool())
        self.tool_registry.register(BrowserScreenshotTool(self.settings.artifacts_dir))
        self.tool_registry.register(KnowledgeSearchTool(self.settings.knowledge_dir))
        # Document processing tools
        self.tool_registry.register(PdfReadTool(allowed_paths))
        self.tool_registry.register(DocxReadTool(allowed_paths))
        self.tool_registry.register(ExcelReadTool(allowed_paths))
        # Playwright browser automation tools
        self.tool_registry.register(BrowserNavigateTool())
        self.tool_registry.register(BrowserClickTool())
        self.tool_registry.register(BrowserFillTool())
        self.tool_registry.register(BrowserScreenshotFullTool(self.settings.artifacts_dir))
        # Phase 1: Extended filesystem tools
        self.tool_registry.register(FileListTool(allowed_paths))
        self.tool_registry.register(FileSearchTool(allowed_paths))
        self.tool_registry.register(FileDeleteTool(allowed_paths, self.settings.backups_dir))
        self.tool_registry.register(FileMetaTool(allowed_paths))
        # Phase 1: Network tools
        self.tool_registry.register(HttpRequestTool())
        self.tool_registry.register(WebSearchTool())
        self.tool_registry.register(HttpDownloadTool(allowed_paths))
        # Phase 1: Code sandbox
        self.tool_registry.register(CodeExecuteTool())
        # Workflow discovery — bridges Web UI marketplace with Agent tools
        marketplace_dir = self.settings.data_dir / "marketplace"
        marketplace_dir.mkdir(parents=True, exist_ok=True)
        self.tool_registry.register(WorkflowSearchTool(self.db, marketplace_dir))
        self.tool_registry.register(WorkflowExecuteTool(self.db, None))  # runtime lazily set below

