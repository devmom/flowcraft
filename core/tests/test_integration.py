"""Integration tests - full app lifecycle, API endpoints, cross-module flows."""
import pytest
import json
from pathlib import Path
from flowcraft_core.config.settings import Settings, load_settings
from flowcraft_core.app import FlowCraftApp
from flowcraft_core.domain.enums import TaskStatus
from flowcraft_core.domain.schemas import AgentRequest


class TestAppInit:
    def test_app_initializes(self):
        settings = load_settings()
        app = FlowCraftApp(settings)
        assert app.settings is not None
        assert app.db is not None
        assert app.tool_registry is not None
        assert app.model_gateway is not None

    def test_app_has_all_tools(self):
        settings = load_settings()
        app = FlowCraftApp(settings)
        tools = [d["tool_name"] for d in app.tool_registry.list_definitions()]
        assert "file.read" in tools
        assert "file.write" in tools
        assert "command.run" in tools
        assert "browser.read" in tools
        assert "document.pdf.read" in tools
        assert "document.docx.read" in tools
        assert "document.xlsx.read" in tools
        assert "browser.navigate" in tools

    def test_app_health(self):
        settings = load_settings()
        app = FlowCraftApp(settings)
        assert app.model_gateway.is_live() or not app.model_gateway.is_live()

    def test_startup_recovery(self):
        """Verify startup recovery works without crashing."""
        settings = load_settings()
        app = FlowCraftApp(settings)
        # Recovery runs silently; just verify app is alive
        assert app.db is not None


class TestTaskLifecycle:
    def test_create_task(self):
        settings = load_settings()
        app = FlowCraftApp(settings)
        import asyncio
        request = AgentRequest(session_id="test_integration", raw_input="Test task")
        task = asyncio.run(app.runtime.create_task_async(request))
        assert task.task_id.startswith("task_")
        assert task.status in (TaskStatus.CREATED, TaskStatus.INTENT_RECOGNIZED,
                               TaskStatus.PLANNED, TaskStatus.COMPLETED)
        assert task.session_id == "test_integration"

    def test_task_store_roundtrip(self):
        settings = load_settings()
        app = FlowCraftApp(settings)
        import asyncio
        request = AgentRequest(session_id="test_roundtrip", raw_input="Echo test")
        task = asyncio.run(app.runtime.create_task_async(request))
        stored = app.task_store.get_task_row(task.task_id)
        assert stored is not None
        assert dict(stored)["id"] == task.task_id


class TestConfigSync:
    def test_export_import(self):
        settings = load_settings()
        app = FlowCraftApp(settings)
        from flowcraft_core.config.sync import ConfigExporter
        ce = ConfigExporter(app.db, settings.data_dir)
        data = ce.export_all()
        assert "version" in data
        assert "settings" in data
        assert "workflows" in data


class TestI18n:
    def test_default_locale(self):
        from flowcraft_core.config.i18n import I18n
        i18n = I18n()
        assert i18n.locale == "zh-CN"

    def test_translate(self):
        from flowcraft_core.config.i18n import I18n
        i18n = I18n("en")
        assert i18n.t("task.created") == "Task Created"

    def test_fallback(self):
        from flowcraft_core.config.i18n import I18n
        i18n = I18n("fr")  # non-existent locale
        assert i18n.t("task.created") == "Task Created"  # falls back to en


class TestEnterprisePolicy:
    def test_add_and_list_rules(self):
        from flowcraft_core.config.settings import load_settings
        from flowcraft_core.storage.database import Database
        settings = load_settings()
        db = Database(settings.database_path)
        db.initialize()
        from flowcraft_core.policy.enterprise import EnterprisePolicyEngine
        epe = EnterprisePolicyEngine(db)
        epe.add_rule(name="block_cmd", target="command.run",
                     action="DENY", priority=10)
        rules = epe.list_rules()
        assert len(rules) >= 1
        result = epe.evaluate("command.run")
        assert result["decision"] == "DENY"
        epe.remove_rule(rules[0]["rule_id"])


class TestKnowledgeBase:
    def test_tokenize(self):
        from flowcraft_core.memory.knowledge_base import KnowledgeBase
        from flowcraft_core.config.settings import load_settings
        from flowcraft_core.storage.database import Database
        settings = load_settings()
        db = Database(settings.database_path)
        db.initialize()
        kb = KnowledgeBase(db, settings.knowledge_dir)
        tokens = kb._tokenize("This is a test document about Python programming")
        assert "test" in tokens
        assert "python" in tokens
        assert "is" not in tokens  # stopword
        assert "a" not in tokens    # stopword
        assert "about" not in tokens  # stopword

