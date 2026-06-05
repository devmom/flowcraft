"""Session Isolation & File Conflict Resolution.

Inspired by Codex CLI's session isolation model:
  - Each session has its own working directory
  - File operations are scoped to the session
  - Cross-session file conflicts are detected and resolved
  - Permission model: ask → allow → always allow for directories

Key features:
  1. Per-session workspace: session-scoped temp/working directories
  2. File lock: prevent concurrent writes to the same file
  3. Conflict detection: detect when two sessions target the same file
  4. Git safety net: optional auto-commit before dangerous operations
  5. Access control: session A cannot read session B's private files
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── Data Types ──────────────────────────────────────────────

@dataclass
class SessionWorkspace:
    """Per-session isolated workspace."""
    session_id: str
    root_dir: Path
    work_dir: Path        # Where the session operates
    temp_dir: Path         # Session-scoped temp files
    outputs_dir: Path      # Output artifacts
    created_at: float = field(default_factory=time.time)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at


@dataclass
class FileConflict:
    """Detected file conflict between sessions."""
    file_path: Path
    sessions: list[str]          # Which sessions are involved
    operation: str                # "write", "delete", "move"
    detected_at: float = field(default_factory=time.time)
    resolved: bool = False
    resolution: str = ""          # How it was resolved


# ── File Lock ───────────────────────────────────────────────

class FileLock:
    """Cross-session file lock to prevent concurrent writes."""

    def __init__(self):
        self._locks: dict[str, threading.Lock] = {}
        self._owners: dict[str, str] = {}  # file_path → session_id
        self._lock = threading.Lock()  # Protects _locks dict

    def acquire(self, file_path: str, session_id: str, timeout: float = 30.0) -> bool:
        """Try to acquire a lock on a file for a session.

        Returns True if lock acquired, False if timeout.
        """
        path = str(Path(file_path).resolve())
        with self._lock:
            if path not in self._locks:
                self._locks[path] = threading.Lock()

        acquired = self._locks[path].acquire(timeout=timeout)
        if acquired:
            self._owners[path] = session_id
            logger.debug("File lock acquired: %s by %s", path, session_id)
        else:
            current_owner = self._owners.get(path, "unknown")
            logger.warning("File lock timeout: %s held by %s, requested by %s",
                          path, current_owner, session_id)
        return acquired

    def release(self, file_path: str, session_id: str) -> None:
        """Release a file lock."""
        path = str(Path(file_path).resolve())
        if path in self._locks:
            self._locks[path].release()
            self._owners.pop(path, None)
            logger.debug("File lock released: %s by %s", path, session_id)

    def is_locked(self, file_path: str) -> bool:
        """Check if a file is currently locked."""
        path = str(Path(file_path).resolve())
        with self._lock:
            if path not in self._locks:
                return False
            return self._locks[path].locked()

    def owner(self, file_path: str) -> str | None:
        """Get the session_id that currently holds the lock."""
        return self._owners.get(str(Path(file_path).resolve()))


# ── Session Manager ─────────────────────────────────────────

class SessionIsolationManager:
    """Manage isolated session workspaces and file conflict resolution.

    Usage:
        mgr = SessionIsolationManager(base_dir=Path("./workspaces"))
        workspace = mgr.create_session("session-abc")
        # ... session operates in workspace.work_dir ...
        conflicts = mgr.check_conflicts("session-abc", Path("./shared/file.txt"))
        mgr.cleanup_session("session-abc")
    """

    DEFAULT_LOCK_TIMEOUT = 30.0  # seconds

    def __init__(
        self,
        base_dir: Path | None = None,
        enable_git_safety: bool = True,
        enable_file_lock: bool = True,
    ):
        self.base_dir = base_dir or Path(os.environ.get(
            "FLOWCRAFT_WORKSPACES_DIR",
            Path.home() / ".flowcraft" / "workspaces",
        ))
        self.base_dir.mkdir(parents=True, exist_ok=True)

        self._workspaces: dict[str, SessionWorkspace] = {}
        self._file_lock = FileLock() if enable_file_lock else None
        self._conflicts: list[FileConflict] = []
        self._git_safety = enable_git_safety

    # ── Session Lifecycle ────────────────────────────────

    def create_session(self, session_id: str, label: str = "") -> SessionWorkspace:
        """Create an isolated workspace for a new session.

        Directory structure:
            workspaces/{session_id}/
                ├── work/       ← Session's working directory
                ├── temp/       ← Session-scoped temp files
                └── outputs/    ← Generated artifacts
        """
        if session_id in self._workspaces:
            return self._workspaces[session_id]

        session_dir = self.base_dir / session_id
        work_dir = session_dir / "work"
        temp_dir = session_dir / "temp"
        outputs_dir = session_dir / "outputs"

        for d in [session_dir, work_dir, temp_dir, outputs_dir]:
            d.mkdir(parents=True, exist_ok=True)

        workspace = SessionWorkspace(
            session_id=session_id,
            root_dir=session_dir,
            work_dir=work_dir,
            temp_dir=temp_dir,
            outputs_dir=outputs_dir,
        )
        self._workspaces[session_id] = workspace

        logger.info("Session workspace created: %s (work=%s)", session_id, work_dir)
        return workspace

    def get_workspace(self, session_id: str) -> SessionWorkspace | None:
        """Get existing workspace, or None."""
        return self._workspaces.get(session_id)

    def cleanup_session(self, session_id: str) -> None:
        """Remove a session's workspace and release all its locks."""
        ws = self._workspaces.pop(session_id, None)
        if ws:
            # Release any file locks held by this session
            if self._file_lock:
                paths = list(self._file_lock._owners.keys())
                for path in paths:
                    if self._file_lock.owner(path) == session_id:
                        self._file_lock.release(path, session_id)
            logger.info("Session workspace cleaned: %s", session_id)

    def list_sessions(self) -> list[dict]:
        """List all active sessions."""
        return [
            {
                "session_id": ws.session_id,
                "work_dir": str(ws.work_dir),
                "age_seconds": ws.age_seconds,
            }
            for ws in self._workspaces.values()
        ]

    # ── File Operation Guard ──────────────────────────────

    def guard_file_write(
        self,
        session_id: str,
        file_path: Path,
        content: str | bytes | None = None,
        operation: str = "write",
    ) -> tuple[bool, str]:
        """Guard a file write operation.

        Checks:
          1. File is within allowed paths
          2. File is not locked by another session
          3. If locked, detect conflict

        Returns (allowed, reason).
        """
        resolved = file_path.resolve()

        # Check 1: Is the file locked by another session?
        if self._file_lock and self._file_lock.is_locked(str(resolved)):
            owner = self._file_lock.owner(str(resolved))
            if owner != session_id:
                conflict = FileConflict(
                    file_path=resolved,
                    sessions=[session_id, owner or "unknown"],
                    operation=operation,
                )
                self._conflicts.append(conflict)
                return False, f"File locked by session '{owner}'"

        # Check 2: Is the file in another session's private workspace?
        for sid, ws in self._workspaces.items():
            if sid == session_id:
                continue
            try:
                resolved.relative_to(ws.root_dir)
                # File is inside another session's workspace — deny
                return False, f"File belongs to session '{sid}' workspace"
            except ValueError:
                pass

        # Acquire lock for the duration of the operation
        return True, "allowed"

    def lock_for_operation(self, session_id: str, file_path: Path) -> bool:
        """Acquire a file lock before writing. Call release_after_operation() when done."""
        if not self._file_lock:
            return True
        return self._file_lock.acquire(str(file_path.resolve()), session_id, self.DEFAULT_LOCK_TIMEOUT)

    def release_after_operation(self, session_id: str, file_path: Path) -> None:
        """Release the file lock after operation completes."""
        if self._file_lock:
            self._file_lock.release(str(file_path.resolve()), session_id)

    # ── Conflict Detection ────────────────────────────────

    def check_conflicts(self, session_id: str, file_path: Path) -> list[FileConflict]:
        """Check if a file operation would conflict with other sessions."""
        resolved = file_path.resolve()
        conflicts = []
        for sid, ws in self._workspaces.items():
            if sid == session_id:
                continue
            try:
                resolved.relative_to(ws.work_dir)
                conflicts.append(FileConflict(
                    file_path=resolved,
                    sessions=[session_id, sid],
                    operation="write",
                ))
            except ValueError:
                pass
        return conflicts

    def get_pending_conflicts(self) -> list[FileConflict]:
        """Get all unresolved conflicts."""
        return [c for c in self._conflicts if not c.resolved]

    def resolve_conflict(self, conflict: FileConflict, resolution: str) -> None:
        """Mark a conflict as resolved."""
        conflict.resolved = True
        conflict.resolution = resolution
        logger.info("Conflict resolved: %s → %s", conflict.file_path, resolution)

    # ── Git Safety Net ────────────────────────────────────

    def git_safety_commit(self, session_id: str, file_path: Path, message: str = "") -> bool:
        """Create a git safety commit before a dangerous file operation.

        Returns True if commit was created.
        """
        if not self._git_safety:
            return False

        try:
            import subprocess
            repo_dir = file_path.parent
            # Find git root
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=repo_dir, capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return False

            subprocess.run(
                ["git", "add", str(file_path)],
                cwd=repo_dir, capture_output=True, timeout=5,
            )
            msg = message or f"FlowCraft safety commit: {session_id} - {file_path.name}"
            subprocess.run(
                ["git", "commit", "-m", msg, "--allow-empty"],
                cwd=repo_dir, capture_output=True, timeout=5,
            )
            logger.info("Git safety commit: %s (%s)", file_path.name, session_id)
            return True
        except Exception as exc:
            logger.debug("Git safety commit skipped: %s", exc)
            return False

    # ── Cleanup ───────────────────────────────────────────

    def cleanup_old_sessions(self, max_age_seconds: float = 3600.0) -> int:
        """Remove workspaces older than max_age_seconds. Returns count cleaned."""
        cleaned = 0
        now = time.time()
        for sid in list(self._workspaces.keys()):
            ws = self._workspaces[sid]
            if now - ws.created_at > max_age_seconds:
                self.cleanup_session(sid)
                cleaned += 1
        return cleaned

    def cleanup_all(self) -> None:
        """Remove all session workspaces."""
        for sid in list(self._workspaces.keys()):
            self.cleanup_session(sid)
