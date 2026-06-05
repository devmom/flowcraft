"""Shared test fixtures for FlowCraft functional testing."""
from __future__ import annotations

import os
import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the core package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flowcraft_core.config.settings import Settings
from flowcraft_core.storage.database import Database
from flowcraft_core.domain.enums import RiskLevel


# ── Path fixtures ────────────────────────────────────────────

@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    """A clean temporary workspace directory shared across tests."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_workspace: Path) -> None:
    """Automatically isolate each test from real env / real data dir."""
    monkeypatch.setenv("FLOWCRAFT_DATA_DIR", str(tmp_workspace / "flowcraft_data"))
    monkeypatch.setenv("FLOWCRAFT_WORKSPACE", str(tmp_workspace))
    # Remove any real API keys so we don't accidentally hit real models
    for key in ("FLOWCRAFT_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY", "AGNES_API_KEY"):
        monkeypatch.delenv(key, raising=False)


# ── Database fixtures ─────────────────────────────────────────

@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def tmp_database(tmp_db_path: Path) -> Database:
    """An initialized, empty SQLite database in a temp directory."""
    db = Database(tmp_db_path)
    db.initialize()
    return db


@pytest.fixture
def populated_db(tmp_database: Database) -> Database:
    """Database with a session and task pre-inserted."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    tmp_database.insert_json("sessions", {
        "id": "sess_1", "title": "Test Session",
        "created_at": now, "updated_at": now,
    })
    tmp_database.insert_json("tasks", {
        "id": "task_test1", "session_id": "sess_1",
        "title": "Test Task", "objective": "Run tests",
        "task_type": "QA", "status": "CREATED",
        "risk_level": "LOW",
        "constraints_json": "[]", "success_criteria_json": "[]",
        "created_at": now, "updated_at": now,
    })
    return tmp_database


# ── Settings fixtures ─────────────────────────────────────────

@pytest.fixture
def test_settings(tmp_workspace: Path) -> Settings:
    """Settings pointing to a temp workspace with default values."""
    data_dir = tmp_workspace / "flowcraft_data"
    db_path = data_dir / "data" / "flowcraft.db"
    s = Settings(
        data_dir=data_dir,
        database_path=db_path,
        allowed_paths=[tmp_workspace],
    )
    return s


# ── Model gateway mock ────────────────────────────────────────

@pytest.fixture
def mock_model_gateway() -> MagicMock:
    """Mock ModelGateway that returns valid structured responses."""
    gateway = MagicMock()
    gateway.generate_structured.return_value = {
        "task_type": "QA",
        "objective": "Answer the question",
        "risk_level": "LOW",
        "success_criteria": ["Answer provided"],
        "constraints": ["Be accurate"],
        "target_objects": [],
        "required_capabilities": ["text_generation"],
        "requires_local_files": False,
        "requires_network": False,
        "requires_tools": False,
        "clarification_required": False,
        "clarification_questions": [],
        "expected_output_format": "text",
    }
    gateway.generate.return_value = "Mock model response."
    gateway.is_live.return_value = True
    return gateway


# ── Application fixture (lightweight, mock model) ─────────────

@pytest.fixture
def test_app(tmp_workspace: Path, mock_model_gateway: MagicMock):
    """Lightweight FlowCraftApp with mock model gateway."""
    from flowcraft_core.app import FlowCraftApp

    data_dir = tmp_workspace / "flowcraft_data"
    db_path = data_dir / "data" / "flowcraft.db"
    settings = Settings(
        data_dir=data_dir,
        database_path=db_path,
        allowed_paths=[tmp_workspace],
    )
    settings.ensure_directories()

    # Patch ModelGateway before import
    with patch('flowcraft_core.app.ModelGateway', return_value=mock_model_gateway):
        app = FlowCraftApp(settings)
        yield app
        # Cleanup: close database
        try:
            app.db.connect().close()
        except Exception:
            pass


# ── Concurrent test helper ────────────────────────────────────

def run_concurrent(func, args_list: list[tuple], worker_count: int = 10) -> list:
    """Run `func` concurrently from multiple threads, return collected results."""
    results = []
    errors = []
    lock = threading.Lock()

    def worker(*a: object) -> None:
        try:
            r = func(*a)
            with lock:
                results.append(r)
        except Exception as e:
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker, args=args) for args in args_list]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    if errors:
        raise AssertionError(f"{len(errors)} workers failed: {errors[0]}")
    return results


# ── Time utilities ────────────────────────────────────────────

@pytest.fixture
def frozen_time(monkeypatch: pytest.MonkeyPatch):
    """Freeze time at a known point."""
    from datetime import datetime, timezone
    frozen = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr("flowcraft_core.domain.schemas.datetime", FrozenDatetime)
    return frozen
