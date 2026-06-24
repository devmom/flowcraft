from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from flowcraft_core.app import FlowCraftApp
from flowcraft_core.config.settings import load_settings
from flowcraft_core.domain.schemas import AgentRequest, TraceEvent
from flowcraft_core.domain.enums import TaskStatus


class CreateTaskRequest(BaseModel):
    session_id: str = "default"
    input: str = Field(min_length=1)
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class CreateTaskResponse(BaseModel):
    task_id: str
    status: str
    title: str


class ApprovalResolveRequest(BaseModel):
    decision: str = Field(pattern=r"^(APPROVED|REJECTED)$")
    comment: str = ""


class ModelConfigRequest(BaseModel):
    provider: str
    model_name: str
    base_url: str = ""
    api_key: str = ""
    enabled: bool = True


class SettingValueRequest(BaseModel):
    value: Any


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    app.state.flowcraft = FlowCraftApp(settings)
    await app.state.flowcraft.startup()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="FlowCraft Local API", version="0.1.0", lifespan=lifespan)

    # ── Health ──────────────────────────────────────────────

    @app.get("/health")
    async def health():
        flowcraft: FlowCraftApp = app.state.flowcraft
        model_configured = bool(flowcraft.model_gateway.provider_name != "deterministic-dev")
        return {
            "status": "ok",
            "version": flowcraft.settings.version,
            "db_status": "ok",
            "model_configured": model_configured,
            "provider": flowcraft.model_gateway.provider_name,
            "model": flowcraft.model_gateway._profile.model_id if model_configured else "none",
            "server": "fastapi",
            "data_dir": str(flowcraft.settings.data_dir),
        }

    # ── Tasks ───────────────────────────────────────────────

    @app.get("/api/tasks")
    async def list_tasks():
        flowcraft: FlowCraftApp = app.state.flowcraft
        rows = flowcraft.db.fetch_all(
            "SELECT id as task_id, title, status, objective, risk_level, created_at, updated_at "
            "FROM tasks ORDER BY created_at DESC LIMIT 50", ()
        )
        return {"tasks": [dict(row) for row in rows]}

    @app.post("/api/tasks", response_model=CreateTaskResponse)
    async def create_task(payload: CreateTaskRequest):
        flowcraft: FlowCraftApp = app.state.flowcraft
        request = AgentRequest(
            session_id=payload.session_id,
            raw_input=payload.input,
            attachments=payload.attachments,
        )
        task = await flowcraft.runtime.start_task(request)
        return CreateTaskResponse(task_id=task.task_id, status=task.status.value, title=task.title)

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str):
        flowcraft: FlowCraftApp = app.state.flowcraft
        task = flowcraft.task_store.get_task_row(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        # 兼容 web UI 的字段名 (id, task_id 双返回)
        task_dict = dict(task)
        if "id" in task_dict and "task_id" not in task_dict:
            task_dict["task_id"] = task_dict["id"]
        context = flowcraft.memory.get_task_context(task_id)
        return {"task": task_dict, "brief": context.get("brief"), "current_plan": context.get("plan"), "current_step": context.get("steps", [None])[0] if context.get("steps") else None}

    @app.get("/api/tasks/{task_id}/model-stats")
    async def get_task_model_stats(task_id: str):
        """Return aggregated token/cost stats for a task from model_calls table."""
        flowcraft: FlowCraftApp = app.state.flowcraft
        try:
            rows = flowcraft.db.fetch_all(
                "SELECT prompt_tokens, completion_tokens, cost_estimate "
                "FROM model_calls WHERE task_id = ?",
                (task_id,)
            )
        except Exception:
            return {"task_id": task_id, "total_tokens": 0, "total_cost": 0.0, "call_count": 0}

        total_prompt = sum(r["prompt_tokens"] or 0 for r in rows)
        total_completion = sum(r["completion_tokens"] or 0 for r in rows)
        total_cost = sum(r["cost_estimate"] or 0.0 for r in rows)
        return {
            "task_id": task_id,
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "total_tokens": total_prompt + total_completion,
            "total_cost": round(total_cost, 6),
            "call_count": len(rows),
        }

    @app.get("/api/tasks/{task_id}/events")
    async def get_task_events(
        task_id: str,
        event_type: str | None = Query(None),
        severity: str | None = Query(None),
        limit: int = Query(50, ge=1, le=200),
    ):
        flowcraft: FlowCraftApp = app.state.flowcraft
        events = flowcraft.events.list_for_task(task_id)
        if event_type:
            events = [e for e in events if e.get("event_type") == event_type]
        if severity:
            events = [e for e in events if e.get("severity") == severity]
        return {"events": events[:limit]}

    @app.get("/api/tasks/{task_id}/stream")
    async def stream_task_events(task_id: str, request: Request):
        """SSE endpoint: real-time task event streaming.

        Replaces polling — frontend receives events as they happen.
        Events are newline-delimited JSON (SSE format).
        """
        flowcraft: FlowCraftApp = app.state.flowcraft

        async def event_generator():
            last_event_idx = 0
            while True:
                if await request.is_disconnected():
                    break

                events = flowcraft.events.list_for_task(task_id)
                new_events = events[last_event_idx:]
                for evt in new_events:
                    data = json.dumps(evt, ensure_ascii=False, default=str)
                    yield f"event: {evt.get('event_type', 'message')}\n"
                    yield f"data: {data}\n\n"
                    last_event_idx += 1

                # Check task completion
                task = flowcraft.task_store.get_task_row(task_id)
                if task and task.get("status") in ("COMPLETED", "FAILED", "CANCELLED"):
                    yield f"event: done\n"
                    yield f"data: {json.dumps({'status': task['status']})}\n\n"
                    break

                await asyncio.sleep(0.5)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/tasks/{task_id}/approve")
    async def approve_task(task_id: str):
        """批准等待审批的任务，继续执行."""
        flowcraft: FlowCraftApp = app.state.flowcraft
        task = flowcraft.task_store.get_task_row(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        if task["status"] != "WAITING_APPROVAL":
            raise HTTPException(400, f"Task is not waiting for approval: {task['status']}")

        # 信任此会话，后续高风险操作自动批准
        flowcraft.policy_engine.trust_session(task.get("session_id", ""))

        # 将任务相关的所有待处理审批标记为已批准
        flowcraft.db.execute(
            "UPDATE approval_requests SET status = 'APPROVED', resolved_at = ? "
            "WHERE task_id = ? AND status = 'PENDING'",
            (datetime.now(timezone.utc).isoformat(), task_id))

        # 记录批准
        flowcraft.events.record(
            TraceEvent(
                task_id=task_id,
                event_type="approval.resolved",
                title="用户已批准（会话已信任）",
                message="用户批准执行。同会话后续操作将自动批准。",
            )
        )

        # 重新加载完整的 task 对象并继续执行
        from flowcraft_core.domain.schemas import Task
        row = flowcraft.db.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if not row:
            raise HTTPException(404, "Task not found")
        full_task = Task(
            task_id=row["id"],
            session_id=row["session_id"],
            user_id=row["user_id"],
            title=row["title"],
            objective=row["objective"],
            task_type=row.get("task_type", "UNKNOWN"),
            status=TaskStatus(row["status"]),
            risk_level=row.get("risk_level", "LOW"),
        )
        full_task.status = TaskStatus.EXECUTING
        flowcraft.task_store.update_task(full_task)

        # 获取 brief 和 plan 并执行
        import json
        brief_row = flowcraft.db.fetch_one("SELECT * FROM task_briefs WHERE task_id = ?", (task_id,))
        plan_row = flowcraft.db.fetch_one("SELECT * FROM plans WHERE task_id = ? ORDER BY created_at DESC LIMIT 1", (task_id,))

        if brief_row and plan_row:
            from flowcraft_core.domain.schemas import TaskBrief, ExecutionPlan, PlanStep
            brief_data = json.loads(dict(brief_row).get("data_json", "{}"))
            brief_data.pop("task_id", None)
            brief = TaskBrief(task_id=task_id, **brief_data)

            plan_data = json.loads(dict(plan_row).get("data_json", "{}"))
            steps = [PlanStep(**step) for step in plan_data.get("steps", [])]
            plan = ExecutionPlan(
                task_id=task_id,
                mode=plan_data["mode"],
                goal=plan_data["goal"],
                steps=steps,
            )

            try:
                task_result = await flowcraft.execution_engine.execute_plan(full_task, brief, plan)
                flowcraft.task_store.update_task(task_result)
                return {"task_id": task_id, "status": task_result.status.value, "title": task_result.title}
            except Exception as exc:
                full_task.status = TaskStatus.FAILED
                full_task.failed_reason = str(exc)
                flowcraft.task_store.update_task(full_task)
                return {"task_id": task_id, "status": "FAILED", "title": full_task.title}

        return {"task_id": task_id, "status": "COMPLETED", "title": full_task.title}

    @app.post("/api/tasks/{task_id}/pause")
    async def pause_task(task_id: str):
        flowcraft: FlowCraftApp = app.state.flowcraft
        task = flowcraft.task_store.get_task_row(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        if task["status"] not in {"EXECUTING", "WAITING_TOOL", "OBSERVING"}:
            raise HTTPException(400, f"Cannot pause task in status: {task['status']}")
        flowcraft.runtime.events.record(
            TraceEvent(
                task_id=task_id,
                event_type="task.paused",
                title="任务已暂停",
                message="用户手动暂停任务。",
            )
        )
        return {"task_id": task_id, "status": "PAUSED"}

    @app.post("/api/tasks/{task_id}/resume")
    async def resume_task(task_id: str):
        flowcraft: FlowCraftApp = app.state.flowcraft
        task = flowcraft.task_store.get_task_row(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        if task["status"] != "PAUSED":
            raise HTTPException(400, f"Cannot resume task in status: {task['status']}")
        flowcraft.runtime.events.record(
            TraceEvent(
                task_id=task_id,
                event_type="task.resumed",
                title="任务已恢复",
                message="用户手动恢复任务。",
            )
        )
        return {"task_id": task_id, "status": "EXECUTING"}

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str):
        flowcraft: FlowCraftApp = app.state.flowcraft
        task = flowcraft.task_store.get_task_row(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        if task["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
            raise HTTPException(400, f"Cannot cancel task in status: {task['status']}")
        flowcraft.runtime.events.record(
            TraceEvent(
                task_id=task_id,
                event_type="task.cancelled",
                title="任务已取消",
                message="用户手动取消任务。",
                severity="WARN",
            )
        )
        return {"task_id": task_id, "status": "CANCELLED"}

    # ── Approvals ───────────────────────────────────────────

    @app.post("/api/approvals/{approval_id}/resolve")
    async def resolve_approval(approval_id: str, payload: ApprovalResolveRequest):
        flowcraft: FlowCraftApp = app.state.flowcraft
        row = flowcraft.events.db.fetch_one(
            "SELECT * FROM approval_requests WHERE id = ?", (approval_id,)
        )
        if not row:
            raise HTTPException(404, "Approval not found")
        approval = dict(row)
        if approval["status"] != "PENDING":
            raise HTTPException(400, f"Approval already {approval['status']}")
        new_status = "APPROVED" if payload.decision == "APPROVED" else "REJECTED"
        flowcraft.events.db.update(
            "approval_requests", "id", approval_id,
            {"status": new_status, "resolved_at": flowcraft.domain.schemas.now_utc().isoformat()},
        )
        flowcraft.events.record(
            flowcraft.domain.schemas.TraceEvent(  # type: ignore[attr-defined]
                task_id=approval["task_id"],
                event_type="approval.resolved",
                title=f"审批已{payload.decision}",
                message=payload.comment or f"用户{payload.decision}了审批请求。",
                payload={"decision": payload.decision, "comment": payload.comment},
            )
        )
        return {"approval_id": approval_id, "status": new_status}

    # ── Tools ───────────────────────────────────────────────

    @app.get("/api/tools")
    async def list_tools():
        flowcraft: FlowCraftApp = app.state.flowcraft
        return {"tools": flowcraft.tool_registry.list_definitions()}

    # ── Settings / Models ───────────────────────────────────

    @app.get("/api/settings/models")
    async def get_model_config():
        flowcraft: FlowCraftApp = app.state.flowcraft
        models = flowcraft.secrets.get_setting("models", [])
        return {"models": models}

    @app.post("/api/settings/models")
    async def set_model_config(payload: ModelConfigRequest):
        flowcraft: FlowCraftApp = app.state.flowcraft
        # Store API key in secrets
        if payload.api_key:
            flowcraft.secrets.set(f"model:{payload.provider}:{payload.model_name}:api_key", payload.api_key)
        # Store model config (without key)
        config = payload.model_dump()
        config.pop("api_key", None)
        models = flowcraft.secrets.get_setting("models", [])
        existing_idx = next(
            (i for i, m in enumerate(models)
             if m["provider"] == payload.provider and m["model_name"] == payload.model_name),
            None,
        )
        if existing_idx is not None:
            models[existing_idx] = config
        else:
            models.append(config)
        flowcraft.secrets.set_setting("models", models)
        return {"status": "ok", "provider": payload.provider, "model_name": payload.model_name}

    @app.post("/api/settings/models/test")
    async def test_model_connection(payload: ModelConfigRequest):
        flowcraft: FlowCraftApp = app.state.flowcraft
        # MVP: simple connectivity check via health endpoint or fallback
        import asyncio
        try:
            result = await flowcraft.model_gateway.generate_text("Hello, respond with 'ok' only.")
            return {"status": "ok" if result else "no_response", "message": "Connection test completed."}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    @app.get("/api/settings/tools")
    async def get_tool_settings():
        flowcraft: FlowCraftApp = app.state.flowcraft
        return {"tools": flowcraft.secrets.get_setting("tool_permissions", {})}

    @app.post("/api/settings/tools")
    async def set_tool_settings(payload: dict[str, Any]):
        flowcraft: FlowCraftApp = app.state.flowcraft
        flowcraft.secrets.set_setting("tool_permissions", payload)
        return {"status": "ok"}

    # ── Settings / General ──────────────────────────────────

    @app.get("/api/settings")
    async def get_all_settings():
        flowcraft: FlowCraftApp = app.state.flowcraft
        return {"settings": flowcraft.secrets.all_settings()}

    @app.post("/api/settings/{key}")
    async def set_setting(key: str, payload: SettingValueRequest):
        flowcraft: FlowCraftApp = app.state.flowcraft
        flowcraft.secrets.set_setting(key, payload.value)
        return {"key": key, "status": "ok"}

    # ── Allowed Paths (runtime authorization) ─────────────────

    @app.get("/api/settings/allowed-paths")
    async def get_allowed_paths():
        """Get the current list of allowed paths."""
        flowcraft: FlowCraftApp = app.state.flowcraft
        return {
            "allowed_paths": [str(p) for p in flowcraft.settings.allowed_paths],
            "workspace": str(flowcraft.settings.allowed_paths[0]) if flowcraft.settings.allowed_paths else "",
        }

    @app.post("/api/settings/allowed-paths")
    async def add_allowed_path(payload: dict[str, Any]):
        """Dynamically add a path to the allowed list at runtime.

        Request: {"path": "D:\\my_folder"}
        The path is added immediately; all file tools share the same list reference.
        """
        flowcraft: FlowCraftApp = app.state.flowcraft
        path_str = str(payload.get("path", "")).strip()
        if not path_str:
            raise HTTPException(400, "Missing 'path' field.")

        from pathlib import Path
        target = Path(path_str)
        if not target.exists():
            raise HTTPException(400, f"Path does not exist: {path_str}")

        added = flowcraft.settings.add_allowed_path(target)
        return {
            "status": "ok",
            "added": added,
            "path": str(target.resolve()),
            "allowed_paths": [str(p) for p in flowcraft.settings.allowed_paths],
        }

    # ── Sessions ─────────────────────────────────────────────

    @app.get("/api/sessions")
    async def list_sessions():
        """Return all sessions with correct timestamps for sidebar display."""
        flowcraft: FlowCraftApp = app.state.flowcraft
        rows = flowcraft.db.fetch_all(
            "SELECT id, title, created_at, updated_at, last_task_id "
            "FROM sessions ORDER BY updated_at DESC LIMIT 50"
        )
        return {"sessions": [dict(r) for r in rows]}

    # ── Memories ────────────────────────────────────────────

    @app.get("/api/memories")
    async def list_memories(
        memory_type: str | None = Query(None),
        scope_id: str | None = Query(None),
    ):
        flowcraft: FlowCraftApp = app.state.flowcraft
        return {"memories": flowcraft.memory.list_memories(memory_type, scope_id)}

    @app.delete("/api/memories/{memory_id}")
    async def delete_memory(memory_id: str):
        flowcraft: FlowCraftApp = app.state.flowcraft
        flowcraft.memory.soft_delete(memory_id)
        return {"memory_id": memory_id, "status": "deleted"}

    # ── Task Reports ─────────────────────────────────────────

    @app.get("/api/tasks/{task_id}/report")
    async def task_report(task_id: str, format: str = Query("markdown")):
        flowcraft: FlowCraftApp = app.state.flowcraft
        from flowcraft_core.simple_server import _build_markdown_report, _build_html_report
        task_row = flowcraft.task_store.get_task_row(task_id)
        if not task_row:
            raise HTTPException(404, "Task not found")
        task_dict = dict(task_row)
        events = flowcraft.events.list_for_task(task_id)
        if format == "html":
            from fastapi.responses import HTMLResponse
            return HTMLResponse(_build_html_report(task_dict, events))
        return {"report": _build_markdown_report(task_dict, events), "format": "markdown"}

    # ── Task Replay ──────────────────────────────────────────

    @app.get("/api/tasks/{task_id}/replay")
    async def task_replay(task_id: str):
        flowcraft: FlowCraftApp = app.state.flowcraft
        return flowcraft.task_replay.get_timeline(task_id)

    @app.post("/api/tasks/{task_id}/extract-memory")
    async def extract_task_memory(task_id: str):
        flowcraft: FlowCraftApp = app.state.flowcraft
        task_row = flowcraft.task_store.get_task_row(task_id)
        if not task_row:
            raise HTTPException(404, "Task not found")
        task_dict = dict(task_row)
        events = flowcraft.events.list_for_task(task_id)
        output = "\n".join(e.get("message", "") for e in events if e.get("event_type") in ("step.answer", "task.completed"))
        entries = flowcraft.long_term_memory.extract_from_task(
            task_id, task_dict.get("title", ""), output, task_dict.get("session_id", "default"))
        return {"extracted": len(entries), "memories": [{"title": e.title, "content": e.content[:100]} for e in entries]}

    # ── Workflows ────────────────────────────────────────────

    @app.get("/api/workflows")
    async def list_workflows():
        """List all saved workflow templates."""
        flowcraft: FlowCraftApp = app.state.flowcraft
        rows = flowcraft.db.fetch_all(
            "SELECT * FROM workflow_templates WHERE status != 'deleted' "
            "ORDER BY updated_at DESC")
        return {"workflows": [dict(r) for r in rows]}

    @app.get("/api/workflows/{workflow_id}")
    async def get_workflow(workflow_id: str):
        """Get a single workflow by ID."""
        flowcraft: FlowCraftApp = app.state.flowcraft
        row = flowcraft.db.fetch_one(
            "SELECT * FROM workflow_templates WHERE id = ?", (workflow_id,))
        if not row:
            raise HTTPException(404, "Workflow not found")
        return {"workflow": dict(row)}

    @app.delete("/api/workflows/{workflow_id}")
    async def delete_workflow(workflow_id: str, hard: bool = Query(True)):
        """Delete a workflow.

        By default (hard=True): permanently removes from the database.
        Set hard=False for soft-delete (renames and marks status='deleted').
        """
        flowcraft: FlowCraftApp = app.state.flowcraft
        row = flowcraft.db.fetch_one(
            "SELECT id, name FROM workflow_templates WHERE id = ?", (workflow_id,))
        if not row:
            raise HTTPException(404, "Workflow not found")

        if hard:
            # Hard delete: permanently remove from database
            flowcraft.db.execute(
                "DELETE FROM workflow_templates WHERE id = ?", (workflow_id,))
            return {"workflow_id": workflow_id, "status": "hard_deleted"}
        else:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            old_name = dict(row).get("name", "")
            deleted_name = f"{old_name} (已删除 {datetime.now(timezone.utc).strftime('%m-%d %H:%M')})"

            flowcraft.db.update("workflow_templates", "id", workflow_id, {
                "status": "deleted",
                "name": deleted_name,
                "updated_at": now,
            })
            return {"workflow_id": workflow_id, "status": "soft_deleted"}

    # ── Workflow Import/Export (Phase 2) ──────────────────────

    @app.get("/api/workflows/{workflow_id}/export")
    async def export_workflow(workflow_id: str):
        """Export a workflow as a downloadable JSON file."""
        flowcraft: FlowCraftApp = app.state.flowcraft
        row = flowcraft.db.fetch_one(
            "SELECT * FROM workflow_templates WHERE id = ?", (workflow_id,))
        if not row:
            raise HTTPException(status_code=404, detail="Workflow not found")
        wf = dict(row)
        export_data = {
            "flowcraft_workflow_version": "1.0",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "workflow": {
                "name": wf.get("name", ""),
                "description": wf.get("description", ""),
                "risk_summary": wf.get("risk_summary", "LOW"),
                "required_tools": json.loads(wf.get("required_tools_json", "[]")),
                "required_permissions": json.loads(wf.get("required_permissions_json", "[]")),
                "input_schema": json.loads(wf.get("input_schema_json", "{}")),
                "output_schema": json.loads(wf.get("output_schema_json", "{}")),
                "steps": json.loads(wf.get("steps_json", "[]")),
            },
        }
        return export_data

    @app.post("/api/workflows/import")
    async def import_workflow(payload: dict[str, Any]):
        """Import a workflow from JSON data."""
        flowcraft: FlowCraftApp = app.state.flowcraft
        wf_data = payload.get("workflow", {})
        if not wf_data or not wf_data.get("name"):
            raise HTTPException(status_code=400, detail="Invalid workflow data: 'name' required")

        import uuid
        wf_id = f"wf_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()

        flowcraft.db.execute(
            """INSERT INTO workflow_templates (id, name, description, risk_summary,
            required_tools_json, required_permissions_json, input_schema_json,
            output_schema_json, steps_json, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
            (
                wf_id,
                wf_data.get("name", "Imported Workflow"),
                wf_data.get("description", ""),
                wf_data.get("risk_summary", "LOW"),
                json.dumps(wf_data.get("required_tools", []), ensure_ascii=False),
                json.dumps(wf_data.get("required_permissions", []), ensure_ascii=False),
                json.dumps(wf_data.get("input_schema", {}), ensure_ascii=False),
                json.dumps(wf_data.get("output_schema", {}), ensure_ascii=False),
                json.dumps(wf_data.get("steps", []), ensure_ascii=False),
                now, now,
            ),
        )
        logger.info("Workflow imported: %s (id=%s)", wf_data.get("name"), wf_id)
        return {"workflow_id": wf_id, "name": wf_data.get("name"), "status": "imported"}

    # ── Workflow Builder ──────────────────────────────────────

    @app.post("/api/workflows/build/start")
    async def workflow_build_start(payload: dict[str, Any]):
        """Begin a new workflow building session.

        Architecture Doc §3.5:
          POST /api/workflows/build/start
          Request: { "session_id": "...", "input": "帮我创建..." }
          Response: { "session_id": "...", "stage": "...", "agent_message": "...", ... }
        """
        flowcraft: FlowCraftApp = app.state.flowcraft
        result = await flowcraft.workflow_builder.start(
            user_input=payload.get("input", ""),
            session_id=payload.get("session_id"),
        )
        return result

    @app.post("/api/workflows/build/continue")
    async def workflow_build_continue(payload: dict[str, Any]):
        """Process user reply and advance the conversation.

        Architecture Doc §3.5:
          POST /api/workflows/build/continue
          Request: { "session_id": "...", "input": "文件在 D:\\sales\\..." }
          Response: { "session_id": "...", "stage": "...", "agent_message": "...", ... }
        """
        flowcraft: FlowCraftApp = app.state.flowcraft
        import asyncio
        try:
            result = await asyncio.wait_for(
                flowcraft.workflow_builder.continue_dialog(
                    payload.get("session_id", ""),
                    payload.get("input", ""),
                ),
                timeout=60.0,
            )
            return result
        except asyncio.TimeoutError:
            return {
                "session_id": payload.get("session_id", ""),
                "stage": "await_confirm",
                "agent_message": "生成超时，请重试或简化工作流描述。",
                "error": "timeout",
            }

    @app.post("/api/workflows/build/confirm")
    async def workflow_build_confirm(payload: dict[str, Any]):
        """Confirm and save the generated workflow.

        Architecture Doc §3.5:
          POST /api/workflows/build/confirm
          Request: { "session_id": "...", "confirmed": true }
          Response: { "workflow_id": "...", "name": "...", "steps": [...] }
        """
        from datetime import datetime, timezone
        from uuid import uuid4
        import json as _json

        flowcraft: FlowCraftApp = app.state.flowcraft
        session_id = payload.get("session_id", "")
        session = flowcraft.workflow_builder.complete_session(session_id)
        if not session or not session.workflow_preview:
            raise HTTPException(400, "No workflow to confirm")

        wf = session.workflow_preview
        wf_id = f"wf_{uuid4().hex}"
        now = datetime.now(timezone.utc).isoformat()
        data_blob = {
            "steps": wf.get("steps", []),
            "required_tools": wf.get("required_tools", []),
            "required_permissions": wf.get("required_permissions", []),
            "risk_summary": wf.get("risk_summary", "LOW"),
            "environment_setup": wf.get("environment_setup", {}),
            "input_schema": wf.get("input_schema", {}),
            "output_schema": wf.get("output_schema", {}),
            "tags": wf.get("tags", []),
        }
        flowcraft.db.insert_json("workflow_templates", {
            "id": wf_id,
            "name": wf.get("name", "Untitled"),
            "description": wf.get("description", ""),
            "data_json": _json.dumps(data_blob, ensure_ascii=False),
            "created_at": now,
            "updated_at": now,
        })
        flowcraft.workflow_builder.delete_session(session_id)
        return {
            "status": "created",
            "workflow_id": wf_id,
            "name": wf.get("name"),
            "steps_count": len(wf.get("steps", [])),
        }

    @app.post("/api/workflows/build/modify")
    async def workflow_build_modify(payload: dict[str, Any]):
        """Modify the workflow preview based on user feedback.

        Architecture Doc §3.5:
          POST /api/workflows/build/modify
          Request: { "session_id": "...", "feedback": "去掉 Step 3" }
          Response: { "workflow_preview": { ... } }
        """
        flowcraft: FlowCraftApp = app.state.flowcraft
        import asyncio
        session_id = payload.get("session_id", "")
        feedback = payload.get("feedback", "")
        if not session_id or not feedback:
            raise HTTPException(400, "需要 session_id 和 feedback")
        try:
            result = await asyncio.wait_for(
                flowcraft.workflow_builder.modify_workflow(session_id, feedback),
                timeout=60.0,
            )
            return result
        except asyncio.TimeoutError:
            return {
                "session_id": session_id,
                "stage": "await_confirm",
                "agent_message": "修改超时，请重试。",
                "error": "timeout",
            }

    @app.get("/api/workflows/build/{session_id}")
    async def workflow_build_state(session_id: str):
        """Get the current state of a workflow building session.

        Architecture Doc §3.5:
          GET /api/workflows/build/{session_id}
          Response: { "stage": "...", "agent_message": "...", "collected_info": {...} }
        """
        flowcraft: FlowCraftApp = app.state.flowcraft
        session = flowcraft.workflow_builder.get_session(session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        return session.to_dict()

    # ── Marketplace ──────────────────────────────────────────

    @app.get("/api/marketplace")
    async def browse_marketplace(q: str = Query("")):
        flowcraft: FlowCraftApp = app.state.flowcraft
        from flowcraft_core.config.sync import WorkflowMarketplace
        mp = WorkflowMarketplace(flowcraft.db, flowcraft.settings.data_dir)
        return {"workflows": mp.browse(q)}

    @app.get("/api/marketplace/{workflow_id}")
    async def download_workflow(workflow_id: str):
        flowcraft: FlowCraftApp = app.state.flowcraft
        from flowcraft_core.config.sync import WorkflowMarketplace
        mp = WorkflowMarketplace(flowcraft.db, flowcraft.settings.data_dir)
        wf = mp.download(workflow_id)
        if not wf:
            raise HTTPException(404, "Workflow not found")
        return {"workflow": wf}

    @app.post("/api/workflows/{workflow_id}/publish")
    async def publish_workflow(workflow_id: str):
        flowcraft: FlowCraftApp = app.state.flowcraft
        from flowcraft_core.config.sync import WorkflowMarketplace
        mp = WorkflowMarketplace(flowcraft.db, flowcraft.settings.data_dir)
        return mp.publish(workflow_id)

    @app.post("/api/workflows/{workflow_id}/run")
    async def run_workflow(workflow_id: str, payload: dict[str, Any]):
        """Execute a saved workflow by creating a task.

        Frontend calls: POST /api/workflows/{id}/run {"session_id": "..."}
        Response: { "task_id": "...", "status": "CREATED", "title": "..." }
        """
        flowcraft: FlowCraftApp = app.state.flowcraft

        row = flowcraft.db.fetch_one(
            "SELECT * FROM workflow_templates WHERE id = ?",
            (workflow_id,),
        )
        if not row:
            raise HTTPException(404, "Workflow not found")

        wf = dict(row)
        name = wf.get("name", "Untitled") or "Untitled"
        description = wf.get("description", "") or ""

        # Parse data_json to get steps; schema: {"steps": [...], "required_tools": [...], ...}
        steps: list[dict] = []
        try:
            data_blob = json.loads(wf.get("data_json", "{}") or "{}")
            steps = data_blob.get("steps", [])
        except (json.JSONDecodeError, TypeError):
            pass

        # Build a clear instruction for the Agent
        step_lines = []
        for s in sorted(steps, key=lambda x: x.get("index", 0)):
            title = s.get("title", "Step")
            objective = s.get("objective", "")
            action = s.get("action_type", "")
            tool = s.get("tool_name", "")
            step_lines.append(
                f"- 步骤{s.get('index', '?')}. {title}"
                f" [类型: {action}]" + (f" [工具: {tool}]" if tool else "") +
                f": {objective}"
            )

        if step_lines:
            input_text = (
                f"执行工作流「{name}」\n\n"
                f"描述: {description}\n\n"
                f"执行以下步骤:\n" + "\n".join(step_lines) +
                "\n\n请按顺序执行以上步骤，每步完成后汇报结果。"
            )
        else:
            # Fallback: use name/description as the task
            input_text = f"执行工作流「{name}」\n\n{description or '执行该工作流的所有步骤。'}"

        from uuid import uuid4
        session_id = payload.get("session_id", f"session_{uuid4().hex[:12]}")
        request = AgentRequest(
            session_id=session_id,
            raw_input=input_text,
        )
        task = await flowcraft.runtime.start_task(request)

        # Update workflow usage count
        try:
            current_count = wf.get("use_count", 0)
            flowcraft.db.update("workflow_templates", "id", workflow_id, {
                "use_count": current_count + 1,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            pass

        return CreateTaskResponse(
            task_id=task.task_id,
            status=task.status.value,
            title=f"运行: {name}",
        )

    @app.post("/api/workflows/{workflow_id}/unpublish")
    async def unpublish_workflow(workflow_id: str):
        flowcraft: FlowCraftApp = app.state.flowcraft
        from flowcraft_core.config.sync import WorkflowMarketplace
        mp = WorkflowMarketplace(flowcraft.db, flowcraft.settings.data_dir)
        return mp.unpublish(workflow_id)

    # ── Workspaces ───────────────────────────────────────────

    @app.get("/api/workspaces")
    async def list_workspaces():
        flowcraft: FlowCraftApp = app.state.flowcraft
        from flowcraft_core.policy.enterprise import TeamWorkspace
        tw = TeamWorkspace(flowcraft.db, flowcraft.settings.data_dir)
        return {"workspaces": tw.list_workspaces()}

    @app.post("/api/workspaces")
    async def create_workspace(payload: dict[str, Any]):
        flowcraft: FlowCraftApp = app.state.flowcraft
        from flowcraft_core.policy.enterprise import TeamWorkspace
        tw = TeamWorkspace(flowcraft.db, flowcraft.settings.data_dir)
        return tw.create_workspace(payload.get("name", "Default"))

    @app.post("/api/workspaces/{workspace_id}/members")
    async def add_workspace_member(workspace_id: str, payload: dict[str, Any]):
        flowcraft: FlowCraftApp = app.state.flowcraft
        from flowcraft_core.policy.enterprise import TeamWorkspace
        tw = TeamWorkspace(flowcraft.db, flowcraft.settings.data_dir)
        ok = tw.add_member(workspace_id, payload.get("user_id", ""))
        return {"status": "ok" if ok else "error"}

    # ── Enterprise Policies ──────────────────────────────────

    @app.get("/api/policies")
    async def list_policies():
        flowcraft: FlowCraftApp = app.state.flowcraft
        from flowcraft_core.policy.enterprise import EnterprisePolicyEngine
        epe = EnterprisePolicyEngine(flowcraft.db)
        return {"policies": epe.list_rules()}

    @app.post("/api/policies")
    async def manage_policy(payload: dict[str, Any]):
        flowcraft: FlowCraftApp = app.state.flowcraft
        from flowcraft_core.policy.enterprise import EnterprisePolicyEngine
        epe = EnterprisePolicyEngine(flowcraft.db)
        if payload.get("action") == "delete":
            epe.remove_rule(payload.get("rule_id", ""))
            return {"status": "deleted"}
        rule = epe.add_rule(
            name=payload.get("name", "New Rule"),
            description=payload.get("description", ""),
            target=payload.get("target", "*"),
            action=payload.get("action", "ALLOW"),
            scope=payload.get("scope", "global"),
            priority=payload.get("priority", 0),
        )
        return {"status": "created", "rule_id": rule.rule_id}

    # ── Config Sync ──────────────────────────────────────────

    @app.get("/api/sync/export")
    async def export_config():
        flowcraft: FlowCraftApp = app.state.flowcraft
        from flowcraft_core.config.sync import ConfigExporter
        from fastapi.responses import JSONResponse
        ce = ConfigExporter(flowcraft.db, flowcraft.settings.data_dir)
        return JSONResponse(ce.export_all())

    @app.post("/api/sync/import")
    async def import_config(payload: dict[str, Any]):
        flowcraft: FlowCraftApp = app.state.flowcraft
        import json as _json
        tmp = flowcraft.settings.temp_dir / f"import_{payload.get('id','cfg')}.json"
        tmp.write_text(_json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        from flowcraft_core.config.sync import ConfigExporter
        ce = ConfigExporter(flowcraft.db, flowcraft.settings.data_dir)
        stats = ce.import_from_file(tmp)
        tmp.unlink(missing_ok=True)
        return {"status": "imported", "stats": stats}

    # ── DAG Plan ─────────────────────────────────────────────

    @app.post("/api/tools/dag-plan")
    async def generate_dag_plan(payload: dict[str, Any]):
        flowcraft: FlowCraftApp = app.state.flowcraft
        from flowcraft_core.planning.dag_planner import DagPlanner, MultiAgentOrchestrator
        from flowcraft_core.domain.schemas import TaskBrief
        brief = TaskBrief(
            task_id=payload.get("task_id", "dag_test"),
            objective=payload.get("input", ""),
            task_type=payload.get("task_type", "QA"),
            risk_level=payload.get("risk_level", "LOW"),
        )
        planner = DagPlanner(flowcraft.model_gateway)
        plan = await planner.create_dag_plan(brief)
        layers = planner.topological_sort(plan.steps)
        orch = MultiAgentOrchestrator(flowcraft.model_gateway, flowcraft.events)
        agents = orch.select_agents(brief)
        return {
            "plan": plan.model_dump(mode="json"),
            "parallel_layers": [[s.index for s in layer] for layer in layers],
            "recommended_agents": agents,
        }

    # ── Knowledge Base ───────────────────────────────────────

    @app.get("/api/knowledge/sources")
    async def list_kb_sources():
        flowcraft: FlowCraftApp = app.state.flowcraft
        return {"sources": flowcraft.knowledge_base.list_sources()}

    @app.get("/api/knowledge/search")
    async def search_knowledge(q: str = Query("")):
        flowcraft: FlowCraftApp = app.state.flowcraft
        return {"results": flowcraft.knowledge_base.search(q)}

    @app.post("/api/knowledge/ingest")
    async def ingest_knowledge(payload: dict[str, Any]):
        flowcraft: FlowCraftApp = app.state.flowcraft
        from pathlib import Path
        return flowcraft.knowledge_base.ingest_file(Path(payload["path"]), payload.get("name"))

    # ── i18n ─────────────────────────────────────────────────

    @app.get("/api/i18n/locales")
    async def list_locales():
        flowcraft: FlowCraftApp = app.state.flowcraft
        return {"locales": flowcraft.i18n.available_locales(), "current": flowcraft.i18n.locale}

    @app.post("/api/i18n/locale")
    async def set_locale(payload: dict[str, Any]):
        flowcraft: FlowCraftApp = app.state.flowcraft
        flowcraft.i18n.set_locale(payload.get("locale", "zh-CN"))
        return {"locale": flowcraft.i18n.locale}

    # ── Sessions ─────────────────────────────────────────────

    @app.get("/api/sessions/{session_id}/events")
    async def get_session_events(session_id: str):
        flowcraft: FlowCraftApp = app.state.flowcraft
        task_rows = flowcraft.db.fetch_all(
            "SELECT id FROM tasks WHERE session_id = ? AND status != 'CANCELLED' ORDER BY created_at ASC",
            (session_id,))
        all_events = []
        for row in task_rows:
            all_events.extend(flowcraft.events.list_for_task(dict(row)["id"]))
        all_events.sort(key=lambda e: e.get("created_at", ""))
        return {"events": all_events, "session_id": session_id}

    # ── Vent Mode (Agent Vent / Feedback) ────────────────────

    class VentDetectRequest(BaseModel):
        input: str
        session_id: str = "default"
        task_id: str = ""

    class VentStartRequest(BaseModel):
        session_id: str = "default"
        task_id: str = ""
        severity: int = 1
        pain_points: list[str] = Field(default_factory=list)

    class VentSubmitRequest(BaseModel):
        user_pain_point: str = ""
        user_suggestion: str = ""
        selected_phrase_id: str = ""

    class VentPhraseVoteRequest(BaseModel):
        phrase_id: str

    class VentCustomPhraseRequest(BaseModel):
        text: str = Field(min_length=1, max_length=200)
        lang: str = "zh"
        pain_direction: str = "general"

    @app.post("/api/vent/detect")
    async def vent_detect(payload: VentDetectRequest):
        """Detect user frustration in a message.

        Returns FrustrationAssessment with severity, target, and pain_points.
        Frontend calls this on every user message to decide whether to show Vent Panel.
        """
        flowcraft: FlowCraftApp = app.state.flowcraft
        if not flowcraft.frustration_detector:
            raise HTTPException(501, "Vent module not initialized")
        assessment = flowcraft.frustration_detector.detect(
            payload.input, session_id=payload.session_id, task_id=payload.task_id,
        )
        return {
            "is_frustrated": assessment.is_frustrated,
            "severity": assessment.severity,
            "target": assessment.target,
            "pain_points": assessment.pain_points,
            "confidence": assessment.confidence,
            "detection_method": assessment.detection_method,
            "should_trigger_vent": assessment.should_trigger_vent(),
            "severity_level": assessment.vent_severity_level(),
        }

    class VentRefineRequest(BaseModel):
        input: str
        session_id: str = "default"
        severity: int = 1
        pain_points: list[str] = Field(default_factory=list)
        target: str = "agent"

    @app.post("/api/vent/detect/refine")
    async def vent_detect_refine(payload: VentRefineRequest):
        """Deferred LLM refinement of frustration detection (Filter 3).

        Called only when user interacts with Vent Panel (selects phrase or
        fills template). Uses ~200 tokens — much cheaper than calling on
        every message.
        """
        flowcraft: FlowCraftApp = app.state.flowcraft
        if not flowcraft.frustration_detector:
            raise HTTPException(501, "Vent module not initialized")

        # Reconstruct the keyword assessment
        from flowcraft_core.feedback.sentiment import FrustrationAssessment
        assessment = FrustrationAssessment(
            is_frustrated=True,
            severity=payload.severity,
            target=payload.target,
            pain_points=payload.pain_points,
            original_input=payload.input,
            confidence=0.75,
            detection_method="keyword",
            filter_level=2,
        )

        refined = await flowcraft.frustration_detector.refine_with_llm(assessment)
        return {
            "is_frustrated": refined.is_frustrated,
            "severity": refined.severity,
            "target": refined.target,
            "pain_points": refined.pain_points,
            "confidence": refined.confidence,
            "detection_method": refined.detection_method,
            "filter_level": refined.filter_level,
            "was_refined": refined.detection_method == "llm",
        }

    @app.post("/api/vent/session/start")
    async def vent_session_start(payload: VentStartRequest):
        """Start a new vent session.

        Generates a context-aware template (with auto-filled fields from task trace
        when available) and returns top phrases for the user to choose from.
        """
        flowcraft: FlowCraftApp = app.state.flowcraft
        if not flowcraft.vent_session_manager or not flowcraft.phrase_library:
            raise HTTPException(501, "Vent module not initialized")

        # Create vent session
        vent = flowcraft.vent_session_manager.start_session(
            session_id=payload.session_id,
            severity=payload.severity,
            task_id=payload.task_id,
            pain_points=payload.pain_points,
        )

        # Try to pre-fill template from task trace (Phase 2: full trace extraction)
        task_objective = ""
        actual_action = ""
        consequence = ""
        if payload.task_id:
            try:
                task_row = flowcraft.db.fetch_one(
                    "SELECT * FROM tasks WHERE id = ?", (payload.task_id,))
                task_row_dict = dict(task_row) if task_row else None
                events = flowcraft.events.list_for_task(payload.task_id)
                task_objective, actual_action, consequence = (
                    flowcraft.vent_session_manager.extract_context_from_traces(
                        events, task_row_dict,
                    )
                )
            except Exception:
                pass

        flowcraft.vent_session_manager.build_template(
            vent.id,
            task_objective=task_objective,
            actual_action=actual_action,
            consequence=consequence,
        )

        # Get phrases grouped by pain_direction
        phrases_grouped = flowcraft.phrase_library.get_phrases_grouped(lang="zh")
        top_phrases = flowcraft.phrase_library.get_top_phrases(lang="zh", limit=10)

        # Format phrases for API response
        phrases_dict = {pd: [p.to_dict() for p in phrases]
                        for pd, phrases in phrases_grouped.items()}
        top_dict = [p.to_dict() for p in top_phrases]

        return vent.to_api_response(
            top_phrases=top_dict,
            phrases_grouped=phrases_dict,
        )

    @app.post("/api/vent/session/{vent_id}/submit")
    async def vent_session_submit(vent_id: str, payload: VentSubmitRequest):
        """Submit user's vent feedback.

        Processes feedback through InsightMapper, generates correction hints,
        integrates into memory system, and closes the vent session.
        """
        flowcraft: FlowCraftApp = app.state.flowcraft
        if not flowcraft.vent_session_manager:
            raise HTTPException(501, "Vent module not initialized")

        session = flowcraft.vent_session_manager.submit_feedback(
            vent_id,
            user_pain_point=payload.user_pain_point,
            user_suggestion=payload.user_suggestion,
            selected_phrase_id=payload.selected_phrase_id,
        )
        if not session:
            raise HTTPException(404, "Vent session not found")

        # Vote for selected phrase
        if payload.selected_phrase_id and flowcraft.phrase_library:
            flowcraft.phrase_library.vote(payload.selected_phrase_id)

        # Map to insight (Phase 2: LLM-enhanced)
        if flowcraft.insight_mapper:
            # Determine pain directions: from session pain_points + phrase's pain_direction
            pain_dirs = list(session.pain_points) if session.pain_points else []
            if payload.selected_phrase_id and flowcraft.phrase_library:
                phrase = flowcraft.phrase_library.get_phrase(payload.selected_phrase_id)
                if phrase and phrase.pain_direction not in pain_dirs:
                    pain_dirs.append(phrase.pain_direction)

            # Phase 2: LLM-based insight analysis when user provides detailed complaint
            if payload.user_pain_point and len(payload.user_pain_point) > 10:
                insight = await flowcraft.insight_mapper.map_with_llm(
                    user_complaint=payload.user_pain_point,
                    pain_points=pain_dirs,
                    task_objective=session.template.task_objective if session.template else "",
                    severity=session.severity,
                )
            else:
                insight = flowcraft.insight_mapper.map_from_pain_points(
                    pain_dirs, severity=session.severity,
                )

            # Integrate into memory (Phase 2: LLM condensation)
            if flowcraft.feedback_memory_integrator:
                tools_involved = []
                task_type = ""
                try:
                    task_row = flowcraft.db.fetch_one(
                        "SELECT task_type FROM tasks WHERE id = ?", (session.task_id,))
                    task_type = task_row["task_type"] if task_row else ""
                except Exception:
                    pass

                # Use async condensation when complaint is detailed
                if payload.user_pain_point and len(payload.user_pain_point) > 10:
                    await flowcraft.feedback_memory_integrator.integrate_with_condensation(
                        vent_session_id=vent_id,
                        failure_type=insight.failure_type.value,
                        pain_direction=pain_dirs[0] if pain_dirs else "general",
                        task_type=task_type,
                        severity=session.severity,
                        tools_involved=tools_involved,
                        pain_point_text=payload.user_pain_point,
                        correction_hint=insight.correction_hint,
                        task_objective=session.template.task_objective if session.template else "",
                    )
                else:
                    flowcraft.feedback_memory_integrator.integrate(
                        vent_session_id=vent_id,
                        failure_type=insight.failure_type.value,
                        pain_direction=pain_dirs[0] if pain_dirs else "general",
                        task_type=task_type,
                        severity=session.severity,
                        tools_involved=tools_involved,
                        pain_point_text=payload.user_pain_point,
                        correction_hint=insight.correction_hint,
                    )

            # Close session with insight
            flowcraft.vent_session_manager.close_session(
                vent_id,
                insight_generated=insight.correction_hint,
                mapped_failure_type=insight.failure_type.value,
            )

        # Record cooldown
        if flowcraft.frustration_detector:
            flowcraft.frustration_detector.record_vent_occurred(session.session_id)

        # Record event
        flowcraft.events.record(
            TraceEvent(
                task_id=session.task_id or "",
                session_id=session.session_id,
                event_type="VENT_COMPLETED",
                title="用户提交了Vent反馈",
                message=f"用户反馈: {payload.user_pain_point or '(话术选择)'}",
                payload={
                    "vent_session_id": vent_id,
                    "severity": session.severity,
                    "selected_phrase_id": payload.selected_phrase_id,
                    "user_suggestion": payload.user_suggestion,
                },
                severity="INFO",
            )
        )

        # Build closing message
        closing_msg = ""
        if flowcraft.agent_response_sanitizer:
            closing_msg = flowcraft.agent_response_sanitizer.build_closing(
                f"问题类型: {insight.failure_type.value if flowcraft.insight_mapper else '已记录'}"
            )

        return {
            "status": "submitted",
            "vent_id": vent_id,
            "closing_message": closing_msg,
            "insight": insight.to_dict() if (flowcraft.insight_mapper and 'insight' in dir()) else None,
        }

    @app.get("/api/vent/phrases")
    async def vent_list_phrases(lang: str = Query("zh")):
        """List all active vent phrases, grouped by pain_direction."""
        flowcraft: FlowCraftApp = app.state.flowcraft
        if not flowcraft.phrase_library:
            raise HTTPException(501, "Vent module not initialized")
        phrases = flowcraft.phrase_library.list_all_phrases(lang=lang)
        phrases_grouped = flowcraft.phrase_library.get_phrases_grouped(lang=lang)
        return {
            "phrases": [p.to_dict() for p in phrases],
            "groups": {pd: [p.to_dict() for p in phrases]
                       for pd, phrases in phrases_grouped.items()},
        }

    @app.post("/api/vent/phrase/vote")
    async def vent_phrase_vote(payload: VentPhraseVoteRequest):
        """Vote for a vent phrase (increments local vote count)."""
        flowcraft: FlowCraftApp = app.state.flowcraft
        if not flowcraft.phrase_library:
            raise HTTPException(501, "Vent module not initialized")
        phrase = flowcraft.phrase_library.vote(payload.phrase_id)
        if not phrase:
            raise HTTPException(404, "Phrase not found")
        return {"status": "voted", "phrase": phrase.to_dict()}

    @app.post("/api/vent/phrase/custom")
    async def vent_phrase_custom(payload: VentCustomPhraseRequest):
        """Add a custom vent phrase."""
        flowcraft: FlowCraftApp = app.state.flowcraft
        if not flowcraft.phrase_library:
            raise HTTPException(501, "Vent module not initialized")
        phrase = flowcraft.phrase_library.add_custom_phrase(
            text=payload.text,
            lang=payload.lang,
            pain_direction=payload.pain_direction,
        )
        return {"status": "created", "phrase": phrase.to_dict()}

    @app.get("/api/vent/insights")
    async def vent_insights(limit: int = Query(20, ge=1, le=100)):
        """Get accumulated pain point analytics."""
        flowcraft: FlowCraftApp = app.state.flowcraft
        rows = flowcraft.db.fetch_all(
            "SELECT * FROM pain_point_analytics ORDER BY occurrence_count DESC LIMIT ?",
            (limit,))
        return {"insights": [dict(r) for r in rows]}

    @app.get("/api/vent/sessions/{vent_id}")
    async def vent_get_session(vent_id: str):
        """Get a vent session by ID."""
        flowcraft: FlowCraftApp = app.state.flowcraft
        if not flowcraft.vent_session_manager:
            raise HTTPException(501, "Vent module not initialized")
        session = flowcraft.vent_session_manager.get_session(vent_id)
        if not session:
            raise HTTPException(404, "Vent session not found")
        return session.to_dict()

    @app.get("/api/vent/lessons")
    async def vent_list_lessons(
        task_type: str = Query(""),
        failure_type: str = Query(""),
        pain_direction: str = Query(""),
        limit: int = Query(5, ge=1, le=20),
    ):
        """Retrieve learned lessons from feedback memory."""
        flowcraft: FlowCraftApp = app.state.flowcraft
        if not flowcraft.feedback_memory_integrator:
            raise HTTPException(501, "Vent module not initialized")
        lessons = flowcraft.feedback_memory_integrator.retrieve_lessons(
            task_type=task_type,
            failure_type=failure_type,
            pain_direction=pain_direction,
            limit=limit,
        )
        return {"lessons": [l.to_dict() for l in lessons]}

    return app


app = create_app()

