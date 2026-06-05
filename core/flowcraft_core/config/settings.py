from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class Settings:
    data_dir: Path
    database_path: Path
    allowed_paths: list[Path]
    app_name: str = "FlowCraft"
    version: str = "0.1.0"
    host: str = "127.0.0.1"
    port: int = 8765

    # Derived paths (set by ensure_directories)
    config_dir: Path = field(init=False)
    logs_dir: Path = field(init=False)
    artifacts_dir: Path = field(init=False)
    backups_dir: Path = field(init=False)
    plugins_dir: Path = field(init=False)
    workflows_dir: Path = field(init=False)
    knowledge_dir: Path = field(init=False)
    temp_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        self.config_dir = self.data_dir / "config"
        self.logs_dir = self.data_dir / "logs"
        self.artifacts_dir = self.data_dir / "artifacts"
        self.backups_dir = self.data_dir / "backups" / "file-operations"
        self.plugins_dir = self.data_dir / "plugins"
        self.workflows_dir = self.data_dir / "workflows"
        self.knowledge_dir = self.data_dir / "knowledge"
        self.temp_dir = self.data_dir / "temp"

    def ensure_directories(self) -> None:
        """创建所有必要的本地目录结构。

        目录结构：
            FlowCraft/
            ├── config/          # 应用配置
            ├── data/            # SQLite 数据库
            ├── logs/            # 日志文件
            ├── artifacts/       # 任务产物
            │   └── tasks/       # 按 task_id 组织
            ├── backups/
            │   └── file-operations/  # 文件操作前备份
            ├── plugins/
            │   ├── installed/
            │   └── cache/
            ├── workflows/
            │   ├── installed/
            │   └── created/
            ├── knowledge/
            │   ├── sources/
            │   └── indexes/
            └── temp/
        """
        dirs = [
            self.config_dir,
            self.database_path.parent,
            self.logs_dir,
            self.artifacts_dir / "tasks",
            self.backups_dir,
            self.plugins_dir / "installed",
            self.plugins_dir / "cache",
            self.workflows_dir / "installed",
            self.workflows_dir / "created",
            self.knowledge_dir / "sources",
            self.knowledge_dir / "indexes",
            self.temp_dir,
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
        logger.info("Directory structure ensured under %s", self.data_dir)

    def task_artifacts_dir(self, task_id: str) -> Path:
        """获取特定任务的产物目录。"""
        p = self.artifacts_dir / "tasks" / task_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def task_outputs_dir(self, task_id: str) -> Path:
        p = self.task_artifacts_dir(task_id) / "outputs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def task_tool_results_dir(self, task_id: str) -> Path:
        p = self.task_artifacts_dir(task_id) / "tool-results"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def add_allowed_path(self, path: str | Path) -> bool:
        """Dynamically add a path to the allowed list at runtime.

        Returns True if added, False if already present.
        The tools reference the same list object, so they see the update immediately.
        """
        p = Path(path).resolve()
        existing = [ap.resolve() for ap in self.allowed_paths]
        if p in existing:
            return False
        self.allowed_paths.append(p)
        return True


def default_data_dir() -> Path:
    if os.name == "nt":
        root = os.environ.get("APPDATA")
        if root:
            return Path(root) / "FlowCraft"
    return Path.home() / ".local" / "share" / "flowcraft"


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("FLOWCRAFT_DATA_DIR", default_data_dir()))
    workspace = Path(os.environ.get("FLOWCRAFT_WORKSPACE", Path.cwd()))
    settings = Settings(
        data_dir=data_dir,
        database_path=data_dir / "data" / "flowcraft.db",
        allowed_paths=[workspace.resolve()],
    )
    return settings
