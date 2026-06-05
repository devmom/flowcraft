"""Workflow discovery and management tools for the Agent.

Bridges the gap between the Web UI workflow list (marketplace) and
the Agent's tool system. Without these tools, the Agent has no way
to discover or use workflows when asked in the dialog.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from flowcraft_core.domain.enums import RiskLevel
from flowcraft_core.domain.schemas import ToolIntent, ToolObservation, now_utc
from flowcraft_core.tools.base import Tool, ToolDefinition

logger = logging.getLogger(__name__)


class WorkflowSearchTool(Tool):
    """Search and list available workflows for the Agent.

    Searches both the database (all workflows) and the marketplace
    (published workflows), so the Agent can discover workflows that
    exist regardless of publish status.
    """

    def __init__(self, db: Any, marketplace_dir: Path):
        self._db = db
        self._mp_dir = Path(marketplace_dir)
        self.definition = ToolDefinition(
            tool_name="workflow_search",
            display_name="Workflow Search",
            description="Search available workflows by keyword. Returns workflow name, description, and ID. Use this to find workflows that match the user's request.",
            category="workflow",
            risk_level=RiskLevel.LOW,
            examples=[
                {"query": "长篇小说", "result": "Found 1 workflow: 长篇小说自动写作工作流"},
                {"query": "code review", "result": "Found 2 workflows: ..."},
            ],
        )

    async def execute(self, intent: ToolIntent) -> ToolObservation:
        query = intent.input_payload.get("query", "")
        results = self._search(query)

        if not results:
            return ToolObservation(
                tool_intent_id=intent.tool_intent_id,
                task_id=intent.task_id,
                step_id=intent.step_id,
                status="success",
                output_summary=f"No workflows found matching '{query}'",
                output_payload={"workflows": [], "total": 0},
                started_at=now_utc(),
                finished_at=now_utc(),
            )

        return ToolObservation(
            tool_intent_id=intent.tool_intent_id,
            task_id=intent.task_id,
            step_id=intent.step_id,
            status="success",
            output_summary=f"Found {len(results)} workflow(s) matching '{query}'",
            output_payload={"workflows": results, "total": len(results)},
            started_at=now_utc(),
            finished_at=now_utc(),
        )

    def _search(self, query: str) -> list[dict]:
        """Search workflows from both DB and marketplace."""
        results = []
        seen_ids = set()

        # 1. Search marketplace JSON files (published workflows)
        try:
            for f in self._mp_dir.glob("*.json"):
                try:
                    wf = json.loads(f.read_text(encoding="utf-8"))
                    wf_id = wf.get("id", "")
                    if wf_id in seen_ids:
                        continue
                    if query and query.lower() not in json.dumps(wf, ensure_ascii=False).lower():
                        continue
                    seen_ids.add(wf_id)
                    results.append(self._format_workflow(wf, source="marketplace"))
                except Exception:
                    continue
        except Exception as exc:
            logger.warning("Marketplace search error: %s", exc)

        # 2. Search database workflow_templates (all workflows, including unpublished)
        try:
            rows = self._db.fetch_all(
                "SELECT * FROM workflow_templates WHERE status != 'deleted'"
            )
            for row in rows:
                wf = dict(row)
                wf_id = wf.get("id", "")
                if wf_id in seen_ids:
                    continue
                if query and query.lower() not in json.dumps(wf, ensure_ascii=False).lower():
                    continue
                seen_ids.add(wf_id)
                results.append(self._format_workflow(wf, source="database"))
        except Exception as exc:
            logger.warning("Database workflow search error: %s", exc)

        return results

    def _format_workflow(self, wf: dict, source: str) -> dict:
        """Format a workflow for Agent consumption."""
        steps = wf.get("steps_json", "[]")
        if isinstance(steps, str):
            try:
                steps = json.loads(steps)
            except json.JSONDecodeError:
                steps = []
        return {
            "id": wf.get("id", ""),
            "name": wf.get("name", ""),
            "description": wf.get("description", ""),
            "author": wf.get("author", "local-user"),
            "version": wf.get("version", "1.0.0"),
            "step_count": len(steps) if isinstance(steps, list) else 0,
            "risk_summary": wf.get("risk_summary", "LOW"),
            "status": wf.get("status", "active"),
            "source": source,
        }


class WorkflowExecuteTool(Tool):
    """Execute a workflow by ID for the Agent.

    When a user wants to run a specific workflow, the Agent uses this tool
    to trigger execution.
    """

    def __init__(self, db: Any, runtime_engine: Any = None):
        self._db = db
        self._runtime = runtime_engine
        self.definition = ToolDefinition(
            tool_name="workflow_execute",
            display_name="Execute Workflow",
            description="Execute a saved workflow by its ID. First use workflow_search to find the workflow ID.",
            category="workflow",
            risk_level=RiskLevel.MEDIUM,
            requires_approval_by_default=True,
            examples=[
                {"workflow_id": "wf_abc123", "input": "Write a novel about AI"},
            ],
        )

    async def execute(self, intent: ToolIntent) -> ToolObservation:
        workflow_id = intent.input_payload.get("workflow_id", "")
        user_input = intent.input_payload.get("input", "")

        # Find workflow from DB or marketplace
        wf = self._find_workflow(workflow_id)
        if not wf:
            return ToolObservation(
                tool_intent_id=intent.tool_intent_id,
                task_id=intent.task_id,
                step_id=intent.step_id,
                status="error",
                output_summary=f"Workflow '{workflow_id}' not found",
                error_message=f"No workflow with ID '{workflow_id}'",
                started_at=now_utc(),
                finished_at=now_utc(),
            )

        return ToolObservation(
            tool_intent_id=intent.tool_intent_id,
            task_id=intent.task_id,
            step_id=intent.step_id,
            status="success",
            output_summary=f"Workflow '{wf.get('name', workflow_id)}' ready to execute",
            output_payload={
                "workflow_id": workflow_id,
                "workflow_name": wf.get("name", ""),
                "steps": wf.get("steps", []),
                "input": user_input,
            },
            started_at=now_utc(),
            finished_at=now_utc(),
        )

    def _find_workflow(self, workflow_id: str) -> dict | None:
        # Try DB first
        row = self._db.fetch_one(
            "SELECT * FROM workflow_templates WHERE id=? AND status!='deleted'",
            (workflow_id,),
        )
        if row:
            return dict(row)
        # Try marketplace
        mp_dir = self._db.path.parent.parent / "marketplace"
        f = mp_dir / f"{workflow_id}.json"
        if f.exists():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                pass
        return None
