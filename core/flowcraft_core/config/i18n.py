"""Internationalization (i18n) framework.

Simple key-value translation with fallback chains.
No external dependencies - pure Python dictionary-based.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Default translations (Chinese + English)
DEFAULT_LOCALE = "zh-CN"

TRANSLATIONS: dict[str, dict[str, str]] = {
    "zh-CN": {
        "app.name": "FlowCraft",
        "app.tagline": "Harness-first 本地 Agent 工作流平台",
        "task.created": "任务已创建",
        "task.completed": "任务已完成",
        "task.failed": "任务执行失败",
        "task.cancelled": "任务已取消",
        "task.executing": "任务执行中",
        "task.paused": "任务已暂停",
        "task.waiting_approval": "等待审批",
        "step.start": "开始执行步骤",
        "step.complete": "步骤完成",
        "step.failed": "步骤失败",
        "step.retry": "重试步骤",
        "tool.requested": "请求工具",
        "tool.completed": "工具执行完成",
        "tool.failed": "工具执行失败",
        "approval.requested": "需要审批",
        "approval.approved": "已批准",
        "approval.rejected": "已拒绝",
        "model.error": "模型服务异常",
        "model.retry": "模型重试",
        "error.unknown": "未知错误",
        "error.permission": "权限不足",
        "error.timeout": "操作超时",
        "error.network": "网络连接失败",
        "ui.send": "发送",
        "ui.run": "运行",
        "ui.pause": "暂停",
        "ui.resume": "继续",
        "ui.cancel": "取消",
        "ui.approve": "批准",
        "ui.reject": "拒绝",
        "ui.settings": "设置",
        "ui.report": "报告",
        "ui.export": "导出",
        "ui.import": "导入",
        "ui.search": "搜索",
        "ui.agent_working": "Agent 工作中...",
        "ui.task_paused": "任务已暂停",
        "ui.no_tasks": "暂无任务",
        "status.COMPLETED": "完成",
        "status.FAILED": "失败",
        "status.CANCELLED": "已取消",
        "status.EXECUTING": "执行中",
        "status.PAUSED": "已暂停",
        "status.WAITING_APPROVAL": "待审批",
        "status.PLANNED": "已规划",
    },
    "en": {
        "app.name": "FlowCraft",
        "app.tagline": "Harness-first Local Agent Workflow Platform",
        "task.created": "Task Created",
        "task.completed": "Task Completed",
        "task.failed": "Task Failed",
        "task.cancelled": "Task Cancelled",
        "task.executing": "Executing",
        "task.paused": "Paused",
        "task.waiting_approval": "Waiting Approval",
        "step.start": "Starting Step",
        "step.complete": "Step Complete",
        "step.failed": "Step Failed",
        "step.retry": "Retrying Step",
        "tool.requested": "Tool Requested",
        "tool.completed": "Tool Completed",
        "tool.failed": "Tool Failed",
        "approval.requested": "Approval Required",
        "approval.approved": "Approved",
        "approval.rejected": "Rejected",
        "model.error": "Model Service Error",
        "model.retry": "Model Retry",
        "error.unknown": "Unknown Error",
        "error.permission": "Permission Denied",
        "error.timeout": "Operation Timeout",
        "error.network": "Network Error",
        "ui.send": "Send",
        "ui.run": "Run",
        "ui.pause": "Pause",
        "ui.resume": "Resume",
        "ui.cancel": "Cancel",
        "ui.approve": "Approve",
        "ui.reject": "Reject",
        "ui.settings": "Settings",
        "ui.report": "Report",
        "ui.export": "Export",
        "ui.import": "Import",
        "ui.search": "Search",
        "ui.agent_working": "Agent working...",
        "ui.task_paused": "Task Paused",
        "ui.no_tasks": "No tasks",
        "status.COMPLETED": "Completed",
        "status.FAILED": "Failed",
        "status.CANCELLED": "Cancelled",
        "status.EXECUTING": "Executing",
        "status.PAUSED": "Paused",
        "status.WAITING_APPROVAL": "Waiting Approval",
        "status.PLANNED": "Planned",
    },
    "ja": {
        "app.name": "FlowCraft",
        "app.tagline": "Harness-first ローカルエージェントワークフロープラットフォーム",
        "task.created": "タスク作成",
        "task.completed": "タスク完了",
        "task.failed": "タスク失敗",
        "task.cancelled": "タスクキャンセル",
        "task.executing": "実行中",
        "task.paused": "一時停止",
        "task.waiting_approval": "承認待ち",
        "step.start": "ステップ開始",
        "step.complete": "ステップ完了",
        "step.failed": "ステップ失敗",
        "step.retry": "ステップ再試行",
        "tool.requested": "ツール要求",
        "tool.completed": "ツール完了",
        "tool.failed": "ツール失敗",
        "approval.requested": "承認が必要",
        "approval.approved": "承認済み",
        "approval.rejected": "拒否",
        "model.error": "モデルサービスエラー",
        "model.retry": "モデル再試行",
        "error.unknown": "不明なエラー",
        "error.permission": "権限不足",
        "error.timeout": "タイムアウト",
        "error.network": "ネットワークエラー",
        "ui.send": "送信",
        "ui.run": "実行",
        "ui.pause": "一時停止",
        "ui.resume": "再開",
        "ui.cancel": "キャンセル",
        "ui.approve": "承認",
        "ui.reject": "拒否",
        "ui.settings": "設定",
        "ui.report": "レポート",
        "ui.export": "エクスポート",
        "ui.import": "インポート",
        "ui.search": "検索",
        "ui.agent_working": "エージェント作業中...",
        "ui.task_paused": "タスク一時停止中",
        "ui.no_tasks": "タスクなし",
        "status.COMPLETED": "完了",
        "status.FAILED": "失敗",
        "status.CANCELLED": "キャンセル",
        "status.EXECUTING": "実行中",
        "status.PAUSED": "一時停止",
        "status.WAITING_APPROVAL": "承認待ち",
        "status.PLANNED": "計画済み",
    },
}


class I18n:
    """Internationalization manager with locale fallback."""

    def __init__(self, locale: str = DEFAULT_LOCALE) -> None:
        self.locale = locale
        self._translations: dict[str, dict] = dict(TRANSLATIONS)
        self._load_custom()

    def _load_custom(self) -> None:
        """Load custom translations from disk."""
        custom_path = Path("i18n_custom.json")
        if custom_path.exists():
            try:
                custom = json.loads(custom_path.read_text(encoding="utf-8"))
                for lang, trans in custom.items():
                    if lang in self._translations:
                        self._translations[lang].update(trans)
                    else:
                        self._translations[lang] = trans
            except Exception:
                pass

    def t(self, key: str, fallback: str = "", **kwargs) -> str:
        """Translate a key to the current locale."""
        result = self._translations.get(self.locale, {}).get(
            key,
            self._translations.get("en", {}).get(key, fallback or key),
        )
        if kwargs:
            try:
                result = result.format(**kwargs)
            except (KeyError, ValueError):
                pass
        return result

    def set_locale(self, locale: str) -> None:
        if locale in self._translations:
            self.locale = locale

    def available_locales(self) -> list[str]:
        return list(self._translations.keys())

    def add_translations(self, locale: str, translations: dict[str, str]) -> None:
        if locale not in self._translations:
            self._translations[locale] = {}
        self._translations[locale].update(translations)


# Global instance
i18n = I18n()
t = i18n.t

