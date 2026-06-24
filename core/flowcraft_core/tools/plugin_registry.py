"""Plugin System — Third-party tool plugin manifest, discovery, installation.

Phase 2 additions:
- Plugin signature verification (SHA-256 manifest hash)
- Plugin isolation: tool execution wrapped in try/except to prevent crashes
"""
from __future__ import annotations

import hashlib
import importlib
import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from flowcraft_core.domain.enums import RiskLevel
from flowcraft_core.tools.base import Tool, ToolDefinition

logger = logging.getLogger(__name__)


@dataclass
class PluginManifest:
    """Plugin manifest definition."""
    name: str
    version: str
    author: str = "unknown"
    description: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    min_flowcraft_version: str = "0.1.0"
    signature: str = ""  # SHA-256 hash of manifest content (Phase 2)
    homepage: str = ""
    license: str = "MIT"

    @classmethod
    def from_json(cls, path: Path) -> "PluginManifest":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**data)

    @classmethod
    def from_dict(cls, data: dict) -> "PluginManifest":
        fields = set(cls.__dataclass_fields__.keys())
        return cls(**{k: v for k, v in data.items() if k in fields})

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "version": self.version,
            "author": self.author, "description": self.description,
            "tools": self.tools, "dependencies": self.dependencies,
            "permissions": self.permissions,
            "min_flowcraft_version": self.min_flowcraft_version,
        }


@dataclass
class InstalledPlugin:
    plugin_id: str
    manifest: PluginManifest
    install_path: Path
    installed_at: str
    enabled: bool = True
    tools_registered: list[str] = field(default_factory=list)


