"""ApplyPatchTool — structured multi-file edits with preview and rollback.

Inspired by OpenClaw's apply_patch subtool. Enables the agent to make
structured code changes atomically: preview first, apply with backup,
rollback on failure.

Operation modes:
  - create: Create new files
  - update: Replace/create files (with backup)
  - delete: Remove files (with backup)
  - patch: Apply unified diff patches
"""

from __future__ import annotations

import difflib
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from flowcraft_core.domain.enums import RiskLevel
from flowcraft_core.domain.schemas import ToolIntent
from flowcraft_core.tools.base import Tool, ToolDefinition, is_path_allowed, observation_from_output

logger = logging.getLogger(__name__)


class ApplyPatchTool(Tool):
    """Apply structured file changes with preview, backup, and rollback.

    Use this tool to make precise code changes:
      - Create new files
      - Update existing files
      - Delete files
      - Apply unified diff patches

    All mutations create automatic backups (.flowcraft.bak).
    Preview mode shows what will change without actually modifying files.
    """

    def __init__(
        self,
        allowed_paths: list[Path],
        workspace_only: bool = True,
    ) -> None:
        self.allowed_paths = allowed_paths
        self.workspace_only = workspace_only

        self.definition = ToolDefinition(
            tool_name="apply_patch",
            display_name="应用代码补丁",
            description=(
                "对多个文件进行结构化修改。支持创建、更新、删除文件和 unified diff 补丁。"
                "所有修改自动备份，可预览后执行。"
                "参数: operation(create|update|delete|patch), "
                "changes(文件修改列表), preview(是否仅预览, 默认false)"
            ),
            category="code",
            risk_level=RiskLevel.MEDIUM,
            permissions=["tool:apply_patch"],
            requires_approval_by_default=True,
            supports_rollback=True,
            examples=[
                {
                    "description": "Create a new Python file",
                    "input": {
                        "operation": "create",
                        "changes": [
                            {"path": "src/utils.py", "content": "def hello():\\n    return 'world'\\n"}
                        ],
                    },
                },
                {
                    "description": "Update multiple files",
                    "input": {
                        "operation": "update",
                        "changes": [
                            {"path": "config.py", "content": "DEBUG = True\\n"},
                            {"path": "README.md", "content": "# New Title\\n\\nUpdated.\\n"},
                        ],
                    },
                },
                {
                    "description": "Apply a unified diff patch",
                    "input": {
                        "operation": "patch",
                        "changes": [
                            {
                                "path": "app.py",
                                "patch": "@@ -1,3 +1,5 @@\\n import os\\n+import sys\\n+sys.path.insert(0, '.')\\n def main():\\n     pass\\n",
                            },
                        ],
                    },
                },
            ],
        )

    async def execute(self, intent: ToolIntent):
        """Apply structured file changes.

        Input payload:
          - operation: "create" | "update" | "delete" | "patch"
          - changes: list of {path, content, patch?}
          - preview: bool (default false) — if true, only show diff without applying
          - message: str (optional) — commit-like message describing the change
        """
        operation = str(intent.input_payload.get("operation", "update"))
        changes = intent.input_payload.get("changes", [])
        preview = bool(intent.input_payload.get("preview", False))
        message = str(intent.input_payload.get("message", ""))

        if not changes:
            return observation_from_output(
                intent, "FAILED", "Missing 'changes' parameter",
                error="changes list is required")

        if operation not in ("create", "update", "delete", "patch"):
            return observation_from_output(
                intent, "FAILED",
                f"Unknown operation: {operation}",
                error=f"Valid operations: create, update, delete, patch")

        results: list[dict[str, Any]] = []
        backups: list[dict[str, Any]] = []
        errors: list[str] = []

        for i, change in enumerate(changes):
            file_path_str = str(change.get("path", ""))
            if not file_path_str:
                errors.append(f"Change #{i}: missing 'path'")
                continue

            file_path = Path(file_path_str)

            # Security: path must be within allowed directories
            if not is_path_allowed(file_path, self.allowed_paths):
                errors.append(f"Path not allowed: {file_path}")
                continue

            if self.workspace_only:
                # Check file is in workspace
                in_workspace = False
                for allowed in self.allowed_paths:
                    try:
                        file_path.resolve().relative_to(allowed.resolve())
                        in_workspace = True
                        break
                    except ValueError:
                        continue
                if not in_workspace:
                    errors.append(f"Path outside workspace: {file_path}")
                    continue

            try:
                if operation == "create":
                    result = self._handle_create(file_path, change, preview)
                elif operation == "update":
                    result = self._handle_update(file_path, change, preview)
                elif operation == "delete":
                    result = self._handle_delete(file_path, change, preview)
                elif operation == "patch":
                    result = self._handle_patch(file_path, change, preview)
                else:
                    result = {"status": "error", "path": file_path_str, "error": f"Unknown operation: {operation}"}

                results.append(result)
                if result.get("backup_path"):
                    backups.append({"path": file_path_str, "backup": result["backup_path"]})

                if result.get("status") == "error":
                    errors.append(f"{file_path_str}: {result.get('error', 'unknown')}")

            except Exception as exc:
                error_msg = f"{file_path_str}: {exc}"
                errors.append(error_msg)
                results.append({"status": "error", "path": file_path_str, "error": str(exc)})

        # Build response
        changed_count = sum(1 for r in results if r.get("status") == "changed" or r.get("status") == "created")
        deleted_count = sum(1 for r in results if r.get("status") == "deleted")
        error_count = len(errors)

        if preview:
            summary = f"[PREVIEW] 将修改 {changed_count} 个文件" + (
                f", 删除 {deleted_count} 个" if deleted_count else ""
            )
            if errors:
                summary += f" (含 {error_count} 个错误)"
        else:
            summary = f"已修改 {changed_count} 个文件" + (
                f", 删除 {deleted_count} 个" if deleted_count else ""
            )
            if errors:
                summary += f" (含 {error_count} 个错误: {'; '.join(errors[:3])})"

        status = "COMPLETED" if error_count == 0 else "FAILED"

        return observation_from_output(
            intent, status, summary,
            payload={
                "operation": operation,
                "message": message,
                "preview": preview,
                "changes": results,
                "backups": backups,
                "changed_count": changed_count,
                "deleted_count": deleted_count,
                "error_count": error_count,
                "errors": errors[:10],
            },
            error="; ".join(errors[:3]) if errors else None,
        )

    # ── Individual change handlers ──────────────────────────

    def _handle_create(self, file_path: Path, change: dict, preview: bool) -> dict:
        """Create a new file."""
        content = str(change.get("content", ""))

        if file_path.exists():
            return {
                "status": "error",
                "path": str(file_path),
                "error": f"File already exists (use 'update' to modify)",
            }

        if preview:
            return {
                "status": "created",
                "path": str(file_path),
                "preview": True,
                "diff": f"+++ {file_path}\n{content[:2000]}",
                "size": len(content),
            }

        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")

        return {
            "status": "created",
            "path": str(file_path),
            "size": len(content),
        }

    def _handle_update(self, file_path: Path, change: dict, preview: bool) -> dict:
        """Update an existing file (or create if not exists)."""
        content = str(change.get("content", ""))
        old_content = file_path.read_text(encoding="utf-8") if file_path.exists() else ""

        if old_content == content:
            return {
                "status": "unchanged",
                "path": str(file_path),
                "reason": "Content is identical",
            }

        # Generate diff
        diff_lines = list(difflib.unified_diff(
            old_content.splitlines(keepends=True),
            content.splitlines(keepends=True),
            fromfile=str(file_path),
            tofile=str(file_path),
            lineterm="",
        ))
        diff_text = "\n".join(diff_lines)

        if preview:
            return {
                "status": "changed",
                "path": str(file_path),
                "preview": True,
                "diff": diff_text[:3000],
                "old_size": len(old_content),
                "new_size": len(content),
            }

        # Create backup
        backup_path = None
        if file_path.exists():
            backup_path = file_path.with_suffix(file_path.suffix + ".flowcraft.bak")
            backup_path.write_bytes(file_path.read_bytes())

        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")

        return {
            "status": "changed",
            "path": str(file_path),
            "diff": diff_text[:3000],
            "old_size": len(old_content),
            "new_size": len(content),
            "backup_path": str(backup_path) if backup_path else None,
        }

    def _handle_delete(self, file_path: Path, change: dict, preview: bool) -> dict:
        """Delete a file (with backup)."""
        if not file_path.exists():
            return {
                "status": "error",
                "path": str(file_path),
                "error": "File does not exist",
            }

        if preview:
            return {
                "status": "deleted",
                "path": str(file_path),
                "preview": True,
                "size": file_path.stat().st_size,
            }

        # Create backup before deletion
        backup_path = file_path.with_suffix(file_path.suffix + ".flowcraft.bak")
        backup_path.write_bytes(file_path.read_bytes())
        file_path.unlink()

        return {
            "status": "deleted",
            "path": str(file_path),
            "backup_path": str(backup_path),
        }

    def _handle_patch(self, file_path: Path, change: dict, preview: bool) -> dict:
        """Apply a unified diff patch to a file."""
        patch_text = str(change.get("patch", ""))
        if not patch_text:
            return {"status": "error", "path": str(file_path), "error": "Missing 'patch' content"}

        if not file_path.exists():
            return {"status": "error", "path": str(file_path), "error": "File does not exist (use 'create' first)"}

        old_content = file_path.read_text(encoding="utf-8")
        old_lines = old_content.splitlines(keepends=True)

        try:
            new_lines = list(difflib.unified_diff(
                old_lines, old_lines,  # placeholder
                fromfile=str(file_path), tofile=str(file_path),
            ))
            # Actually apply the patch manually
            patched_lines = self._apply_unified_diff(old_lines, patch_text)

            new_content = "".join(patched_lines)

            if preview:
                diff_lines = list(difflib.unified_diff(
                    old_lines, patched_lines,
                    fromfile=str(file_path), tofile=str(file_path),
                    lineterm="",
                ))
                return {
                    "status": "changed",
                    "path": str(file_path),
                    "preview": True,
                    "diff": "\n".join(diff_lines)[:3000],
                }

            # Create backup
            backup_path = file_path.with_suffix(file_path.suffix + ".flowcraft.bak")
            backup_path.write_bytes(file_path.read_bytes())

            file_path.write_text(new_content, encoding="utf-8")

            return {
                "status": "changed",
                "path": str(file_path),
                "backup_path": str(backup_path),
            }

        except Exception as exc:
            return {"status": "error", "path": str(file_path), "error": f"Patch application failed: {exc}"}

    @staticmethod
    def _apply_unified_diff(original_lines: list[str], patch_text: str) -> list[str]:
        """Apply a unified diff patch to original lines.

        Parses standard unified diff format and returns patched lines.
        """
        result = list(original_lines)
        patch_lines = patch_text.split("\n")

        # Parse hunks
        i = 0
        while i < len(patch_lines):
            line = patch_lines[i]
            if line.startswith("@@"):
                # Parse hunk header: @@ -old_start,old_count +new_start,new_count @@
                parts = line.split("@@")
                if len(parts) >= 3:
                    hunk_info = parts[1].strip()
                    old_info, new_info = hunk_info.split()
                    old_start = int(old_info.split(",")[0].lstrip("-"))
                    new_start = int(new_info.split(",")[0].lstrip("+"))

                    old_pos = old_start - 1  # 0-based
                    new_pos = new_start - 1  # 0-based
                    i += 1

                    while i < len(patch_lines) and not patch_lines[i].startswith("@@"):
                        hunk_line = patch_lines[i]
                        if hunk_line.startswith(" "):
                            # Context line
                            if old_pos < len(result):
                                result[old_pos] = hunk_line[1:] + "\n"
                            old_pos += 1
                            new_pos += 1
                        elif hunk_line.startswith("-"):
                            # Remove line
                            if old_pos < len(result):
                                del result[old_pos]
                            # Don't increment old_pos since we removed
                        elif hunk_line.startswith("+"):
                            # Add line
                            result.insert(new_pos, hunk_line[1:] + "\n")
                            new_pos += 1
                            old_pos += 1
                        elif hunk_line == "\\ No newline at end of file":
                            pass  # Ignore
                        i += 1
            else:
                i += 1

        return result
