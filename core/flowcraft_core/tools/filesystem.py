"""Extended filesystem tools — list, search, delete, metadata.

Security:
    All path operations are validated against allowed_paths.
    Destructive operations (delete) require explicit approval.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flowcraft_core.domain.enums import RiskLevel
from flowcraft_core.domain.schemas import ToolIntent
from flowcraft_core.tools.base import Tool, ToolDefinition, is_path_allowed, observation_from_output


class FileListTool(Tool):
    """List directory contents with optional glob/wildcard filtering."""

    def __init__(self, allowed_paths: list[Path]) -> None:
        self.allowed_paths = allowed_paths
        self.definition = ToolDefinition(
            tool_name="file.list",
            display_name="列出目录",
            description=(
                "列出目录内文件和子目录。参数: path(路径), pattern(过滤如*.py), "
                "recursive(递归), max_items(最大返回数,默认100)"
            ),
            category="file",
            risk_level=RiskLevel.LOW,
            permissions=["tool:file.read"],
            timeout_seconds=15,
        )

    async def execute(self, intent: ToolIntent):
        path_str = str(intent.input_payload.get("path", "."))
        pattern = str(intent.input_payload.get("pattern", "*"))
        recursive = bool(intent.input_payload.get("recursive", False))
        max_items = int(intent.input_payload.get("max_items", 100))

        path = Path(path_str)
        if not path.is_absolute():
            path = self.allowed_paths[0] / path

        if not is_path_allowed(path, self.allowed_paths):
            return observation_from_output(intent, "DENIED",
                f"No permission for {path}", error="Path not allowed.",
                payload={"action": "ask_user_for_permission"})

        if not path.exists():
            return observation_from_output(intent, "FAILED", f"Not found: {path}")
        if not path.is_dir():
            return observation_from_output(intent, "FAILED", f"Not a directory: {path}")

        try:
            items: list[dict[str, Any]] = []
            iterator = path.rglob(pattern) if recursive else path.glob(pattern)
            for child in iterator:
                if len(items) >= max_items:
                    break
                try:
                    st = child.stat()
                    items.append({
                        "name": child.name, "path": str(child),
                        "relative": str(child.relative_to(path)),
                        "type": "dir" if child.is_dir() else "file",
                        "size": st.st_size if child.is_file() else 0,
                        "modified_at": datetime.fromtimestamp(
                            st.st_mtime, tz=timezone.utc).isoformat(),
                    })
                except OSError:
                    pass
            items.sort(key=lambda i: (0 if i["type"] == "dir" else 1, i["name"].lower()))
            return observation_from_output(intent, "COMPLETED",
                f"Found {len(items)} items in {path}",
                {"path": str(path), "pattern": pattern, "items": items, "count": len(items)})
        except Exception as exc:
            return observation_from_output(intent, "FAILED", str(exc))


class FileSearchTool(Tool):
    """Search file contents for keywords (grep-like)."""

    TEXT_EXTENSIONS = {
        ".txt", ".md", ".py", ".js", ".ts", ".jsx", ".tsx",
        ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
        ".html", ".css", ".xml", ".csv", ".log", ".sql",
        ".rs", ".go", ".java", ".c", ".cpp", ".h", ".hpp",
        ".sh", ".bat", ".ps1", ".rb", ".php",
    }

    def __init__(self, allowed_paths: list[Path]) -> None:
        self.allowed_paths = allowed_paths
        self.definition = ToolDefinition(
            tool_name="file.search",
            display_name="搜索文件内容",
            description=(
                "在文件中搜索关键词(grep)。参数: path(目录), query(搜索词), "
                "file_pattern(过滤,如*.py), recursive, max_results, context_lines"
            ),
            category="file",
            risk_level=RiskLevel.LOW,
            permissions=["tool:file.read"],
            timeout_seconds=30,
        )

    async def execute(self, intent: ToolIntent):
        path_str = str(intent.input_payload.get("path", "."))
        query = str(intent.input_payload.get("query", ""))
        file_pattern = str(intent.input_payload.get("file_pattern", "*"))
        recursive = bool(intent.input_payload.get("recursive", True))
        max_results = int(intent.input_payload.get("max_results", 20))
        context_lines = int(intent.input_payload.get("context_lines", 0))

        if not query:
            return observation_from_output(intent, "FAILED", "Missing query")

        path = Path(path_str)
        if not path.is_absolute():
            path = self.allowed_paths[0] / path

        if not is_path_allowed(path, self.allowed_paths):
            return observation_from_output(intent, "DENIED", "Path not allowed.",
                payload={"action": "ask_user_for_permission"})

        if not path.exists():
            return observation_from_output(intent, "FAILED", f"Not found: {path}")

        search_root = path if path.is_dir() else path.parent
        results: list[dict[str, Any]] = []
        query_lower = query.lower()
        files_scanned = 0

        try:
            iterator = search_root.rglob(file_pattern) if recursive else search_root.glob(file_pattern)
            for file_path in iterator:
                if len(results) >= max_results:
                    break
                if not file_path.is_file():
                    continue
                if file_path.suffix.lower() not in self.TEXT_EXTENSIONS:
                    continue
                try:
                    if file_path.stat().st_size > 5 * 1024 * 1024:
                        continue
                except OSError:
                    continue
                files_scanned += 1
                try:
                    content = file_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                if query_lower not in content.lower():
                    continue

                lines = content.split("\n")
                for line_num, line in enumerate(lines, 1):
                    if query_lower in line.lower():
                        if len(results) >= max_results:
                            break
                        ctx_start = max(0, line_num - context_lines - 1)
                        ctx_end = min(len(lines), line_num + context_lines)
                        ctx_lines = []
                        for ci in range(ctx_start, ctx_end):
                            marker = ">>>" if ci == line_num - 1 else "   "
                            cl = lines[ci]
                            if len(cl) > 200:
                                cl = cl[:200] + "..."
                            ctx_lines.append(f"{marker} {ci + 1}: {cl}")

                        results.append({
                            "file": str(file_path),
                            "relative": str(file_path.relative_to(search_root)),
                            "line": line_num,
                            "context": "\n".join(ctx_lines),
                            "match_count": content.lower().count(query_lower),
                        })

            if not results:
                return observation_from_output(intent, "COMPLETED",
                    f"No matches for '{query}' in {files_scanned} files",
                    {"query": query, "files_scanned": files_scanned, "results": []})

            return observation_from_output(intent, "COMPLETED",
                f"Found {len(results)} matches for '{query}' in {files_scanned} files",
                {"query": query, "files_scanned": files_scanned,
                 "results": results, "total_matches": len(results)})
        except Exception as exc:
            return observation_from_output(intent, "FAILED", str(exc))


class FileDeleteTool(Tool):
    """Delete files with mandatory approval and automatic backup."""

    def __init__(self, allowed_paths: list[Path], backup_dir: Path | None = None) -> None:
        self.allowed_paths = allowed_paths
        self.backup_dir = backup_dir
        self.definition = ToolDefinition(
            tool_name="file.delete",
            display_name="删除文件",
            description="删除授权目录内的文件。删除前自动创建备份。",
            category="file",
            risk_level=RiskLevel.HIGH,
            permissions=["tool:file.delete"],
            requires_approval_by_default=True,
            supports_rollback=True,
            timeout_seconds=30,
        )

    async def execute(self, intent: ToolIntent):
        path_str = str(intent.input_payload.get("path", ""))
        if not path_str:
            return observation_from_output(intent, "FAILED", "Missing path")

        path = Path(path_str)
        if not path.is_absolute():
            path = self.allowed_paths[0] / path

        if not is_path_allowed(path, self.allowed_paths):
            return observation_from_output(intent, "DENIED", "Path not allowed.",
                payload={"action": "ask_user_for_permission"})

        if not path.exists():
            return observation_from_output(intent, "FAILED", "File not found")
        if path.is_dir():
            return observation_from_output(intent, "FAILED", "Is a directory, not file")

        try:
            backup_dir = self.backup_dir or path.parent
            backup_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            backup_path = backup_dir / f"{path.name}.{ts}.bak"
            shutil.copy2(path, backup_path)
            file_size = path.stat().st_size
            path.unlink()

            return observation_from_output(intent, "COMPLETED",
                f"Deleted: {path.name} (backup: {backup_path})",
                {"deleted": str(path), "backup": str(backup_path),
                 "size": file_size, "can_restore": True})
        except Exception as exc:
            return observation_from_output(intent, "FAILED", str(exc))


class FileMetaTool(Tool):
    """Read file/directory metadata."""

    def __init__(self, allowed_paths: list[Path]) -> None:
        self.allowed_paths = allowed_paths
        self.definition = ToolDefinition(
            tool_name="file.meta",
            display_name="文件信息",
            description="读取文件/目录元信息（大小、修改时间、类型等）。",
            category="file",
            risk_level=RiskLevel.LOW,
            permissions=["tool:file.read"],
            timeout_seconds=10,
        )

    async def execute(self, intent: ToolIntent):
        path_str = str(intent.input_payload.get("path", ""))
        if not path_str:
            return observation_from_output(intent, "FAILED", "Missing path")

        path = Path(path_str)
        if not path.is_absolute():
            path = self.allowed_paths[0] / path

        if not is_path_allowed(path, self.allowed_paths):
            return observation_from_output(intent, "DENIED", "Path not allowed.",
                payload={"action": "ask_user_for_permission"})

        if not path.exists():
            return observation_from_output(intent, "FAILED", f"Not found: {path}")

        try:
            st = path.stat()
            size = st.st_size
            for unit in ("B", "KB", "MB", "GB"):
                if size < 1024:
                    size_h = f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
                    break
                size /= 1024
            else:
                size_h = f"{size:.1f} TB"

            meta = {
                "path": str(path), "name": path.name,
                "type": "dir" if path.is_dir() else "file",
                "size": st.st_size, "size_human": size_h,
                "modified_at": datetime.fromtimestamp(
                    st.st_mtime, tz=timezone.utc).isoformat(),
                "created_at": datetime.fromtimestamp(
                    st.st_ctime, tz=timezone.utc).isoformat(),
                "suffix": path.suffix,
            }
            if path.is_dir():
                try:
                    meta["children_count"] = len(list(path.iterdir()))
                except PermissionError:
                    meta["children_count"] = -1

            return observation_from_output(intent, "COMPLETED",
                f"{meta['type']}: {path.name} ({meta['size_human']})", meta)
        except Exception as exc:
            return observation_from_output(intent, "FAILED", str(exc))