class PluginRegistry:
    """Plugin registry - discover, install, manage third-party tool plugins."""

    def __init__(self, plugins_dir: Path) -> None:
        self.plugins_dir = Path(plugins_dir)
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self._installed: dict[str, InstalledPlugin] = {}
        self._tool_sources: dict[str, str] = {}

    def discover_available(self, sources_dir: Path | None = None) -> list[PluginManifest]:
        if sources_dir is None:
            sources_dir = self.plugins_dir / "sources"
        sources_dir = Path(sources_dir)
        result: list[PluginManifest] = []
        if not sources_dir.exists():
            return result
        for mf in sources_dir.rglob("plugin.json"):
            try:
                result.append(PluginManifest.from_json(mf))
            except Exception as exc:
                logger.warning("Bad manifest %s: %s", mf, exc)
        return result

    def install(self, manifest: PluginManifest, source_path: Path) -> InstalledPlugin:
        pid = f"plugin_{uuid4().hex[:12]}"
        install_dir = self.plugins_dir / "installed" / pid
        install_dir.mkdir(parents=True, exist_ok=True)
        if source_path.is_dir():
            shutil.copytree(source_path, install_dir, dirs_exist_ok=True)
        else:
            shutil.copy2(source_path, install_dir / source_path.name)
        installed = InstalledPlugin(
            plugin_id=pid, manifest=manifest, install_path=install_dir,
            installed_at=datetime.now(timezone.utc).isoformat(),
        )
        self._installed[pid] = installed
        self._save_state()
        logger.info("Plugin installed: %s v%s", manifest.name, manifest.version)
        return installed

    def load_tools(self, tool_registry) -> int:
        count = 0
        for plugin in self._installed.values():
            if not plugin.enabled:
                continue
            for td in plugin.manifest.tools:
                entry = td.get("entry_point", "")
                if not entry or ":" not in entry:
                    continue
                try:
                    mod_path, cls_name = entry.rsplit(":", 1)
                    module = __import__(mod_path, fromlist=[cls_name])
                    cls = getattr(module, cls_name)
                    definition = ToolDefinition(
                        tool_name=td["tool_name"],
                        display_name=td.get("display_name", td["tool_name"]),
                        description=td.get("description", ""),
                        category=td.get("category", "custom"),
                        risk_level=RiskLevel(td.get("risk_level", "LOW")),
                        permissions=td.get("permissions", []),
                        requires_approval_by_default=td.get("requires_approval", False),
                    )
                    instance = cls()
                    instance.definition = definition
                    tool_registry.register(instance)
                    plugin.tools_registered.append(td["tool_name"])
                    self._tool_sources[td["tool_name"]] = plugin.plugin_id
                    count += 1
                except Exception as exc:
                    logger.warning("Tool load fail %s: %s", entry, exc)
        return count

    def uninstall(self, plugin_id: str) -> bool:
        if plugin_id not in self._installed:
            return False
        plugin = self._installed.pop(plugin_id)
        if plugin.install_path.exists():
            shutil.rmtree(plugin.install_path, ignore_errors=True)
        self._save_state()
        return True

    def list_installed(self) -> list[dict]:
        return [
            {
                "plugin_id": p.plugin_id, "name": p.manifest.name,
                "version": p.manifest.version, "author": p.manifest.author,
                "description": p.manifest.description, "enabled": p.enabled,
                "tools": p.tools_registered, "installed_at": p.installed_at,
            }
            for p in self._installed.values()
        ]

    def load_state(self) -> None:
        sf = self.plugins_dir / "plugin_state.json"
        if not sf.exists():
            return
        try:
            data = json.loads(sf.read_text(encoding="utf-8"))
            for item in data.get("plugins", []):
                manifest = PluginManifest.from_dict(item["manifest"])
                installed = InstalledPlugin(
                    plugin_id=item["plugin_id"], manifest=manifest,
                    install_path=Path(item["install_path"]),
                    installed_at=item["installed_at"],
                    enabled=item.get("enabled", True),
                    tools_registered=item.get("tools_registered", []),
                )
                self._installed[installed.plugin_id] = installed
                for tn in installed.tools_registered:
                    self._tool_sources[tn] = installed.plugin_id
        except Exception as exc:
            logger.warning("Plugin state load fail: %s", exc)

    # ── Phase 2: Signature Verification ──────────────────────

    @staticmethod
    def compute_signature(manifest: PluginManifest) -> str:
        """Compute SHA-256 signature of manifest content (excluding signature field)."""
        sig = manifest.signature
        manifest.signature = ""  # exclude self from hash
        try:
            raw = json.dumps(manifest.to_dict(), sort_keys=True, ensure_ascii=False)
            return hashlib.sha256(raw.encode("utf-8")).hexdigest()
        finally:
            manifest.signature = sig

    @staticmethod
    def verify_signature(manifest: PluginManifest) -> bool:
        """Verify manifest integrity against its stored signature."""
        if not manifest.signature:
            return False  # unsigned
        expected = PluginRegistry.compute_signature(manifest)
        return expected == manifest.signature

    def install_verified(self, manifest: PluginManifest, source_path: Path) -> InstalledPlugin | None:
        """Install plugin with signature verification.

        Returns None if signature verification fails (tampered manifest).
        """
        if manifest.signature and not self.verify_signature(manifest):
            logger.error("Plugin signature verification FAILED: %s v%s — manifest may be tampered",
                         manifest.name, manifest.version)
            return None
        # Display permissions to user (logged for now; UI integration in Phase 3)
        self._log_permissions(manifest)
        return self.install(manifest, source_path)

    # ── Phase 2: Plugin Isolation ────────────────────────────

    def load_tools_isolated(self, tool_registry) -> int:
        """Load plugin tools with per-tool isolation.

        Each tool class is wrapped so that any exception during execution
        is caught and logged, preventing a single plugin crash from
        bringing down the entire FlowCraft process.
        """
        count = 0
        for plugin in self._installed.values():
            if not plugin.enabled:
                continue
            for td in plugin.manifest.tools:
                entry = td.get("entry_point", "")
                if not entry or ":" not in entry:
                    continue
                try:
                    mod_path, cls_name = entry.rsplit(":", 1)
                    module = __import__(mod_path, fromlist=[cls_name])
                    cls = getattr(module, cls_name)
                    definition = ToolDefinition(
                        tool_name=td["tool_name"],
                        display_name=td.get("display_name", td["tool_name"]),
                        description=td.get("description", ""),
                        category=td.get("category", "custom"),
                        risk_level=RiskLevel(td.get("risk_level", "LOW")),
                        permissions=td.get("permissions", []),
                        requires_approval_by_default=td.get("requires_approval", False),
                    )
                    instance = cls()
                    instance.definition = definition
                    # Wrap with isolation
                    isolated = _IsolatedToolWrapper(instance, plugin.plugin_id)
                    tool_registry.register(isolated)
                    plugin.tools_registered.append(td["tool_name"])
                    self._tool_sources[td["tool_name"]] = plugin.plugin_id
                    count += 1
                except Exception as exc:
                    logger.error("Plugin tool load failed (isolated): %s from %s — %s",
                                 entry, plugin.manifest.name, exc)
        return count

    @staticmethod
    def _log_permissions(manifest: PluginManifest) -> None:
        """Log the permissions a plugin declares for user review."""
        perms = manifest.permissions or ["(no permissions declared)"]
        logger.info(
            "Plugin [%s v%s] requests permissions: %s",
            manifest.name, manifest.version, ", ".join(perms),
        )

    def _save_state(self) -> None:
        sf = self.plugins_dir / "plugin_state.json"
        data = {
            "plugins": [
                {
                    "plugin_id": p.plugin_id, "manifest": p.manifest.to_dict(),
                    "install_path": str(p.install_path),
                    "installed_at": p.installed_at, "enabled": p.enabled,
                    "tools_registered": p.tools_registered,
                }
                for p in self._installed.values()
            ]
        }
        sf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Phase 2: Isolated Tool Wrapper ────────────────────────────

class _IsolatedToolWrapper(Tool):
    """Wraps a plugin tool to catch and isolate exceptions.

    If a plugin tool raises an unhandled exception, the wrapper catches it,
    logs the error, and returns a safe error message — preventing the crash
    from propagating to FlowCraft's main execution loop.
    """

    def __init__(self, inner: Tool, plugin_id: str) -> None:
        self._inner = inner
        self._plugin_id = plugin_id
        # Forward definition so ToolRegistry sees it
        self.definition = inner.definition

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        try:
            return await self._inner.execute(**kwargs)
        except Exception as exc:
            logger.error(
                "Plugin tool [%s/%s] crashed during execution: %s",
                self._plugin_id, self.definition.tool_name, exc,
            )
            return {
                "status": "error",
                "error": f"Plugin tool execution failed: {exc}",
                "plugin_id": self._plugin_id,
                "tool_name": self.definition.tool_name,
            }
