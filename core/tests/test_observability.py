"""P0: Observability Tests — EventRecorder, SSE queues, trace events."""

from __future__ import annotations

import json
import queue
from datetime import datetime, timezone

import pytest

from flowcraft_core.observability.events import (
    EventRecorder, get_sse_queue, remove_sse_queue, sse_listener_factory,
)
from flowcraft_core.domain.schemas import TraceEvent


def make_event(task_id: str = "task_ev1", event_type: str = "task.created",
               title: str = "Created", message: str = "Task created",
               payload: dict | None = None) -> TraceEvent:
    return TraceEvent(
        task_id=task_id,
        session_id="sess_1",
        event_type=event_type,
        title=title,
        message=message,
        payload=payload or {},
        severity="INFO",
    )


class TestEventRecorder:
    """TC-H2: Event recording, listing, and streaming."""

    @pytest.mark.unit
    def test_record_persists_to_db(self, tmp_database) -> None:
        """record() writes event to database."""
        rec = EventRecorder(tmp_database)
        evt = make_event()
        rec.record(evt)
        events = rec.list_for_task("task_ev1")
        assert len(events) == 1
        assert events[0]["event_type"] == "task.created"
        assert events[0]["title"] == "Created"

    @pytest.mark.unit
    def test_list_for_task_returns_chronological(self, tmp_database) -> None:
        """Events are returned in created_at ascending order."""
        rec = EventRecorder(tmp_database)
        rec.record(make_event("t1", "task.created", "First"))
        rec.record(make_event("t1", "intent.recognized", "Second"))
        rec.record(make_event("t1", "task.completed", "Third"))
        events = rec.list_for_task("t1")
        assert [e["event_type"] for e in events] == [
            "task.created", "intent.recognized", "task.completed"]

    @pytest.mark.unit
    def test_list_for_task_isolation(self, tmp_database) -> None:
        """list_for_task only returns events for the given task."""
        rec = EventRecorder(tmp_database)
        rec.record(make_event("task_a", "task.created"))
        rec.record(make_event("task_b", "task.created"))
        assert len(rec.list_for_task("task_a")) == 1
        assert len(rec.list_for_task("task_b")) == 1

    @pytest.mark.unit
    def test_payload_is_deserialized(self, tmp_database) -> None:
        """Payload is stored as JSON and restored as dict."""
        rec = EventRecorder(tmp_database)
        rec.record(make_event("t1", "test.event", payload={"key": "value", "num": 42}))
        events = rec.list_for_task("t1")
        assert events[0]["payload"] == {"key": "value", "num": 42}

    @pytest.mark.unit
    def test_stream_listener_is_notified(self, tmp_database) -> None:
        """Subscribed listener receives events."""
        rec = EventRecorder(tmp_database)
        received = []

        def listener(evt: dict) -> None:
            received.append(evt)

        rec.subscribe(listener)
        evt = make_event()
        rec.record(evt)
        assert len(received) == 1
        assert received[0]["event_type"] == "task.created"

    @pytest.mark.unit
    def test_unsubscribe_stops_notifications(self, tmp_database) -> None:
        """Unsubscribed listener no longer receives events."""
        rec = EventRecorder(tmp_database)
        received = []

        def listener(evt: dict) -> None:
            received.append(evt)

        rec.subscribe(listener)
        rec.unsubscribe(listener)
        rec.record(make_event())
        assert len(received) == 0


class TestSSEQueues:
    """SSE queue management."""

    @pytest.mark.unit
    def test_get_sse_queue_creates_if_missing(self) -> None:
        q = get_sse_queue("new_task")
        assert isinstance(q, queue.Queue)
        remove_sse_queue("new_task")  # cleanup

    @pytest.mark.unit
    def test_remove_sse_queue(self) -> None:
        get_sse_queue("tmp_task")
        remove_sse_queue("tmp_task")
        q = get_sse_queue("tmp_task")  # should re-create
        assert q is not None
        remove_sse_queue("tmp_task")  # cleanup

    @pytest.mark.unit
    def test_sse_listener_pushes_to_queue(self) -> None:
        q = get_sse_queue("task_sse")
        listener = sse_listener_factory("task_sse")
        listener({"task_id": "task_sse", "event_type": "test.event"})
        # Queue should have one item
        assert q.qsize() == 1
        evt = q.get_nowait()
        assert evt["event_type"] == "test.event"
        remove_sse_queue("task_sse")  # cleanup

    @pytest.mark.unit
    def test_sse_listener_filters_by_task_id(self) -> None:
        """Listener only pushes events matching its task_id."""
        q = get_sse_queue("task_a")
        listener = sse_listener_factory("task_a")
        listener({"task_id": "task_b", "event_type": "other.event"})
        assert q.qsize() == 0  # filtered out
        listener({"task_id": "task_a", "event_type": "my.event"})
        assert q.qsize() == 1
        remove_sse_queue("task_a")  # cleanup
