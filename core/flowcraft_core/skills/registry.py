"""SkillRegistry — discover, load, validate, and hot-reload skills.

Mirrors OpenClaw's progressive skill disclosure pattern:
  1. Load skill names + descriptions upfront (token-cheap)
  2. Inject full SKILL.md content only when skill is activated
  3. Support deterministic script execution via subprocess
  4. Hot-reload: watch for file changes with debounce (Phase 3)
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import threading
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from flowcraft_core.skills.models import (
    SkillDefinition, SkillManifest, SkillExecutionResult,
)

logger = logging.getLogger(__name__)

_SKILL_FILENAME = "SKILL.md"


class SkillRegistry:
    """Central registry for all skills across multiple source directories.

    Priority order (highest first):
      1. workspace/skills/      — user-created skills (highest priority)
      2. skills_generated/      — auto-saved from successful dynamic scripts (Phase 3)
      3. skills_marketplace/    — downloaded from community
      4. builtin/               — bundled with FlowCraft
    """

    def __init__(
        self,
        workspace_skills_dir: str | Path | None = None,
        builtin_skills_dir: str | Path | None = None,
    ) -> None:
        self._skills: dict[str, SkillManifest] = {}  # qualified_name → manifest
        self._lock = threading.RLock()

        # Source directories (priority-ordered)
        self._source_dirs: list[Path] = []
        if workspace_skills_dir:
            self._source_dirs.append(Path(workspace_skills_dir))
        if builtin_skills_dir:
            self._source_dirs.append(Path(builtin_skills_dir))

        # Agent-generated skills: sibling to workspace
        if workspace_skills_dir:
            agent_dir = Path(workspace_skills_dir).parent / "skills_generated"
            self._source_dirs.append(agent_dir)

        # Marketplace
        if workspace_skills_dir:
            marketplace_dir = Path(workspace_skills_dir).parent / "skills_marketplace"
            self._source_dirs.append(marketplace_dir)

        # Hot-reload
        self._watcher_thread: threading.Thread | None = None
        self._debounce_sec = 2.5
        self._last_mtimes: dict[str, float] = {}
        self._watch_interval = 10.0

    # ── Public API ──────────────────────────────────────────

    def discover_all(self) -> int:
        """Scan all source directories and load skills. Returns count loaded."""
        with self._lock:
            count = 0
            for src_dir in self._source_dirs:
                if not src_dir.is_dir():
                    continue
                for skill_dir in src_dir.iterdir():
                    if not skill_dir.is_dir():
                        continue
                    skill_md = skill_dir / _SKILL_FILENAME
                    if not skill_md.exists():
                        continue
                    try:
                        manifest = self._parse_skill_file(skill_md, str(skill_dir))
                        if manifest:
                            key = manifest.definition.qualified_name
                            # First source dir wins (higher priority)
                            if key not in self._skills:
                                self._skills[key] = manifest
                                count += 1
                                logger.debug("Discovered skill: %s from %s", key, src_dir)
                    except Exception as exc:
                        logger.warning("Failed to parse skill %s: %s", skill_dir, exc)

            logger.info(
                "SkillRegistry: discovered %d skills from %d source dirs",
                count, len(self._source_dirs),
            )
            return count

    def list_skills(self, enabled_only: bool = True) -> list[SkillDefinition]:
        """Return all skill definitions (progressive disclosure: metadata only)."""
        with self._lock:
            result = []
            for m in self._skills.values():
                if enabled_only and not m.definition.enabled:
                    continue
                result.append(m.definition)
            return sorted(result, key=lambda s: s.usage_count, reverse=True)

    def get_skill(self, qualified_name: str) -> SkillManifest | None:
        """Get full skill manifest by qualified_name (e.g. 'skill.data.data_analysis')."""
        with self._lock:
            return self._skills.get(qualified_name)

    def get_skill_by_name(self, name: str) -> SkillManifest | None:
        """Find skill by simple name (without category prefix)."""
        with self._lock:
            for m in self._skills.values():
                if m.definition.name == name:
                    return m
            return None

    def resolve_skill(self, name: str) -> SkillManifest | None:
        """Resolve skill name in any format:
        - "skill.network.web_scrape" (qualified name)
        - "network.web_scrape"         (category.name without 'skill.' prefix)
        - "web_scrape"                 (simple name only)
        """
        # 1. Try qualified name directly
        manifest = self.get_skill(name)
        if manifest:
            return manifest
        # 2. Try with "skill." prefix (LLM often drops this)
        manifest = self.get_skill(f"skill.{name}")
        if manifest:
            return manifest
        # 3. Try simple name
        return self.get_skill_by_name(name)

    def get_skills_summary(self) -> str:
        """Progressive disclosure: compact summary for planner prompt injection.

        Returns a markdown list with one line per skill — cheap in tokens.
        """
        with self._lock:
            summaries = []
            for m in self._skills.values():
                if not m.definition.enabled:
                    continue
                summaries.append(m.definition.to_prompt_summary())
            if not summaries:
                return "(No skills available)"
            return "\n".join(summaries)

    def get_agent_context(self, qualified_name: str) -> str | None:
        """Get full agent-readable context for an activated skill.

        This is the full SKILL.md body + schema info, injected into the LLM
        prompt only when the skill is actually needed (progressive disclosure).
        """
        manifest = self.resolve_skill(qualified_name)
        if not manifest:
            return None

        # Track usage
        manifest.definition.usage_count += 1
        manifest.definition.last_used = datetime.now(timezone.utc).isoformat()
        return manifest.to_agent_context()

    async def execute_skill(
        self,
        qualified_name: str,
        params: dict[str, Any] | None = None,
        timeout_seconds: int | None = None,
    ) -> SkillExecutionResult:
        """Execute a skill's deterministic script with parameters.

        Runs the script in a subprocess. Python scripts receive params as JSON
        via stdin. Bash scripts receive params as SKILL_* environment variables.
        """
        manifest = self.resolve_skill(qualified_name)
        if not manifest:
            return SkillExecutionResult(
                skill_name=qualified_name,
                status="FAILED",
                error=f"Skill '{qualified_name}' not found",
            )

        definition = manifest.definition
        script_path = definition.full_script_path
        if not script_path or not script_path.exists():
            return SkillExecutionResult(
                skill_name=qualified_name,
                status="FAILED",
                error=f"Script not found: {script_path}",
            )

        if not definition.enabled:
            return SkillExecutionResult(
                skill_name=qualified_name,
                status="DENIED",
                error=f"Skill '{qualified_name}' is disabled",
            )

        timeout = timeout_seconds or definition.timeout_seconds
        params = params or {}
        t0 = _time.monotonic()

        try:
            if definition.script_language == "python":
                result = await asyncio.wait_for(
                    self._run_python_script(script_path, params, definition.skill_dir),
                    timeout=timeout,
                )
            elif definition.script_language == "bash":
                result = await asyncio.wait_for(
                    self._run_bash_script(script_path, params, definition.skill_dir),
                    timeout=timeout,
                )
            else:
                return SkillExecutionResult(
                    skill_name=qualified_name,
                    status="FAILED",
                    error=f"Unsupported script language: {definition.script_language}",
                )

            elapsed = _time.monotonic() - t0
            result.elapsed_seconds = round(elapsed, 3)

            # Track success/failure
            if result.is_success:
                definition.success_count += 1
            else:
                definition.fail_count += 1

            return result
        except asyncio.TimeoutError:
            elapsed = _time.monotonic() - t0
            definition.fail_count += 1
            return SkillExecutionResult(
                skill_name=qualified_name,
                status="TIMEOUT",
                error=f"Script timed out after {timeout}s",
                elapsed_seconds=round(elapsed, 3),
            )
        except Exception as exc:
            elapsed = _time.monotonic() - t0
            definition.fail_count += 1
            return SkillExecutionResult(
                skill_name=qualified_name,
                status="FAILED",
                error=str(exc),
                elapsed_seconds=round(elapsed, 3),
            )

    # ── Script Runners ──────────────────────────────────────

    async def _run_python_script(
        self, script_path: Path, params: dict, skill_dir: str
    ) -> SkillExecutionResult:
        """Run Python script: params → JSON stdin, stdout → result."""
        proc = await asyncio.create_subprocess_exec(
            "python", str(script_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=skill_dir,
        )
        input_json = json.dumps(params, ensure_ascii=False)
        stdout, stderr = await proc.communicate(input=input_json.encode("utf-8"))

        output = stdout.decode("utf-8", errors="replace").strip()
        error_text = stderr.decode("utf-8", errors="replace").strip()

        output_payload = {}
        try:
            output_payload = json.loads(output)
        except json.JSONDecodeError:
            output_payload = {"raw_output": output}

        return SkillExecutionResult(
            skill_name=script_path.parent.name,
            status="SUCCESS" if proc.returncode == 0 else "FAILED",
            output=output[:50000],
            error=error_text if error_text else None,
            output_payload=output_payload,
        )

    async def _run_bash_script(
        self, script_path: Path, params: dict, skill_dir: str
    ) -> SkillExecutionResult:
        """Run Bash script: params → SKILL_* env vars."""
        env = {**__import__('os').environ}
        for k, v in params.items():
            env[f"SKILL_{k.upper()}"] = str(v)

        proc = await asyncio.create_subprocess_exec(
            "bash", str(script_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=skill_dir,
            env=env,
        )
        stdout, stderr = await proc.communicate()

        output = stdout.decode("utf-8", errors="replace").strip()
        error_text = stderr.decode("utf-8", errors="replace").strip()

        return SkillExecutionResult(
            skill_name=script_path.parent.name,
            status="SUCCESS" if proc.returncode == 0 else "FAILED",
            output=output[:50000],
            error=error_text if error_text else None,
            output_payload={"raw_output": output},
        )

    # ── Skill Management (Phase 3: Self-writing agent) ──────

    def save_agent_skill(
        self,
        name: str,
        description: str,
        category: str,
        script_code: str,
        tags: list[str] | None = None,
    ) -> SkillDefinition | None:
        """Save an agent-generated skill to disk (Phase 3).

        Creates the full directory structure:
          skills_generated/{name}/
            SKILL.md
            scripts/main.py
        """
        with self._lock:
            # Find or create agent_generated directory
            agent_dir = None
            for d in self._source_dirs:
                if "generated" in str(d).lower() or "agent" in str(d).lower():
                    agent_dir = d
                    break
            if not agent_dir and self._source_dirs:
                agent_dir = self._source_dirs[0].parent / "skills_generated"

            if not agent_dir:
                logger.warning("No agent_generated directory available")
                return None

            skill_dir = agent_dir / name
            skill_dir.mkdir(parents=True, exist_ok=True)
            scripts_dir = skill_dir / "scripts"
            scripts_dir.mkdir(exist_ok=True)

            # Write SKILL.md
            frontmatter = {
                "name": name,
                "description": description,
                "category": category,
                "version": "1.0.0",
                "author": "flowcraft-agent",
                "source": "agent_generated",
                "script_path": "scripts/main.py",
                "script_language": "python",
                "tags": tags or [],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            skill_md = "---\n"
            skill_md += yaml.dump(frontmatter, allow_unicode=True, sort_keys=False)
            skill_md += "---\n\n"
            skill_md += f"# {name}\n\n{description}\n\n"
            skill_md += "## Usage\n\n"
            skill_md += (
                "This skill was auto-generated from a successful "
                "dynamic script execution.\n"
            )
            skill_md += (
                "Run `{baseDir}/scripts/main.py` with the appropriate "
                "parameters.\n"
            )

            (skill_dir / _SKILL_FILENAME).write_text(skill_md, encoding="utf-8")

            # Write script
            (scripts_dir / "main.py").write_text(script_code, encoding="utf-8")

            # Parse and register
            manifest = self._parse_skill_file(
                skill_dir / _SKILL_FILENAME, str(skill_dir)
            )
            if manifest:
                manifest.definition.source = "agent_generated"
                self._skills[manifest.definition.qualified_name] = manifest
                logger.info(
                    "Agent-generated skill saved: %s (%s)",
                    manifest.definition.qualified_name, skill_dir,
                )
                return manifest.definition
            return None

    def enable_skill(self, qualified_name: str) -> bool:
        """Enable a skill by qualified name."""
        with self._lock:
            m = self._skills.get(qualified_name)
            if m:
                m.definition.enabled = True
                return True
            return False

    def disable_skill(self, qualified_name: str) -> bool:
        """Disable a skill by qualified name."""
        with self._lock:
            m = self._skills.get(qualified_name)
            if m:
                m.definition.enabled = False
                return True
            return False

    def get_skill_stats(self) -> dict[str, dict]:
        """Get usage statistics for all skills (Phase 3 marketplace analytics)."""
        with self._lock:
            stats = {}
            for key, m in self._skills.items():
                d = m.definition
                stats[key] = {
                    "name": d.name,
                    "usage_count": d.usage_count,
                    "success_count": d.success_count,
                    "fail_count": d.fail_count,
                    "success_rate": round(d.success_rate, 3),
                    "last_used": d.last_used,
                    "source": d.source,
                }
            return stats

    # ── Hot Reload (Phase 3) ────────────────────────────────

    def start_hot_reload(self) -> None:
        """Start background thread watching for SKILL.md file changes.

        When a skill file is modified, it's reloaded automatically.
        Runtime stats (usage_count, etc.) are preserved across reloads.
        """
        if self._watcher_thread and self._watcher_thread.is_alive():
            return
        self._watcher_thread = threading.Thread(
            target=self._watch_loop, daemon=True, name="skill-watcher"
        )
        self._watcher_thread.start()
        logger.info(
            "Skill hot-reload watcher started (interval=%ss, debounce=%ss)",
            self._watch_interval, self._debounce_sec,
        )

    def _watch_loop(self) -> None:
        """Background loop: check for modified SKILL.md files."""
        while True:
            _time.sleep(self._watch_interval)
            try:
                self._check_for_changes()
            except Exception as exc:
                logger.debug("Skill watcher error: %s", exc)

    def _check_for_changes(self) -> None:
        """Scan all source dirs for modified SKILL.md files."""
        with self._lock:
            current_mtimes: dict[str, float] = {}
            for src_dir in self._source_dirs:
                if not src_dir.is_dir():
                    continue
                for skill_dir in src_dir.iterdir():
                    if not skill_dir.is_dir():
                        continue
                    skill_md = skill_dir / _SKILL_FILENAME
                    if not skill_md.exists():
                        continue
                    mtime = skill_md.stat().st_mtime
                    key = str(skill_md)
                    current_mtimes[key] = mtime
                    prev = self._last_mtimes.get(key, 0)
                    if mtime > prev + self._debounce_sec:
                        try:
                            manifest = self._parse_skill_file(
                                skill_md, str(skill_dir))
                            if manifest:
                                qname = manifest.definition.qualified_name
                                # Preserve runtime stats
                                old = self._skills.get(qname)
                                if old:
                                    d = manifest.definition
                                    od = old.definition
                                    d.usage_count = od.usage_count
                                    d.success_count = od.success_count
                                    d.fail_count = od.fail_count
                                    d.last_used = od.last_used
                                self._skills[qname] = manifest
                                logger.info("Hot-reloaded skill: %s", qname)
                        except Exception as exc:
                            logger.warning(
                                "Failed to hot-reload %s: %s", skill_md, exc)
            self._last_mtimes = current_mtimes

    # ── Internal: SKILL.md Parser ───────────────────────────

    def _parse_skill_file(
        self, skill_md_path: Path, skill_dir: str
    ) -> SkillManifest | None:
        """Parse a SKILL.md file: extract YAML frontmatter + markdown body."""
        content = skill_md_path.read_text(encoding="utf-8")

        # Parse YAML frontmatter (between --- delimiters)
        frontmatter: dict[str, Any] = {}
        body = content
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    frontmatter = yaml.safe_load(parts[1]) or {}
                except yaml.YAMLError as exc:
                    logger.warning(
                        "Invalid YAML frontmatter in %s: %s", skill_md_path, exc)
                    return None
                body = parts[2].strip()

        name = frontmatter.get("name", skill_md_path.parent.name)
        if not name:
            logger.warning("Skill %s has no name", skill_md_path)
            return None

        definition = SkillDefinition(
            name=name,
            description=str(
                frontmatter.get(
                    "description", f"Skill: {name}")),
            category=str(frontmatter.get("category", "general")),
            version=str(frontmatter.get("version", "1.0.0")),
            author=str(frontmatter.get("author", "flowcraft")),
            requires_approval=bool(frontmatter.get("requires_approval", False)),
            timeout_seconds=int(frontmatter.get("timeout_seconds", 60)),
            script_path=str(frontmatter.get("script_path", "")) or None,
            script_language=str(frontmatter.get("script_language", "python")),
            input_schema=frontmatter.get("input_schema", {}),
            output_schema=frontmatter.get("output_schema", {}),
            tags=list(frontmatter.get("tags", [])),
            dependencies=list(frontmatter.get("dependencies", [])),
            skill_dir=skill_dir,
            source=str(frontmatter.get("source", "workspace")),
            enabled=bool(frontmatter.get("enabled", True)),
            created_at=str(
                frontmatter.get(
                    "created_at",
                    datetime.now(timezone.utc).isoformat())),
        )

        return SkillManifest(
            definition=definition,
            body=body,
            raw_frontmatter=frontmatter,
        )
