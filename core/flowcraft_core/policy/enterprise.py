"""Enterprise Policy Center + Team Workspace.

Enterprise policies: Configurable security rules with scopes.
Team workspace: Multi-user sessions, shared tasks, role-based access.
"""

from __future__ import annotations
import json, logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from flowcraft_core.storage.database import Database

logger = logging.getLogger(__name__)

@dataclass
class PolicyRule:
    rule_id: str = field(default_factory=lambda: f"rule_{uuid4().hex[:12]}")
    name: str = ""
    description: str = ""
    target: str = "*"       # tool name pattern or "*" for all
    action: str = "ALLOW"   # ALLOW, DENY, REQUIRE_APPROVAL, REQUIRE_SANDBOX
    scope: str = "global"   # global, workspace, team, user
    conditions: dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    enabled: bool = True
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

class EnterprisePolicyEngine:
    def __init__(self, db: Database):
        self.db = db
        self._rules: dict[str, PolicyRule] = {}
        self._load_rules()

    def _load_rules(self):
        rows = self.db.fetch_all("SELECT value_json FROM settings WHERE key='enterprise_policies'")
        if rows:
            try:
                data = json.loads(dict(rows[0])["value_json"])
                for item in data.get("rules", []):
                    rule = PolicyRule(**item)
                    self._rules[rule.rule_id] = rule
            except Exception:
                pass

    def _save_rules(self):
        data = {"rules": [r.__dict__ for r in self._rules.values()]}
        payload = json.dumps(data, ensure_ascii=False)
        now = datetime.now(timezone.utc).isoformat()
        # Use INSERT OR REPLACE (upsert) to avoid UNIQUE constraint on re-save
        existing = self.db.fetch_one(
            "SELECT key FROM settings WHERE key = ?", ("enterprise_policies",))
        if existing:
            self.db.update("settings", "key", "enterprise_policies",
                          {"value_json": payload, "updated_at": now})
        else:
            self.db.insert_json("settings", {
                "key": "enterprise_policies",
                "value_json": payload,
                "updated_at": now,
            })

    def add_rule(self, **kwargs) -> PolicyRule:
        rule = PolicyRule(**kwargs)
        self._rules[rule.rule_id] = rule
        self._save_rules()
        return rule

    def remove_rule(self, rule_id: str) -> bool:
        if rule_id in self._rules:
            del self._rules[rule_id]
            self._save_rules()
            return True
        return False

    def list_rules(self) -> list[dict]:
        return sorted(
            [r.__dict__ for r in self._rules.values()],
            key=lambda x: (-x["priority"], x["name"]),
        )

    def evaluate(self, tool_name: str, context: dict | None = None) -> dict:
        ctx = context or {}
        for rule in sorted(self._rules.values(), key=lambda r: -r.priority):
            if not rule.enabled:
                continue
            target_match = rule.target == "*" or tool_name.startswith(rule.target.replace("*", ""))
            if not target_match:
                continue
            scope_match = rule.scope == "global" or ctx.get("scope") == rule.scope
            if not scope_match:
                continue
            return {"decision": rule.action, "matched_rule": rule.name, "rule_id": rule.rule_id}
        return {"decision": "ALLOW", "matched_rule": "default"}


class TeamWorkspace:
    def __init__(self, db: Database, data_dir: Path):
        self.db = db
        self.workspaces_dir = Path(data_dir) / "workspaces"
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)

    def create_workspace(self, name: str, owner: str = "local-user") -> dict:
        ws_id = f"ws_{uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        self.db.insert_json("settings", {
            "key": f"workspace:{ws_id}",
            "value_json": json.dumps({
                "id": ws_id, "name": name, "owner": owner,
                "members": [owner], "created_at": now, "updated_at": now,
            }, ensure_ascii=False),
            "updated_at": now,
        })
        return {"workspace_id": ws_id, "name": name, "owner": owner}

    def list_workspaces(self, user: str = "local-user") -> list[dict]:
        rows = self.db.fetch_all("SELECT value_json FROM settings WHERE key LIKE 'workspace:%'")
        results = []
        for row in rows:
            data = json.loads(dict(row)["value_json"])
            if user in data.get("members", []):
                results.append(data)
        return results

    def add_member(self, workspace_id: str, user_id: str) -> bool:
        row = self.db.fetch_one(
            "SELECT value_json FROM settings WHERE key = ?", (f"workspace:{workspace_id}",))
        if not row:
            return False
        data = json.loads(dict(row)["value_json"])
        if user_id not in data.get("members", []):
            data["members"].append(user_id)
            data["updated_at"] = datetime.now(timezone.utc).isoformat()
            self.db.update("settings", "key", f"workspace:{workspace_id}",
                          {"value_json": json.dumps(data, ensure_ascii=False)})
        return True

    def get_workspace_sessions(self, workspace_id: str) -> list[dict]:
        return [dict(r) for r in self.db.fetch_all(
            "SELECT * FROM tasks WHERE session_id LIKE ? ORDER BY created_at DESC LIMIT 50",
            (f"{workspace_id}%",),
        )]
