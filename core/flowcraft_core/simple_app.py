from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from flowcraft_core.config.settings import load_settings
from flowcraft_core.domain.simple_models import (
    AgentRequest,
    ExecutionPlan,
    PlanStep,
    Task,
    TaskBrief,
    TraceEvent,
    now_utc,
)
from flowcraft_core.models.gateway import DEFAULT_DEEPSEEK_PROFILE, ModelGateway
from flowcraft_core.models.adapters.openai_compatible import OpenAICompatibleAdapter
from flowcraft_core.storage.database import Database


class SimpleFlowCraft:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.db = Database(self.settings.database_path)
        self.db.initialize()
        self.model_gateway = ModelGateway()
        self._configure_model()

    def _configure_model(self) -> None:
        """从环境变量读取 API Key 并配置模型网关."""
        api_key = os.environ.get("FLOWCRAFT_DEEPSEEK_API_KEY")
        if api_key:
            adapter = OpenAICompatibleAdapter(DEFAULT_DEEPSEEK_PROFILE, api_key=api_key)
            self.model_gateway.configure(adapter, DEFAULT_DEEPSEEK_PROFILE)

    async def create_task(self, request: AgentRequest) -> Task:
        task = Task(
            session_id=request.session_id,
            title=request.raw_input.strip()[:40] or "新任务",
            objective=request.raw_input,
        )
        self._save_task(task)
        self._event(task, "task.created", "任务已创建", task.objective)

        brief_payload = await self.model_gateway.generate_structured(request.raw_input, "TaskBrief")
        brief = TaskBrief(task_id=task.task_id, **brief_payload)
        task.status = "INTENT_RECOGNIZED"
        task.task_type = brief.task_type
        task.risk_level = brief.risk_level
        task.success_criteria = brief.success_criteria
        task.updated_at = now_utc()
        self._update_task(task)
        self.db.insert_json(
            "task_briefs",
            {
                "task_id": task.task_id,
                "data_json": brief.model_dump(mode="json"),
                "created_at": now_utc().isoformat(),
            },
        )
        self._event(task, "intent.recognized", "已识别任务意图", f"类型：{brief.task_type}，风险：{brief.risk_level}", brief.model_dump(mode="json"))

        plan_payload = await self.model_gateway.generate_structured(
            json.dumps(brief.model_dump(mode="json"), ensure_ascii=False),
            "ExecutionPlan",
        )
        steps = [PlanStep(**item) for item in plan_payload["steps"]]
        plan = ExecutionPlan(
            task_id=task.task_id,
            mode=plan_payload["mode"],
            goal=plan_payload["goal"],
            steps=steps,
            constraints=brief.constraints,
            approval_points=[step.title for step in steps if step.approval_required],
            stop_conditions=["满足成功标准", "用户取消任务", "策略阻止继续"],
            success_criteria=brief.success_criteria,
        )
        for step in steps:
            step.plan_id = plan.plan_id
        self._save_plan(plan)
        task.current_plan_id = plan.plan_id
        task.status = "PLANNED"
        task.updated_at = now_utc()
        self._update_task(task)
        self._event(task, "plan.created", "已生成执行计划", f"计划包含 {len(plan.steps)} 个步骤。", plan.model_dump(mode="json"))

        high_risk = any(step.risk_level in ["HIGH", "CRITICAL"] for step in plan.steps)
        if high_risk:
            task.status = "WAITING_APPROVAL"
            task.updated_at = now_utc()
            self._update_task(task)
            self._event(task, "policy.checked", "已完成计划策略检查", "计划包含高风险步骤，需要用户确认。")
            self._event(task, "approval.requested", "需要用户确认", "该计划包含高风险步骤，需要你确认后再继续。")
            return task

        self._event(task, "policy.checked", "已完成计划策略检查", "计划未发现需要阻止的风险。")
        await self._execute_plan(task, brief, plan)
        return task

    def get_task(self, task_id: str) -> dict | None:
        row = self.db.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if not row:
            return None
        item = dict(row)
        item["constraints"] = json.loads(item.pop("constraints_json"))
        item["success_criteria"] = json.loads(item.pop("success_criteria_json"))
        return item

    def get_events(self, task_id: str) -> list[dict]:
        rows = self.db.fetch_all("SELECT * FROM trace_events WHERE task_id = ? ORDER BY created_at ASC", (task_id,))
        events = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            events.append(item)
        return events

    async def approve_task(self, task_id: str) -> Task:
        row = self.db.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if not row:
            raise RuntimeError("任务不存在。")
        task = Task(
            task_id=row["id"],
            session_id=row["session_id"],
            user_id=row["user_id"],
            title=row["title"],
            objective=row["objective"],
            task_type=row["task_type"],
            status=row["status"],
            risk_level=row["risk_level"],
            constraints=json.loads(row["constraints_json"]),
            success_criteria=json.loads(row["success_criteria_json"]),
            current_plan_id=row["current_plan_id"],
            failed_reason=row["failed_reason"],
        )
        if task.status != "WAITING_APPROVAL":
            raise RuntimeError("当前任务不在等待审批状态。")
        self._event(task, "approval.resolved", "用户已批准", "用户批准执行高风险步骤。")
        task.status = "EXECUTING"
        task.updated_at = now_utc()
        self._update_task(task)
        try:
            if task.task_type == "LOCAL_OPERATION":
                result = self._run_command(task, approval_granted=True)
            else:
                result = self._run_capability(task, TaskBrief(task_id=task.task_id, objective=task.objective, task_type=task.task_type, risk_level=task.risk_level))
            task.status = "COMPLETED"
            task.completed_at = now_utc()
            task.updated_at = now_utc()
            self._update_task(task)
            self._event(task, "step.completed", "执行步骤完成", result["summary"], result)
            self._event(task, "task.completed", "任务已完成", result["final_answer"], result)
            return task
        except Exception as exc:
            task.status = "FAILED"
            task.failed_reason = str(exc)
            task.updated_at = now_utc()
            self._update_task(task)
            self._event(task, "task.failed", "任务失败", str(exc), severity="ERROR")
            return task

    def list_tools(self) -> list[dict]:
        return [
            {"tool_name": "file.read", "risk_level": "LOW", "description": "读取授权目录内的文本文件。"},
            {"tool_name": "file.write", "risk_level": "MEDIUM", "description": "在授权目录内创建或覆盖文本文件。"},
            {"tool_name": "command.run", "risk_level": "HIGH", "description": "在授权工作目录执行用户批准的命令。"},
        ]

    async def _execute_plan(self, task: Task, brief: TaskBrief, plan: ExecutionPlan) -> None:
        task.status = "EXECUTING"
        task.updated_at = now_utc()
        self._update_task(task)
        self._event(task, "step.started", "开始执行计划", f"执行计划：{plan.goal}")

        try:
            result = self._run_capability(task, brief)
            task.status = "COMPLETED"
            task.completed_at = now_utc()
            task.updated_at = now_utc()
            self._update_task(task)
            self._event(task, "step.completed", "执行步骤完成", result["summary"], result)
            self._event(task, "task.completed", "任务已完成", result["final_answer"], result)
        except Exception as exc:
            task.status = "FAILED"
            task.failed_reason = str(exc)
            task.updated_at = now_utc()
            self._update_task(task)
            self._event(task, "step.failed", "执行步骤失败", str(exc), severity="ERROR")
            self._event(task, "task.failed", "任务失败", str(exc), severity="ERROR")

    def _run_capability(self, task: Task, brief: TaskBrief) -> dict:
        text = task.objective.strip()
        lower = text.lower()
        if brief.task_type == "QA":
            return {
                "summary": "已生成说明。",
                "final_answer": self._answer_question(text),
            }
        if brief.task_type == "FILE_TASK":
            if self._contains_any(text, lower, ["列出", "目录", "有哪些文件"], ["list", "dir"]):
                return self._list_directory(task)
            if self._contains_any(text, lower, ["写入", "创建", "保存", "生成"], ["write", "create", "save", "generate"]):
                return self._write_file(task)
            if self._contains_any(text, lower, ["删除", "覆盖"], ["delete", "remove", "overwrite"]):
                raise RuntimeError("删除或覆盖任务需要审批执行器支持。当前版本已拦截，未执行任何文件删除。")
            return self._read_file(task)
        if brief.task_type == "LOCAL_OPERATION":
            return self._run_command(task)
        if brief.task_type == "BROWSER_TASK":
            return {
                "summary": "浏览器任务已识别。",
                "final_answer": "当前零依赖版本尚未接入浏览器自动化。后续会接入 Playwright；现在已完成意图识别和风险控制。",
            }
        return {
            "summary": "任务已处理。",
            "final_answer": "FlowCraft 已完成当前 MVP 可处理范围内的任务。",
        }

    def _answer_question(self, text: str) -> str:
        if "flowcraft" in text.lower() or "FlowCraft" in text:
            return (
                "FlowCraft 是一个 Harness-first 的本地 Agent 工作流框架。"
                "它的目标不是让模型自由调用工具，而是通过任务状态、规划、策略、审批、工具网关和审计时间线，"
                "让个人和小团队安全地构建可复用的 AI 工作流。"
            )
        return "当前开发版本已收到你的问题，但还没有接入真实模型。下一阶段接入 Model Gateway 后会生成完整回答。"

    def _read_file(self, task: Task) -> dict:
        path = self._extract_path(task.objective)
        if path is None:
            raise RuntimeError("没有识别到要读取的文件路径。请在任务里写出完整路径。")
        self._ensure_allowed(path)
        if not path.exists() or not path.is_file():
            raise RuntimeError(f"文件不存在：{path}")
        content = path.read_text(encoding="utf-8", errors="replace")
        preview = content[:4000]
        if len(content) > len(preview):
            preview += "\n\n[内容过长，已截断预览]"
        self._event(task, "tool.requested", "请求工具调用", "file.read", {"path": str(path)})
        self._event(task, "tool.completed", "工具调用完成", f"已读取文件：{path}", {"content_preview": preview})
        return {
            "summary": f"已读取文件：{path}",
            "final_answer": preview,
            "path": str(path),
        }

    def _write_file(self, task: Task) -> dict:
        path = self._extract_path(task.objective)
        if path is None:
            path = self.settings.allowed_paths[0] / "flowcraft-output.md"
        self._ensure_allowed(path)
        content = self._extract_quoted_content(task.objective)
        if not content:
            content = f"# FlowCraft 输出\n\n任务：{task.objective}\n\n这是 FlowCraft MVP 生成的文件内容。\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        backup_path = None
        if path.exists():
            backup_path = path.with_suffix(path.suffix + ".flowcraft.bak")
            backup_path.write_bytes(path.read_bytes())
        self._event(task, "tool.requested", "请求工具调用", "file.write", {"path": str(path)})
        path.write_text(content, encoding="utf-8")
        self._event(
            task,
            "tool.completed",
            "工具调用完成",
            f"已写入文件：{path}",
            {"path": str(path), "backup_path": str(backup_path) if backup_path else None, "content_preview": content[:1000]},
        )
        return {
            "summary": f"已写入文件：{path}",
            "final_answer": f"已写入文件：{path}" + (f"\n已创建备份：{backup_path}" if backup_path else ""),
            "path": str(path),
            "backup_path": str(backup_path) if backup_path else None,
        }

    def _list_directory(self, task: Task) -> dict:
        path = self._extract_path(task.objective) or self.settings.allowed_paths[0]
        self._ensure_allowed(path)
        if not path.exists() or not path.is_dir():
            raise RuntimeError(f"目录不存在：{path}")
        items = []
        for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))[:100]:
            items.append({"name": child.name, "type": "dir" if child.is_dir() else "file", "path": str(child)})
        self._event(task, "tool.requested", "请求工具调用", "file.list", {"path": str(path)})
        self._event(task, "tool.completed", "工具调用完成", f"已列出目录：{path}", {"items": items})
        lines = [f"{item['type']}: {item['name']}" for item in items]
        return {
            "summary": f"已列出目录：{path}",
            "final_answer": "\n".join(lines) if lines else "目录为空。",
            "items": items,
        }

    def _run_command(self, task: Task, approval_granted: bool = False) -> dict:
        command = self._extract_command(task.objective)
        if not command:
            raise RuntimeError("没有识别到要执行的命令。请使用：run command: 具体命令")
        if not approval_granted:
            raise RuntimeError("命令执行需要用户审批。")
        blocked = ["format", "del /s", "rm -rf", "reg delete", "shutdown", "powershell -enc"]
        if any(token in command.lower() for token in blocked):
            raise RuntimeError("命令被安全策略拦截，未执行。")
        self._event(task, "tool.requested", "请求工具调用", "command.run", {"command": command, "cwd": str(self.settings.allowed_paths[0])})
        completed = subprocess.run(
            command,
            cwd=str(self.settings.allowed_paths[0]),
            shell=True,
            text=True,
            capture_output=True,
            timeout=30,
        )
        payload = {
            "returncode": completed.returncode,
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-4000:],
        }
        self._event(task, "tool.completed", "工具调用完成", "命令执行完成。", payload)
        final = payload["stdout"] or payload["stderr"] or f"命令退出码：{completed.returncode}"
        return {
            "summary": "命令执行完成。" if completed.returncode == 0 else "命令执行失败。",
            "final_answer": final,
            **payload,
        }

    def _extract_path(self, text: str) -> Path | None:
        text_for_path = re.split(r"\s+(?:内容为|内容是|写入内容|with content)\s*[:：]?", text, maxsplit=1, flags=re.I)[0]
        quoted = re.findall(r'["“](.+?)["”]', text)
        for item in quoted:
            if self._looks_like_path(item):
                return Path(item)
        patterns = [
            r"[A-Za-z]:\\[^\s\"<>|，。；;]+",
            r"[\w.-]+\.(?:txt|md|json|csv|log|py|js|html|docx|xlsx|pdf)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text_for_path)
            if match:
                return Path(match.group(0).strip("。，,"))
        return None

    def _extract_command(self, text: str) -> str:
        markers = ["run command:", "执行命令：", "执行命令:", "命令：", "命令:"]
        lower = text.lower()
        for marker in markers:
            idx = lower.find(marker.lower())
            if idx >= 0:
                return text[idx + len(marker):].strip()
        return ""

    def _extract_quoted_content(self, text: str) -> str:
        markers = ["内容为", "内容是", "写入内容", "content:"]
        for marker in markers:
            idx = text.lower().find(marker.lower())
            if idx >= 0:
                content = text[idx + len(marker):].strip()
                content = content.lstrip(":：").strip()
                if (content.startswith("“") and content.endswith("”")) or (
                    content.startswith('"') and content.endswith('"')
                ):
                    content = content[1:-1]
                return content
        return ""

    def _ensure_allowed(self, path: Path) -> None:
        resolved = path.resolve()
        for allowed in self.settings.allowed_paths:
            try:
                resolved.relative_to(allowed.resolve())
                return
            except ValueError:
                continue
        raise RuntimeError(f"路径不在授权工作目录内：{resolved}")

    @staticmethod
    def _looks_like_path(text: str) -> bool:
        return bool(re.search(r"^[A-Za-z]:\\|[/\\]|\.[A-Za-z0-9]{1,6}$", text))

    @staticmethod
    def _contains_any(text: str, lower: str, zh_words: list[str], en_words: list[str]) -> bool:
        return any(word in text for word in zh_words) or any(word in lower for word in en_words)

    def _save_task(self, task: Task) -> None:
        self.db.insert_json(
            "tasks",
            {
                "id": task.task_id,
                "session_id": task.session_id,
                "user_id": task.user_id,
                "title": task.title,
                "objective": task.objective,
                "task_type": task.task_type,
                "status": task.status,
                "risk_level": task.risk_level,
                "constraints_json": task.constraints,
                "success_criteria_json": task.success_criteria,
                "current_plan_id": task.current_plan_id,
                "failed_reason": task.failed_reason,
                "created_at": task.created_at.isoformat(),
                "updated_at": task.updated_at.isoformat(),
                "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            },
        )

    def _update_task(self, task: Task) -> None:
        self.db.update(
            "tasks",
            "id",
            task.task_id,
            {
                "task_type": task.task_type,
                "status": task.status,
                "risk_level": task.risk_level,
                "success_criteria_json": task.success_criteria,
                "current_plan_id": task.current_plan_id,
                "failed_reason": task.failed_reason,
                "updated_at": task.updated_at.isoformat(),
                "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            },
        )

    def _save_plan(self, plan: ExecutionPlan) -> None:
        self.db.insert_json(
            "plans",
            {
                "id": plan.plan_id,
                "task_id": plan.task_id,
                "mode": plan.mode,
                "goal": plan.goal,
                "data_json": plan.model_dump(mode="json"),
                "status": plan.status,
                "version": plan.version,
                "created_at": plan.created_at.isoformat(),
            },
        )
        for step in plan.steps:
            self.db.insert_json(
                "plan_steps",
                {
                    "id": step.step_id,
                    "plan_id": plan.plan_id,
                    "task_id": plan.task_id,
                    "step_index": step.index,
                    "title": step.title,
                    "objective": step.objective,
                    "action_type": step.action_type,
                    "risk_level": step.risk_level,
                    "approval_required": 1 if step.approval_required else 0,
                    "status": step.status,
                    "data_json": step.model_dump(mode="json"),
                    "created_at": plan.created_at.isoformat(),
                    "updated_at": plan.created_at.isoformat(),
                },
            )

    def _event(self, task: Task, event_type: str, title: str, message: str, payload: dict | None = None, severity: str = "INFO") -> None:
        event = TraceEvent(
            task_id=task.task_id,
            session_id=task.session_id,
            event_type=event_type,
            title=title,
            message=message,
            payload=payload or {},
            severity=severity,
        )
        self.db.insert_json(
            "trace_events",
            {
                "id": event.event_id,
                "task_id": event.task_id,
                "session_id": event.session_id,
                "event_type": event.event_type,
                "title": event.title,
                "message": event.message,
                "payload_json": event.payload,
                "severity": event.severity,
                "created_at": event.created_at.isoformat(),
            },
        )
