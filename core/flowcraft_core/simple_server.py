from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from uuid import uuid4

from flowcraft_core.app import FlowCraftApp
from flowcraft_core.config.settings import load_settings
from flowcraft_core.domain.schemas import AgentRequest, TraceEvent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


_json = json  # alias for use in inner scopes


_app: FlowCraftApp | None = None


def _load_dotenv() -> None:
    """Load .env.local from core directory if present (simple dotenv without dependency)."""
    import os as _os
    env_file = Path(__file__).resolve().parent.parent / ".env.local"
    if env_file.exists():
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip()
                if val.startswith('"') and val.endswith('"'):
                    val = val[1:-1]
                if val.startswith("'") and val.endswith("'"):
                    val = val[1:-1]
                if key and val and key not in _os.environ:
                    _os.environ[key] = val


def get_app() -> FlowCraftApp:
    global _app
    if _app is None:
        _load_dotenv()
        settings = load_settings()
        _app = FlowCraftApp(settings)
    return _app


WEB_DIR = Path(__file__).parent / "web"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        app = get_app()
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self._file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            return

        # ── Static file serving for uploaded files ──
        if parsed.path.startswith("/api/files/"):
            from urllib.parse import unquote as _uq
            rel = parsed.path[len("/api/files/"):]
            # Security: only serve from temp/uploads
            upload_dir = app.settings.temp_dir / "uploads"
            file_path = (upload_dir / _uq(rel)).resolve()
            try:
                file_path.relative_to(upload_dir.resolve())
            except ValueError:
                self._json({"detail": "Forbidden"}, status=403)
                return
            if not file_path.is_file():
                self._json({"detail": "Not found"}, status=404)
                return
            ct = {
                ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
                ".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown",
                ".json": "application/json", ".csv": "text/csv",
                ".html": "text/html", ".css": "text/css", ".js": "application/javascript",
            }.get(file_path.suffix.lower(), "application/octet-stream")
            self._file(file_path, ct)
            return

        if parsed.path == "/health":
            model_configured = app.model_gateway.is_live()
            self._json(
                {
                    "status": "ok",
                    "version": app.settings.version,
                    "db_status": "ok",
                    "model_configured": model_configured,
                    "provider": app.model_gateway.provider_name,
                    "model": app.model_gateway.current_model_id if model_configured else "none",
                    "server": "stdlib",
                    "data_dir": str(app.settings.data_dir),
                }
            )
            return
        if parsed.path.startswith("/api/sessions/") and parsed.path.endswith("/events"):
            # 获取某会话下所有任务的聚合事件
            from urllib.parse import unquote
            session_id = unquote(parsed.path.split("/")[3])
            task_rows = app.db.fetch_all(
                "SELECT id FROM tasks WHERE session_id = ? AND status != 'CANCELLED' ORDER BY created_at ASC",
                (session_id,)
            )
            all_events: list[dict] = []
            for row in task_rows:
                events = app.events.list_for_task(dict(row)["id"])
                all_events.extend(events)
            all_events.sort(key=lambda e: e.get("created_at", ""))
            self._json({"events": all_events, "session_id": session_id})
            return
        if parsed.path in {"/api/tasks", "/api/tasks/"}:
            rows = app.db.fetch_all(
                "SELECT id as task_id, session_id, title, status, objective, risk_level, created_at, updated_at "
                "FROM tasks ORDER BY created_at DESC LIMIT 50", ()
            )
            self._json({"tasks": [dict(row) for row in rows]})
            return
        if parsed.path == "/api/tools":
            self._json({"tools": app.tool_registry.list_definitions()})
            return
        # ── Workflows ──────────────────────────────
        if parsed.path == "/api/workflows":
            rows = app.db.fetch_all(
                "SELECT * FROM workflow_templates WHERE status != 'deleted' "
                "ORDER BY created_at DESC", ())
            self._json({"workflows": [dict(r) for r in rows]})
            return
        if parsed.path.startswith("/api/settings") and parsed.path != "/api/settings/models" and parsed.path != "/api/settings/tools":
            cur = app.db.fetch_one("SELECT value_json FROM settings WHERE key = 'app_settings'")
            settings_data = json.loads(dict(cur)["value_json"]) if cur else {}
            self._json({"settings": settings_data})
            return
        if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/events"):
            task_id = parsed.path.split("/")[3]
            events = app.events.list_for_task(task_id)
            self._json({"events": events})
            return
        if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/report"):
            task_id = parsed.path.split("/")[3]
            task_row = app.task_store.get_task_row(task_id)
            if not task_row:
                self._json({"detail": "Task not found"}, status=404)
                return
            task_dict = dict(task_row)
            events = app.events.list_for_task(task_id)
            if "html" in (parsed.query or "").lower():
                report = _build_html_report(task_dict, events)
                data = report.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="task_{task_id}_report.html"')
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
                return
            else:
                report = _build_markdown_report(task_dict, events)
                self._json({"report": report, "format": "markdown", "task_id": task_id})
                return
        if parsed.path.startswith("/api/tasks/"):
            task_id = parsed.path.split("/")[3]
            task = app.task_store.get_task_row(task_id)
            if not task:
                self._json({"detail": "Task not found"}, status=404)
                return
            task_dict = dict(task)
            if "id" in task_dict and "task_id" not in task_dict:
                task_dict["task_id"] = task_dict["id"]
            self._json({"task": task_dict})
            return
        # ── P2: Marketplace ─────────────────────────
        if parsed.path == "/api/marketplace" or parsed.path == "/api/marketplace/":
            from flowcraft_core.config.sync import WorkflowMarketplace
            mp = WorkflowMarketplace(app.db, app.settings.data_dir)
            search = ""
            if parsed.query and "q=" in parsed.query:
                search = parsed.query.split("q=")[1].split("&")[0]
            self._json({"workflows": mp.browse(search)})
            return
        if parsed.path.startswith("/api/marketplace/") and not parsed.path.endswith("/publish") and not parsed.path.endswith("/unpublish"):
            wf_id = parsed.path.split("/")[3]
            from flowcraft_core.config.sync import WorkflowMarketplace
            mp = WorkflowMarketplace(app.db, app.settings.data_dir)
            wf = mp.download(wf_id)
            if wf:
                self._json({"workflow": wf})
            else:
                self._json({"detail": "Not found"}, status=404)
            return
        # ── P2: Plugins ─────────────────────────────
        if parsed.path == "/api/plugins" or parsed.path == "/api/plugins/":
            plugins = app.tool_registry.list_definitions()
            self._json({"plugins": plugins})
            return
        # ── P2: Workspaces ──────────────────────────
        if parsed.path == "/api/workspaces" or parsed.path == "/api/workspaces/":
            from flowcraft_core.policy.enterprise import TeamWorkspace
            tw = TeamWorkspace(app.db, app.settings.data_dir)
            self._json({"workspaces": tw.list_workspaces()})
            return
        # ── P2: Policies ────────────────────────────
        if parsed.path == "/api/policies" or parsed.path == "/api/policies/":
            from flowcraft_core.policy.enterprise import EnterprisePolicyEngine
            epe = EnterprisePolicyEngine(app.db)
            self._json({"policies": epe.list_rules()})
            return
        # ── P2: Config Sync Export ──────────────────
        if parsed.path == "/api/sync/export":
            from flowcraft_core.config.sync import ConfigExporter
            ce = ConfigExporter(app.db, app.settings.data_dir)
            export_data = ce.export_all()
            data = json.dumps(export_data, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=flowcraft_config.json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
            return
        # ── P4: Knowledge Base ──────────────────────
        if parsed.path == "/api/knowledge/sources":
            self._json({"sources": app.knowledge_base.list_sources()})
            return
        if parsed.path == "/api/knowledge/search":
            q = ""
            if parsed.query and "q=" in parsed.query:
                q = parsed.query.split("q=")[1].split("&")[0]
            self._json({"results": app.knowledge_base.search(q)})
            return
        if parsed.path == "/api/knowledge/stats":
            self._json(app.long_term_memory.memory_stats())
            return
        # ── P4: Task Replay ─────────────────────────
        if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/replay"):
            task_id = parsed.path.split("/")[3]
            self._json(app.task_replay.get_timeline(task_id))
            return
        # ── P4: i18n ────────────────────────────────
        if parsed.path == "/api/i18n/locales":
            self._json({"locales": app.i18n.available_locales(), "current": app.i18n.locale})
            return
        # ── SSE Stream ──────────────────────────────
        if parsed.path.startswith("/api/stream/") and parsed.path.endswith("/events"):
            task_id = parsed.path.split("/")[3]
            self._stream_events(task_id)
            return
        # ── Workflow Builder Session State ──────────
        # GET /api/workflows/build/{session_id} (Architecture Doc §3.5)
        # Must check BEFORE /api/status since that's more specific
        if (parsed.path.startswith("/api/workflows/build/")
                and len(parsed.path.split("/")) == 5):
            session_id = parsed.path.split("/")[4]
            # Skip "state" and "start" etc — those are POST-only anyway
            if session_id not in ("state", "start", "continue", "confirm", "modify"):
                session = app.workflow_builder.get_session(session_id)
                if session:
                    self._json(session.to_dict())
                else:
                    self._json({"error": "Session not found"}, status=404)
                return

        # ── Active/Running Tasks Status ──────────────
        if parsed.path == "/api/status":
            from flowcraft_core.runtime.engine import get_active_tasks, TASK_TIMEOUT
            active = get_active_tasks()
            self._json({
                "active_tasks": len(active),
                "tasks": [
                    {"task_id": t["task_id"], "title": t["title"],
                     "started_at": t.get("started_at", "")}
                    for t in active.values()
                ],
                "task_timeout_seconds": TASK_TIMEOUT,
            })
            return
        self._json({"detail": "Not found"}, status=404)

    def do_DELETE(self) -> None:
        app = get_app()
        parsed = urlparse(self.path)

        # ── Upload cleanup ─────────────────────────
        if parsed.path == "/api/upload":
            from urllib.parse import parse_qs, unquote as _uq
            qs = parse_qs(parsed.query)
            file_path_str = qs.get("path", [""])[0]
            if file_path_str:
                fp = Path(_uq(file_path_str))
                upload_dir = app.settings.temp_dir / "uploads"
                try:
                    fp.resolve().relative_to(upload_dir.resolve())
                    if fp.is_file():
                        fp.unlink()
                        self._json({"status": "deleted", "path": str(fp)})
                        return
                except (ValueError, OSError):
                    pass
            self._json({"status": "skipped"})
            return

        if parsed.path.startswith("/api/sessions/"):
            # 删除会话：取消该 session 下所有任务
            from urllib.parse import unquote
            session_id = unquote(parsed.path.split("/")[3])
            app.db.execute(
                "UPDATE tasks SET status = 'CANCELLED' WHERE session_id = ?",
                (session_id,)
            )
            self._json({"session_id": session_id, "status": "CANCELLED"})
            return
        if parsed.path.startswith("/api/tasks/"):
            task_id = parsed.path.split("/")[3]
            app.db.update("tasks", "id", task_id, {"status": "CANCELLED"})
            self._json({"task_id": task_id, "status": "CANCELLED"})
            return
        # ── Workflow 删除 ────────────────────────────
        if parsed.path.startswith("/api/workflows/"):
            wf_id = parsed.path.split("/")[3]
            row = app.db.fetch_one(
                "SELECT id, name FROM workflow_templates WHERE id = ?", (wf_id,))
            if not row:
                self._json({"detail": "Workflow not found"}, status=404)
                return
            # Hard delete: actually remove from database
            app.db.execute("DELETE FROM workflow_templates WHERE id = ?", (wf_id,))
            self._json({"workflow_id": wf_id, "status": "deleted"})
            return
        self._json({"detail": "Not found"}, status=404)

    def do_POST(self) -> None:
        app = get_app()
        parsed = urlparse(self.path)

        # ── File Upload ────────────────────────────
        if parsed.path == "/api/upload":
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                self._json({"detail": "Content-Type must be multipart/form-data"}, status=400)
                return

            mp = self._parse_multipart()
            files = mp.get("files", [])
            if not files:
                self._json({"detail": "No files uploaded"}, status=400)
                return

            upload_dir = app.settings.temp_dir / "uploads"
            upload_dir.mkdir(parents=True, exist_ok=True)

            uploaded = []
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            for f in files:
                filename = f["filename"]
                # Sanitize filename
                safe_name = Path(filename).name
                saved_name = f"{timestamp}_{safe_name}"
                saved_path = upload_dir / saved_name
                saved_path.write_bytes(f["data"])
                uploaded.append({
                    "filename": filename,
                    "saved_path": str(saved_path),
                    "content_type": f.get("content_type", ""),
                    "size": f.get("size", 0),
                    "size_human": self._format_bytes(f.get("size", 0)),
                })

            self._json({
                "status": "ok",
                "uploaded": uploaded,
                "count": len(uploaded),
                "upload_dir": str(upload_dir),
                "hint": "Use saved_path in task input to reference uploaded files.",
            })
            return

        # ── Workflow Builder ────────────────────────
        if parsed.path == "/api/workflows/build/start":
            body = self._body()
            result = app.workflow_builder.start(
                user_input=body.get("input", ""),
                session_id=body.get("session_id"),
            )
            self._json(result)
            return

        if parsed.path == "/api/workflows/build/continue":
            body = self._body()
            session_id = body.get("session_id", "")
            try:
                result = asyncio.run(asyncio.wait_for(
                    app.workflow_builder.continue_dialog(
                        session_id, body.get("input", "")),
                    timeout=60.0))
            except asyncio.TimeoutError:
                result = {
                    "session_id": session_id,
                    "stage": "await_confirm",
                    "agent_message": "生成超时，请重试或简化工作流描述。",
                    "error": "timeout",
                }
            except Exception as exc:
                logging.exception("Workflow continue failed")
                result = {
                    "session_id": session_id,
                    "stage": "error",
                    "agent_message": f"处理失败: {exc}。请重试。",
                    "error": str(exc),
                }
            self._json(result)
            return

        if parsed.path == "/api/workflows/build/confirm":
            try:
                body = self._body()
                session_id = body.get("session_id", "")
                session = app.workflow_builder.complete_session(session_id)
                if not session or not session.workflow_preview:
                    self._json({"error": "No workflow to confirm"}, status=400)
                    return
                # Save as WorkflowTemplate
                wf = session.workflow_preview
                wf_id = _new_id("wf")
                now = _now_utc()
                # Build data_json blob matching how reads parse it
                data_blob = {
                    "steps": wf.get("steps", []),
                    "required_tools": wf.get("required_tools", []),
                    "required_permissions": wf.get("required_permissions", []),
                    "risk_summary": wf.get("risk_summary", "LOW"),
                    "input_schema": wf.get("input_schema", {}),
                    "output_schema": wf.get("output_schema", {}),
                    "tags": wf.get("tags", []),
                }
                app.db.insert_json("workflow_templates", {
                    "id": wf_id,
                    "name": wf.get("name", "Untitled"),
                    "description": wf.get("description", ""),
                    "data_json": json.dumps(data_blob, ensure_ascii=False),
                    "created_at": now,
                    "updated_at": now,
                })
                app.workflow_builder.delete_session(session_id)
                self._json({
                    "status": "created",
                    "workflow_id": wf_id,
                    "name": wf.get("name"),
                    "steps_count": len(wf.get("steps", [])),
                })
            except Exception as exc:
                logging.exception("Workflow confirm failed: %s", exc)
                try:
                    self._json({"error": f"保存失败: {exc}"}, status=500)
                except Exception:
                    pass
            return

        if parsed.path == "/api/workflows/build/modify":
            body = self._body()
            session_id = body.get("session_id", "")
            feedback = body.get("feedback", "")
            if not session_id or not feedback:
                self._json({"error": "需要 session_id 和 feedback"}, status=400)
                return
            try:
                result = asyncio.run(asyncio.wait_for(
                    app.workflow_builder.modify_workflow(session_id, feedback),
                    timeout=60.0))
                self._json(result)
            except asyncio.TimeoutError:
                self._json({
                    "session_id": session_id,
                    "stage": "await_confirm",
                    "agent_message": "修改超时，请重试。",
                    "error": "timeout",
                }, status=500)
            except Exception as exc:
                logging.exception("Workflow modify failed: %s", exc)
                self._json({
                    "session_id": session_id,
                    "stage": "await_confirm",
                    "agent_message": f"修改失败: {exc}。请重试。",
                    "error": str(exc),
                }, status=500)
            return

        if parsed.path == "/api/workflows/build/state":
            session_id = parsed.query.split("session_id=")[1].split("&")[0] if "session_id=" in (parsed.query or "") else ""
            session = app.workflow_builder.get_session(session_id)
            if session:
                self._json(session.to_dict())
            else:
                self._json({"error": "Session not found"}, status=404)
            return

        if parsed.path == "/api/tasks":
            try:
                body = self._body()
                # ── Model switching (supports DeepSeek, Agnes, Ollama) ──
                requested_model = body.get("model", "")
                switch_error = None
                if requested_model and requested_model != app.model_gateway.current_model_id:
                    # Determine provider from model name
                    if "agnes" in requested_model:
                        provider = "agnes"
                        api_key = (app.secrets.get(f"model:agnes:{requested_model}:api_key")
                                   or app.secrets.get("model:agnes:__default__:api_key")
                                   or os.environ.get("AGNES_API_KEY"))
                    else:
                        provider = "deepseek"
                        api_key = (app.secrets.get(f"model:deepseek:{requested_model}:api_key")
                                   or app.secrets.get("model:deepseek:__default__:api_key")
                                   or os.environ.get("FLOWCRAFT_DEEPSEEK_API_KEY")
                                   or os.environ.get("DEEPSEEK_API_KEY")
                                   # Within same provider: reuse current model's stored key
                                   or app.secrets.get(f"model:deepseek:{app.model_gateway.current_model_id}:api_key"))
                    if not api_key:
                        switch_error = f"No API key for {requested_model}. Configure in Settings."
                        logging.warning(switch_error)
                    else:
                        switched = app.model_gateway.switch_model(requested_model, api_key)
                        if not switched:
                            switch_error = f"Model switch to {requested_model} failed."
                            logging.warning(switch_error)
                        else:
                            logging.info("Model switched to %s (provider=%s)", requested_model, provider)

                request = AgentRequest(
                    session_id=body.get("session_id", "default"),
                    raw_input=body.get("input", ""),
                    attachments=body.get("attachments", []),
                )
                # 先创建任务，立即返回；执行在后台线程进行（流式体验）
                task = asyncio.run(app.runtime.create_task_async(request))
                resp = {"task_id": task.task_id, "status": task.status.value, "title": task.title}
                if switch_error:
                    resp["warning"] = switch_error
                self._json(resp)
            except Exception as exc:
                logging.exception("Task creation failed: %s", exc)
                try:
                    self._json({"error": f"创建任务失败: {exc}", "task_id": None, "status": "FAILED"}, status=500)
                except Exception:
                    pass
            return
        # ── Workflow CRUD ────────────────────────
        if parsed.path == "/api/workflows":
            body = self._body()
            wf_id = _new_id("wf")
            now = _now_utc()
            data_blob = {
                "steps": body.get("steps", []),
                "required_tools": body.get("required_tools", []),
                "required_permissions": body.get("required_permissions", []),
                "risk_summary": body.get("risk_summary", "LOW"),
            }
            app.db.insert_json("workflow_templates", {
                "id": wf_id,
                "name": body.get("name", "Untitled"),
                "description": body.get("description", ""),
                "data_json": _json.dumps(data_blob, ensure_ascii=False),
                "created_at": now,
                "updated_at": now,
            })
            self._json({"workflow_id": wf_id, "status": "created"})
            return
        if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/save-as-workflow"):
            task_id = parsed.path.split("/")[3]
            body = self._body()
            task = app.task_store.get_task_row(task_id)
            plan_row = app.db.fetch_one("SELECT * FROM plans WHERE task_id = ? ORDER BY created_at DESC LIMIT 1", (task_id,))
            steps = []
            if plan_row:
                plan_data = _json.loads(dict(plan_row).get("data_json", "{}"))
                steps = plan_data.get("steps", [])
            wf_id = _new_id("wf")
            now = _now_utc()
            data_blob = {
                "steps": steps,
                "required_tools": [s.get("action_type", "") for s in steps],
                "required_permissions": [],
                "risk_summary": task.get("risk_level", "LOW") if task else "LOW",
            }
            app.db.insert_json("workflow_templates", {
                "id": wf_id,
                "name": body.get("name", task.get("title", "Untitled") if task else "Untitled"),
                "description": task.get("objective", "") if task else "",
                "data_json": _json.dumps(data_blob, ensure_ascii=False),
                "created_at": now,
                "updated_at": now,
            })
            self._json({"workflow_id": wf_id, "status": "saved"})
            return
        # ── Workflow Run ─────────────────────────
        if parsed.path.startswith("/api/workflows/") and parsed.path.endswith("/run"):
            wf_id = parsed.path.split("/")[3]
            wf_row = app.db.fetch_one("SELECT * FROM workflow_templates WHERE id = ?", (wf_id,))
            if not wf_row:
                self._json({"detail": "Workflow not found"}, status=404)
                return
            wf = dict(wf_row)
            # Parse steps from data_json (single JSON blob column)
            data_blob = _json.loads(wf.get("data_json", "{}"))
            steps = data_blob.get("steps", [])
            if not steps:
                self._json({"detail": "Workflow has no steps"}, status=400)
                return
            # Increment use_count
            app.db.execute(
                "UPDATE workflow_templates SET updated_at = ? WHERE id = ?",
                (_now_utc(), wf_id))
            # Run workflow as a task
            prompt = f"执行工作流: {wf['name']}\n描述: {wf.get('description','')}\n步骤: {_json.dumps(steps, ensure_ascii=False)}"
            request = AgentRequest(
                session_id=body.get("session_id", "default"),
                raw_input=prompt,
            )
            task = asyncio.run(app.runtime.create_task_async(request))
            self._json({"workflow_id": wf_id, "task_id": task.task_id, "status": "started"})
            return

        if parsed.path == "/api/settings":
            body = self._body()
            app.db.insert_json("settings", {
                "key": "app_settings",
                "value_json": json.dumps(body, ensure_ascii=False),
                "updated_at": _now_utc(),
            })
            # Apply API keys per provider
            if body.get("deepseek_key"):
                app.secrets.set("model:deepseek:__default__:api_key", body["deepseek_key"])
                app.secrets.set("model:deepseek:deepseek-v4-pro:api_key", body["deepseek_key"])
                app.secrets.set("model:deepseek:deepseek-v4-flash:api_key", body["deepseek_key"])
            if body.get("agnes_key"):
                app.secrets.set("model:agnes:__default__:api_key", body["agnes_key"])
                app.secrets.set("model:agnes:agnes-2.0-flash:api_key", body["agnes_key"])
            # Reconfigure model gateway immediately with new keys
            app._auto_configure_model()
            self._json({"status": "ok"})
            return

        if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/approve"):
            task_id = parsed.path.split("/")[3]
            try:
                task_row = app.task_store.get_task_row(task_id)
                if not task_row:
                    self._json({"detail": "Task not found"}, status=404)
                    return
                task_row_dict = dict(task_row)
                if task_row_dict.get("status") != "WAITING_APPROVAL":
                    self._json({"detail": "Task is not waiting for approval"}, status=400)
                    return

                import json as _json
                import threading
                from flowcraft_core.domain.enums import TaskStatus
                from flowcraft_core.domain.schemas import Task, TaskBrief, ExecutionPlan, PlanStep, TraceEvent as TE

                # 信任此会话
                session_id = task_row_dict.get("session_id", "")
                app.policy_engine.trust_session(session_id)

                app.events.record(TE(
                    task_id=task_id,
                    event_type="approval.resolved",
                    title="用户已批准（会话已信任）",
                    message="用户批准执行。同会话后续操作将自动批准。",
                ))

                # 加载完整任务
                row = app.db.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
                row_dict = dict(row)
                full_task = Task(
                    task_id=row_dict["id"], session_id=row_dict["session_id"],
                    title=row_dict["title"], objective=row_dict["objective"],
                    task_type=row_dict.get("task_type", "UNKNOWN"),
                    status=TaskStatus(row_dict.get("status", "CREATED")),
                )

                brief_row = app.db.fetch_one("SELECT * FROM task_briefs WHERE task_id = ?", (task_id,))
                plan_row = app.db.fetch_one("SELECT * FROM plans WHERE task_id = ? ORDER BY created_at DESC LIMIT 1", (task_id,))

                if not brief_row or not plan_row:
                    self._json({"task_id": task_id, "status": "COMPLETED", "title": full_task.title})
                    return

                brief_data = _json.loads(dict(brief_row).get("data_json", "{}"))
                brief_data.pop("task_id", None)
                brief = TaskBrief(task_id=task_id, **brief_data)
                plan_data = _json.loads(dict(plan_row).get("data_json", "{}"))
                steps = [PlanStep(**step) for step in plan_data.get("steps", [])]
                plan = ExecutionPlan(task_id=task_id, mode=plan_data["mode"], goal=plan_data["goal"], steps=steps)

                # 设置 EXECUTING 并立即返回，后台线程执行
                full_task.status = TaskStatus.EXECUTING
                app.task_store.update_task(full_task)

                # 注册 SSE 流式监听
                from flowcraft_core.observability.events import sse_listener_factory
                listener = sse_listener_factory(task_id)
                app.events.subscribe(listener)

                self._json({"task_id": task_id, "status": "EXECUTING", "title": full_task.title})

                # 后台线程执行（不阻塞 HTTP 响应）
                def _run_approved() -> None:
                    try:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        result = loop.run_until_complete(
                            asyncio.wait_for(
                                app.execution_engine.execute_plan(full_task, brief, plan),
                                timeout=90,
                            )
                        )
                        app.task_store.update_task(result)
                    except asyncio.TimeoutError:
                        full_task.status = TaskStatus.FAILED
                        full_task.failed_reason = "审批后执行超时（90秒）"
                        full_task.updated_at = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
                        app.task_store.update_task(full_task)
                        app.events.record(TE(
                            task_id=task_id, event_type="task.failed",
                            title="任务超时", message="审批后执行超时",
                            severity="ERROR",
                        ))
                    except Exception as exc:
                        full_task.status = TaskStatus.FAILED
                        full_task.failed_reason = str(exc)[:200]
                        full_task.updated_at = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
                        app.task_store.update_task(full_task)
                        app.events.record(TE(
                            task_id=task_id, event_type="task.failed",
                            title="执行失败", message=str(exc)[:200],
                            severity="ERROR",
                        ))
                    finally:
                        from flowcraft_core.observability.events import remove_sse_queue
                        remove_sse_queue(task_id)

                threading.Thread(target=_run_approved, daemon=True, name=f"approve-{task_id[:12]}").start()

            except Exception as exc:
                self._json({"detail": str(exc)}, status=400)
            return
        # ── Pause / Resume / Cancel ─────────────────
        if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/pause"):
            task_id = parsed.path.split("/")[3]
            from flowcraft_core.execution.engine import get_pause_controller
            pc = get_pause_controller(task_id)
            pc.pause()
            app.db.update("tasks", "id", task_id, {
                "status": "PAUSED",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            self._json({"task_id": task_id, "status": "PAUSED"})
            return
        if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/resume"):
            task_id = parsed.path.split("/")[3]
            from flowcraft_core.execution.engine import get_pause_controller
            pc = get_pause_controller(task_id)
            pc.resume()
            app.db.update("tasks", "id", task_id, {
                "status": "EXECUTING",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            self._json({"task_id": task_id, "status": "RESUMED"})
            return
        if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/cancel"):
            task_id = parsed.path.split("/")[3]
            from flowcraft_core.execution.engine import get_pause_controller
            pc = get_pause_controller(task_id)
            pc.cancel()
            app.db.update("tasks", "id", task_id, {
                "status": "CANCELLED",
                "failed_reason": "用户取消",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            self._json({"task_id": task_id, "status": "CANCELLED"})
            return
        # ── Force Kill ──────────────────────────────
        if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/force-kill"):
            task_id = parsed.path.split("/")[3]
            ok = app.force_kill_task(task_id)
            self._json({"task_id": task_id, "status": "CANCELLED" if ok else "error"})
            return
        # ── P2: Marketplace Publish/Unpublish ──────
        if parsed.path.startswith("/api/workflows/") and parsed.path.endswith("/publish"):
            wf_id = parsed.path.split("/")[3]
            from flowcraft_core.config.sync import WorkflowMarketplace
            mp = WorkflowMarketplace(app.db, app.settings.data_dir)
            self._json(mp.publish(wf_id))
            return
        if parsed.path.startswith("/api/workflows/") and parsed.path.endswith("/unpublish"):
            wf_id = parsed.path.split("/")[3]
            from flowcraft_core.config.sync import WorkflowMarketplace
            mp = WorkflowMarketplace(app.db, app.settings.data_dir)
            self._json(mp.unpublish(wf_id))
            return
        # ── P2: Workspaces ──────────────────────────
        if parsed.path == "/api/workspaces":
            body = self._body()
            from flowcraft_core.policy.enterprise import TeamWorkspace
            tw = TeamWorkspace(app.db, app.settings.data_dir)
            self._json(tw.create_workspace(body.get("name", "Default")))
            return
        if parsed.path.startswith("/api/workspaces/") and parsed.path.endswith("/members"):
            ws_id = parsed.path.split("/")[3]
            body = self._body()
            from flowcraft_core.policy.enterprise import TeamWorkspace
            tw = TeamWorkspace(app.db, app.settings.data_dir)
            ok = tw.add_member(ws_id, body.get("user_id", "local-user"))
            self._json({"status": "ok" if ok else "error"})
            return
        # ── P2: Enterprise Policies ─────────────────
        if parsed.path == "/api/policies":
            body = self._body()
            from flowcraft_core.policy.enterprise import EnterprisePolicyEngine
            epe = EnterprisePolicyEngine(app.db)
            if body.get("action") == "delete":
                epe.remove_rule(body.get("rule_id", ""))
                self._json({"status": "deleted"})
            else:
                rule = epe.add_rule(
                    name=body.get("name", "New Rule"),
                    description=body.get("description", ""),
                    target=body.get("target", "*"),
                    action=body.get("action", "ALLOW"),
                    scope=body.get("scope", "global"),
                    priority=body.get("priority", 0),
                )
                self._json({"status": "created", "rule_id": rule.rule_id})
            return
        # ── P2: Config Import ───────────────────────
        if parsed.path == "/api/sync/import":
            body = self._body()
            tmp_path = app.settings.temp_dir / f"import_{uuid4().hex[:8]}.json"
            tmp_path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
            from flowcraft_core.config.sync import ConfigExporter
            ce = ConfigExporter(app.db, app.settings.data_dir)
            stats = ce.import_from_file(tmp_path)
            tmp_path.unlink(missing_ok=True)
            self._json({"status": "imported", "stats": stats})
            return
        # ── P2: DAG Plan ────────────────────────────
        if parsed.path == "/api/tools/dag-plan":
            body = self._body()
            from flowcraft_core.planning.dag_planner import DagPlanner, MultiAgentOrchestrator
            from flowcraft_core.domain.schemas import TaskBrief
            brief_data = {
                "task_id": body.get("task_id", "dag_test"),
                "objective": body.get("input", ""),
                "task_type": body.get("task_type", "QA"),
                "risk_level": body.get("risk_level", "LOW"),
            }
            brief = TaskBrief(**brief_data)
            planner = DagPlanner(app.model_gateway)
            plan = asyncio.run(planner.create_dag_plan(brief))
            layers = planner.topological_sort(plan.steps)
            orch = MultiAgentOrchestrator(app.model_gateway, app.events)
            agents = orch.select_agents(brief)
            self._json({
                "plan": plan.model_dump(mode="json"),
                "parallel_layers": [[s.index for s in layer] for layer in layers],
                "recommended_agents": agents,
            })
            return
        # ── P4: Knowledge Base Ingest ───────────────
        if parsed.path == "/api/knowledge/ingest":
            body = self._body()
            file_path = body.get("path", "")
            if file_path:
                result = app.knowledge_base.ingest_file(
                    Path(file_path), body.get("name"))
                self._json(result)
            else:
                self._json({"status": "error", "message": "No path provided"})
            return
        # ── P4: Long-term Memory Extract ─────────────
        if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/extract-memory"):
            task_id = parsed.path.split("/")[3]
            task_row = app.task_store.get_task_row(task_id)
            if not task_row:
                self._json({"detail": "Task not found"}, status=404)
                return
            task_dict = dict(task_row)
            events = app.events.list_for_task(task_id)
            output = "\n".join(e.get("message", "") for e in events if e.get("event_type") in ("step.answer", "task.completed"))
            entries = app.long_term_memory.extract_from_task(
                task_id, task_dict.get("title", ""), output,
                task_dict.get("session_id", "default"))
            self._json({"extracted": len(entries), "memories": [{"title": e.title, "content": e.content[:100]} for e in entries]})
            return
        # ── P4: i18n Set Locale ──────────────────────
        if parsed.path == "/api/i18n/locale":
            body = self._body()
            locale = body.get("locale", "zh-CN")
            app.i18n.set_locale(locale)
            self._json({"locale": app.i18n.locale})
            return
        self._json({"detail": "Not found"}, status=404)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        # 尝试 utf-8，失败则用 latin-1（保留字节），外层 json.loads 会处理
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                return json.loads(raw.decode(enc))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        return json.loads(raw.decode("utf-8", errors="replace"))

    def _parse_multipart(self) -> dict:
        """Parse multipart/form-data request body.
        Returns dict with 'fields' (form fields) and 'files' (list of file dicts).
        Each file dict: {filename, content_type, data (bytes), saved_path}.
        """
        content_type = self.headers.get("Content-Type", "")
        if "boundary=" not in content_type:
            return {"fields": {}, "files": []}

        boundary = content_type.split("boundary=")[1].strip()
        if boundary.startswith('"') and boundary.endswith('"'):
            boundary = boundary[1:-1]

        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {"fields": {}, "files": []}

        raw = self.rfile.read(length)
        boundary_bytes = boundary.encode("utf-8")
        parts = raw.split(b"--" + boundary_bytes)

        result: dict = {"fields": {}, "files": []}

        for part in parts:
            if not part or part == b"--" or part == b"--\r\n":
                continue

            # Split headers and body
            if b"\r\n\r\n" in part:
                header_section, body = part.split(b"\r\n\r\n", 1)
            else:
                continue

            # Remove trailing \r\n and final boundary marker
            body = body.rstrip(b"\r\n")
            if body.endswith(b"--"):
                body = body[:-2].rstrip(b"\r\n")

            headers_text = header_section.decode("utf-8", errors="replace")
            headers = {}
            for line in headers_text.split("\r\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            disposition = headers.get("content-disposition", "")
            if "name=" in disposition:
                import re as _re
                name_match = _re.search(r'name="([^"]*)"', disposition)
                filename_match = _re.search(r'filename="([^"]*)"', disposition)
                field_name = name_match.group(1) if name_match else ""

                if filename_match:
                    filename = filename_match.group(1)
                    content_type_file = headers.get("content-type", "application/octet-stream")
                    result["files"].append({
                        "field_name": field_name,
                        "filename": filename,
                        "content_type": content_type_file,
                        "data": body,
                        "size": len(body),
                    })
                else:
                    result["fields"][field_name] = body.decode("utf-8", errors="replace")

        return result

    def _json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _stream_events(self, task_id: str) -> None:
        """SSE (Server-Sent Events) 流式推送任务事件."""
        from flowcraft_core.observability.events import get_sse_queue, remove_sse_queue
        import time as _time

        q = get_sse_queue(task_id)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        deadline = _time.time() + 300  # 5 min max
        try:
            # Send existing events first
            app = get_app()
            existing = app.events.list_for_task(task_id)
            for ev in existing:
                data = json.dumps(ev, ensure_ascii=False, default=str)
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()

            # Stream new events
            while _time.time() < deadline:
                try:
                    ev = q.get(timeout=2)
                    data = json.dumps(ev, ensure_ascii=False, default=str)
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    # Stop streaming on terminal events
                    if ev.get("event_type") in ("task.completed", "task.failed", "task.cancelled"):
                        break
                except Exception:
                    # Timeout - send heartbeat
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
        except Exception:
            pass
        finally:
            remove_sse_queue(task_id)

    def _file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self._json({"detail": "Not found"}, status=404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    @staticmethod
    def _format_bytes(size: int) -> str:
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        elif size < 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} MB"
        return f"{size / (1024 * 1024 * 1024):.1f} GB"


def _build_markdown_report(task: dict, events: list[dict]) -> str:
    """生成 Markdown 格式的任务执行报告."""
    status_map = {
        "COMPLETED": "✅ 完成", "FAILED": "❌ 失败", "CANCELLED": "🚫 已取消",
        "WAITING_APPROVAL": "⏸ 待审批", "EXECUTING": "🔄 执行中", "PAUSED": "⏯ 已暂停",
        "PLANNED": "📋 已规划", "CREATED": "📝 已创建",
    }
    status_label = status_map.get(task.get("status", ""), task.get("status", "未知"))
    lines = [
        f"# FlowCraft 任务报告",
        f"",
        f"**任务 ID**: `{task.get('id', task.get('task_id', 'N/A'))}`",
        f"**标题**: {task.get('title', 'N/A')}",
        f"**目标**: {task.get('objective', 'N/A')}",
        f"**状态**: {status_label}",
        f"**风险等级**: {task.get('risk_level', 'N/A')}",
        f"**创建时间**: {task.get('created_at', 'N/A')}",
        f"**更新时间**: {task.get('updated_at', 'N/A')}",
    ]
    if task.get("failed_reason"):
        lines.append(f"**失败原因**: {task.get('failed_reason')}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 执行时间线")
    lines.append("")

    if events:
        lines.append("| 时间 | 事件 | 详情 |")
        lines.append("|------|------|------|")
        for ev in events:
            ts = ev.get("created_at", "")[:19] if ev.get("created_at") else ""
            title = ev.get("title", "")
            msg = ev.get("message", "")[:80]
            severity = ev.get("severity", "")
            marker = "⚠️ " if severity == "WARN" else ("🚨 " if severity == "ERROR" else "")
            lines.append(f"| {ts} | {marker}{title} | {msg} |")
    else:
        lines.append("(无事件记录)")

    lines.append("")
    lines.append("---")
    lines.append(f"*由 FlowCraft 自动生成于 {datetime.now(timezone.utc).isoformat()[:19]}*")
    return "\n".join(lines)


def _build_html_report(task: dict, events: list[dict]) -> str:
    """生成 HTML 格式的任务执行报告."""
    status_map = {
        "COMPLETED": "完成", "FAILED": "失败", "CANCELLED": "已取消",
        "WAITING_APPROVAL": "待审批", "EXECUTING": "执行中", "PAUSED": "已暂停",
    }
    status = status_map.get(task.get("status", ""), task.get("status", "未知"))
    status_color = {"COMPLETED": "#3fb950", "FAILED": "#f85149", "CANCELLED": "#8b949e",
                    "EXECUTING": "#58a6ff", "WAITING_APPROVAL": "#d29922"}.get(task.get("status", ""), "#8b949e")

    events_html = ""
    for ev in events:
        ts = ev.get("created_at", "")[:19] if ev.get("created_at") else ""
        title = ev.get("title", "")
        msg = ev.get("message", "")
        sev = ev.get("severity", "INFO")
        bg = "#1a1a2e" if sev == "ERROR" else ("#2d2d1a" if sev == "WARN" else "#161b22")
        border = "#f85149" if sev == "ERROR" else ("#d29922" if sev == "WARN" else "#30363d")
        events_html += f"""
        <div style="background:{bg};border-left:3px solid {border};padding:8px 12px;margin:4px 0;border-radius:4px">
            <span style="color:#8b949e;font-size:11px">{ts}</span>
            <strong style="color:#e6edf3;margin-left:8px">{title}</strong>
            <div style="color:#8b949e;font-size:12px;margin-top:2px">{msg}</div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>FlowCraft Task Report</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background:#0d1117;color:#e6edf3;max-width:900px;margin:40px auto;padding:0 20px }}
h1 {{ border-bottom:1px solid #30363d;padding-bottom:12px }}
h1 span {{ font-size:14px;padding:2px 10px;border-radius:12px;margin-left:12px;
          background:{status_color}22;color:{status_color};border:1px solid {status_color}44 }}
table {{ width:100%;border-collapse:collapse;margin:16px 0 }}
td {{ padding:6px 12px;border:1px solid #21262d }}
td:first-child {{ color:#8b949e;width:120px }}
</style></head>
<body>
<h1>FlowCraft 任务报告 <span>{status}</span></h1>
<table>
<tr><td>任务 ID</td><td><code>{task.get('id', task.get('task_id', 'N/A'))}</code></td></tr>
<tr><td>标题</td><td>{task.get('title', 'N/A')}</td></tr>
<tr><td>目标</td><td>{task.get('objective', 'N/A')}</td></tr>
<tr><td>风险等级</td><td>{task.get('risk_level', 'N/A')}</td></tr>
<tr><td>创建时间</td><td>{task.get('created_at', 'N/A')}</td></tr>
{f"<tr><td>失败原因</td><td style='color:#f85149'>{task.get('failed_reason')}</td></tr>" if task.get('failed_reason') else ""}
</table>
<h2>执行时间线</h2>
{events_html}
<p style="color:#484f58;font-size:11px;margin-top:40px;text-align:center">
由 FlowCraft 自动生成于 {datetime.now(timezone.utc).isoformat()[:19]}</p>
</body></html>"""


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("FlowCraft API + Web UI listening on http://127.0.0.1:8765")
    print("端点: GET /health  GET/POST /api/tasks  GET /api/tasks/{id}/events")
    server.serve_forever()


if __name__ == "__main__":
    main()
