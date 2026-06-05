from __future__ import annotations

import json
import logging
import queue
from typing import Callable

from flowcraft_core.domain.schemas import TraceEvent
from flowcraft_core.storage.database import Database

logger = logging.getLogger(__name__)

# Type for stream listeners: callable that receives event dict
StreamListener = Callable[[dict], None]


class EventRecorder:
    """事件记录器，支持持久化存储和实时流式推送。

    通过 subscribe() 注册监听器，record() 时同时写入 DB 和通知监听器。
    """

    def __init__(self, db: Database) -> None:
        self.db = db
        self._listeners: list[StreamListener] = []

    def subscribe(self, listener: StreamListener) -> None:
        """注册流式事件监听器."""
        self._listeners.append(listener)

    def unsubscribe(self, listener: StreamListener) -> None:
        """移除监听器."""
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    def record(self, event: TraceEvent) -> TraceEvent:
        """记录事件：持久化 + 通知所有流式监听器."""
        # Persist to DB
        self.db.insert_json(
            "trace_events",
            {
                "id": event.event_id,
                "task_id": event.task_id,
                "session_id": event.session_id,
                "event_type": event.event_type,
                "title": event.title,
                "message": event.message,
                "payload_json": event.payload,
                "severity": event.severity,
                "created_at": event.created_at.isoformat(),
            },
        )

        # Notify stream listeners (non-blocking, best-effort)
        event_dict = self._event_to_dict(event)
        for listener in self._listeners:
            try:
                listener(event_dict)
            except Exception as exc:
                logger.debug("Stream listener error: %s", exc)

        return event

    def list_for_task(self, task_id: str) -> list[dict]:
        rows = self.db.fetch_all(
            "SELECT * FROM trace_events WHERE task_id = ? ORDER BY created_at ASC",
            (task_id,),
        )
        return [self._row_to_event(row) for row in rows]

    @staticmethod
    def _row_to_event(row) -> dict:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        return item

    @staticmethod
    def _event_to_dict(event: TraceEvent) -> dict:
        """将 TraceEvent 转为可 JSON 序列化的 dict，用于流式推送."""
        return {
            "event_id": event.event_id,
            "task_id": event.task_id,
            "session_id": event.session_id,
            "event_type": event.event_type,
            "title": event.title,
            "message": event.message,
            "payload": event.payload,
            "severity": event.severity,
            "created_at": event.created_at.isoformat(),
        }


# Global SSE queues per task_id: task_id -> queue.Queue[dict]
_sse_queues: dict[str, queue.Queue] = {}


def get_sse_queue(task_id: str) -> queue.Queue:
    """Get or create an SSE event queue for a task."""
    if task_id not in _sse_queues:
        _sse_queues[task_id] = queue.Queue()
    return _sse_queues[task_id]


def remove_sse_queue(task_id: str) -> None:
    """Clean up SSE queue when task is done."""
    _sse_queues.pop(task_id, None)


def sse_listener_factory(task_id: str) -> StreamListener:
    """Create a listener that pushes events to the SSE queue for a task."""
    q = get_sse_queue(task_id)
    def _listener(event: dict) -> None:
        if event.get("task_id") == task_id:
            q.put(event)
    return _listener

