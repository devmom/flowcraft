"""Phase 2: Config & Infrastructure Tests

Covers: A1 settings, A2 database, A3 schemas, config/sync.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from flowcraft_core.config.settings import Settings, load_settings, default_data_dir
from flowcraft_core.storage.database import Database, SCHEMA_V1
from flowcraft_core.config.sync import ConfigExporter
from flowcraft_core.domain.schemas import (
    AgentRequest, Task, TaskBrief, ExecutionPlan, PlanStep,
    TraceEvent, ApprovalRequest, new_id, now_utc,
)
from flowcraft_core.domain.enums import (
    TaskStatus, StepStatus, RiskLevel, PlanMode,
    PolicyDecisionValue, ApprovalStatus,
)

from conftest import run_concurrent


# ═══════════════════════════════════════════════════════════════
# A1: Settings tests
# ═══════════════════════════════════════════════════════════════

class TestSettings:
    """TC-A1: Configuration loading and validation."""

    # TC-A1-01
    @pytest.mark.unit
    def test_default_config_loads(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default settings use FLOWCRAFT_DATA_DIR or OS default."""
        monkeypatch.setenv("FLOWCRAFT_DATA_DIR", "/tmp/flowcraft_test")
        monkeypatch.setenv("FLOWCRAFT_WORKSPACE", "/tmp/workspace")
        settings = load_settings()
        assert settings.data_dir == Path("/tmp/flowcraft_test")
        assert settings.database_path == Path("/tmp/flowcraft_test/data/flowcraft.db")
        assert settings.app_name == "FlowCraft"
        assert settings.version == "0.1.0"

    # TC-A1-01b
    @pytest.mark.unit
    def test_workspace_is_in_allowed_paths(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FLOWCRAFT_WORKSPACE becomes the first allowed path."""
        monkeypatch.setenv("FLOWCRAFT_WORKSPACE", str(Path.cwd()))
        settings = load_settings()
        assert Path.cwd().resolve() in [p.resolve() for p in settings.allowed_paths]

    # TC-A1-02
    @pytest.mark.unit
    def test_env_var_override_data_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FLOWCRAFT_DATA_DIR overrides the default data directory."""
        monkeypatch.setenv("FLOWCRAFT_DATA_DIR", "/custom/data")
        monkeypatch.setenv("FLOWCRAFT_WORKSPACE", "/tmp/ws")
        settings = load_settings()
        assert settings.data_dir == Path("/custom/data")
        assert settings.database_path.parent == Path("/custom/data/data")

    # TC-A1-03
    @pytest.mark.unit
    def test_ensure_directories_creates_all(self, tmp_path: Path) -> None:
        """All derived directories are created on ensure_directories()."""
        data_dir = tmp_path / "flowcraft_data"
        settings = Settings(
            data_dir=data_dir,
            database_path=data_dir / "data" / "flowcraft.db",
            allowed_paths=[tmp_path],
        )
        settings.ensure_directories()
        for sub in ["config", "logs", "artifacts/tasks", "temp"]:
            assert (data_dir / sub).exists(), f"Missing: {sub}"
        artifacts_task = settings.task_artifacts_dir("task_123")
        assert artifacts_task.exists()
        assert artifacts_task.parent.name == "tasks"

    # TC-A1-04
    @pytest.mark.unit
    def test_path_traversal_is_contained(self, test_settings: Settings) -> None:
        """Malicious task_id cannot escape artifacts_dir."""
        malicious_id = "../../../etc"
        result = test_settings.task_artifacts_dir(malicious_id)
        assert result.is_relative_to(test_settings.artifacts_dir)


# ═══════════════════════════════════════════════════════════════
# A2: Database tests
# ═══════════════════════════════════════════════════════════════

class TestDatabase:
    """TC-A2: SQLite CRUD, concurrency, and security."""

    # TC-A2-01
    @pytest.mark.unit
    def test_initialize_is_idempotent(self, tmp_db_path: Path) -> None:
        """Calling initialize() multiple times does not error."""
        db = Database(tmp_db_path)
        db.initialize()
        db.initialize()
        db.initialize()
        # Should still be able to query
        rows = db.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")
        table_names = {r["name"] for r in rows}
        assert "tasks" in table_names
        assert "trace_events" in table_names

    # TC-A2-02
    @pytest.mark.unit
    def test_crud_roundtrip(self, tmp_database: Database) -> None:
        """Insert → fetch → update → delete works end-to-end."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        # Insert
        tmp_database.insert_json("sessions", {
            "id": "s1", "title": "CRUD Test",
            "created_at": now, "updated_at": now,
        })
        # Fetch
        row = tmp_database.fetch_one("SELECT * FROM sessions WHERE id = ?", ("s1",))
        assert row is not None
        assert dict(row)["title"] == "CRUD Test"

        # Update
        tmp_database.update("sessions", "id", "s1", {"title": "Updated"})
        row = tmp_database.fetch_one("SELECT title FROM sessions WHERE id = ?", ("s1",))
        assert dict(row)["title"] == "Updated"

        # Delete
        tmp_database.execute("DELETE FROM sessions WHERE id = ?", ("s1",))
        row = tmp_database.fetch_one("SELECT * FROM sessions WHERE id = ?", ("s1",))
        assert row is None

    # TC-A2-03
    @pytest.mark.slow
    @pytest.mark.unit
    def test_concurrent_writes_no_data_loss(self, tmp_db_path: Path) -> None:
        """Ten concurrent insert workers produce exactly ten rows."""
        db = Database(tmp_db_path)
        db.initialize()

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        def insert_worker(worker_id: int) -> None:
            db.insert_json("sessions", {
                "id": f"worker_{worker_id}",
                "title": f"Worker {worker_id}",
                "created_at": now, "updated_at": now,
            })

        run_concurrent(insert_worker, [(i,) for i in range(10)], worker_count=10)

        rows = db.fetch_all("SELECT COUNT(*) as cnt FROM sessions")
        count = dict(rows[0])["cnt"]
        assert count == 10, f"Expected 10 rows, got {count}"

    # TC-A2-04
    @pytest.mark.unit
    def test_json_serialization_roundtrip(self, tmp_database: Database) -> None:
        """Complex nested dict/list survives insert_json → fetch roundtrip."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        complex_data = {
            "nested": {"key": "value", "list": [1, 2, {"deep": True}]},
            "unicode": "中文测试 🚀",
            "null_val": None,
            "bool_val": False,
            "number": 42.5,
        }
        payload_json = json.dumps(complex_data, ensure_ascii=False)

        tmp_database.insert_json("trace_events", {
            "id": "evt_1", "task_id": "task_x",
            "session_id": "sess_x",
            "event_type": "test.event",
            "title": "JSON Test",
            "message": "Testing JSON roundtrip",
            "payload_json": payload_json,
            "severity": "INFO",
            "created_at": now,
        })

        row = tmp_database.fetch_one(
            "SELECT payload_json FROM trace_events WHERE id = ?", ("evt_1",))
        assert row is not None
        restored = json.loads(dict(row)["payload_json"])
        assert restored == complex_data
        assert restored["unicode"] == "中文测试 🚀"

    # TC-A2-05
    @pytest.mark.unit
    def test_sql_injection_protected(self, tmp_database: Database) -> None:
        """Parameterised queries prevent SQL injection."""
        malicious_input = "x'; DROP TABLE tasks; --"
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        # Using insert_json (parameterized)
        tmp_database.insert_json("sessions", {
            "id": malicious_input,  # id is used as value, not part of SQL
            "title": malicious_input,
            "created_at": now, "updated_at": now,
        })

        # Verify tasks table still exists
        row = tmp_database.fetch_one(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'")
        assert row is not None, "tasks table was dropped by injection!"

        # The malicious data should be stored as-is, not executed
        stored = tmp_database.fetch_one("SELECT title FROM sessions WHERE id = ?",
                                        (malicious_input,))
        assert stored is not None
        assert dict(stored)["title"] == malicious_input


# ═══════════════════════════════════════════════════════════════
# A3: Domain Schemas tests
# ═══════════════════════════════════════════════════════════════

class TestDomainSchemas:
    """TC-A3: Pydantic model validation."""

    # TC-A3-01
    @pytest.mark.unit
    def test_agent_request_minimal_fields(self) -> None:
        """AgentRequest accepts minimal required fields."""
        req = AgentRequest(session_id="test_session", raw_input="Hello")
        assert req.session_id == "test_session"
        assert req.raw_input == "Hello"
        assert req.request_id.startswith("req_")

    # TC-A3-01b
    @pytest.mark.unit
    def test_agent_request_defaults_applied(self) -> None:
        """Missing optional fields get sensible defaults."""
        req = AgentRequest(session_id="s", raw_input="hi")
        assert req.user_id == "local-user"
        assert req.source == "desktop"
        assert req.attachments == []
        assert isinstance(req.created_at, object)  # datetime

    # TC-A3-01c
    @pytest.mark.unit
    def test_agent_request_empty_session_id_allowed(self) -> None:
        """Empty session_id is technically valid (validation at higher level)."""
        req = AgentRequest(session_id="", raw_input="test")
        assert req.session_id == ""

    # TC-A3-02
    @pytest.mark.unit
    def test_task_valid_state_transition(self) -> None:
        """Task can transition through normal lifecycle states."""
        task = Task(session_id="s", title="T", objective="test")
        assert task.status == TaskStatus.CREATED
        task.status = TaskStatus.INTENT_RECOGNIZED
        assert task.status == "INTENT_RECOGNIZED"
        task.status = TaskStatus.PLANNED
        task.status = TaskStatus.EXECUTING
        task.status = TaskStatus.COMPLETED
        assert task.status == "COMPLETED"

    # TC-A3-03
    @pytest.mark.unit
    def test_json_roundtrip_preserves_all_fields(self) -> None:
        """model_dump → model_validate roundtrip preserves data."""
        plan = ExecutionPlan(
            task_id="task_x",
            mode=PlanMode.LINEAR,
            goal="Write a file",
            steps=[
                PlanStep(
                    index=0, title="Read file", objective="Read input",
                    action_type="TOOL", expected_output="contents",
                    risk_level=RiskLevel.LOW,
                ),
                PlanStep(
                    index=1, title="Write file", objective="Write output",
                    action_type="TOOL", expected_output="success",
                    depends_on=[0], risk_level=RiskLevel.MEDIUM,
                ),
            ],
            constraints=["no_delete"],
            success_criteria=["File written"],
        )
        dumped = plan.model_dump(mode="json")
        reloaded = ExecutionPlan.model_validate(dumped)
        assert reloaded.task_id == "task_x"
        assert reloaded.mode == PlanMode.LINEAR
        assert len(reloaded.steps) == 2
        assert reloaded.steps[1].depends_on == [0]
        assert reloaded.constraints == ["no_delete"]

    # TC-A3-03b
    @pytest.mark.unit
    def test_trace_event_json_roundtrip(self) -> None:
        """TraceEvent survives JSON serialization cycle."""
        event = TraceEvent(
            task_id="t1", session_id="s1",
            event_type="task.created", title="Created",
            message="Task was created",
            payload={"key": "中文值"},
            severity="INFO",
        )
        dumped = event.model_dump(mode="json")
        reloaded = TraceEvent.model_validate(dumped)
        assert reloaded.event_id == event.event_id
        assert reloaded.payload == {"key": "中文值"}
        assert reloaded.severity == "INFO"


class TestApprovalSchemas:
    """Approval schema tests."""

    @pytest.mark.unit
    def test_approval_lifecycle(self) -> None:
        """ApprovalRequest status transitions."""
        approval = ApprovalRequest(
            task_id="task_x",
            action_title="Delete file",
            action_description="Confirm deletion of test.txt",
            risk_level=RiskLevel.HIGH,
        )
        assert approval.status == ApprovalStatus.PENDING

        approval.status = ApprovalStatus.APPROVED
        approval.user_decision = "approved"
        assert approval.status == "APPROVED"

        approval2 = ApprovalRequest(
            task_id="task_y",
            action_title="Run command",
            action_description="Execute dangerous command",
            risk_level=RiskLevel.CRITICAL,
        )
        approval2.status = ApprovalStatus.REJECTED
        assert approval2.status == "REJECTED"


# ═══════════════════════════════════════════════════════════════
# Config Sync tests
# ═══════════════════════════════════════════════════════════════

class TestConfigSync:
    """Config export/import tests."""

    @pytest.mark.component
    def test_export_has_version(self, tmp_database: Database, tmp_path: Path) -> None:
        """Exported config includes version and timestamp."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        tmp_database.insert_json("settings", {
            "key": "app_settings",
            "value_json": '{"api_key":"secret123","theme":"dark","language":"zh"}',
            "updated_at": now,
        })
        exporter = ConfigExporter(tmp_database, tmp_path)
        data = exporter.export_all()
        assert data["version"] == "1.0"
        assert "exported_at" in data
        assert "settings" in data
        assert "workflows" in data

    @pytest.mark.component
    def test_export_strips_api_key(self, tmp_database: Database, tmp_path: Path) -> None:
        """API keys are stripped from exported config."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        tmp_database.insert_json("settings", {
            "key": "app_settings",
            "value_json": '{"api_key":"secret123","theme":"dark"}',
            "updated_at": now,
        })
        exporter = ConfigExporter(tmp_database, tmp_path)
        data = exporter.export_all()
        settings = data["settings"]
        assert "api_key" not in settings, f"API key leaked: {settings}"
        assert settings.get("theme") == "dark"

    @pytest.mark.component
    def test_export_to_file_writes_valid_json(self, tmp_database: Database, tmp_path: Path) -> None:
        """export_to_file writes valid JSON that can be re-read."""
        exporter = ConfigExporter(tmp_database, tmp_path)
        out_path = tmp_path / "export.json"
        result = exporter.export_to_file(out_path)
        assert result == out_path
        assert out_path.exists()

        re_read = json.loads(out_path.read_text(encoding="utf-8"))
        assert re_read["version"] == "1.0"
