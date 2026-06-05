from fastapi.testclient import TestClient

from flowcraft_core.api.server import create_app


def test_health():
    with TestClient(create_app()) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


def test_create_task_records_timeline():
    with TestClient(create_app()) as client:
        response = client.post("/api/tasks", json={"session_id": "s1", "input": "解释 FlowCraft 是什么"})
        assert response.status_code == 200
        task_id = response.json()["task_id"]
        events = client.get(f"/api/tasks/{task_id}/events").json()["events"]
        event_types = [event["event_type"] for event in events]
        assert "task.created" in event_types
        assert "intent.recognized" in event_types
        assert "plan.created" in event_types

