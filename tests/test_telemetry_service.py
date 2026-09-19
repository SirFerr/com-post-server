import importlib

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.models import OutboxEvent, Telemetry
from tests.conftest import token
from tools import telemetry_worker
from tools.worker_heartbeat import write_heartbeat


def test_telemetry_events_are_idempotent_and_private(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEMETRY_DATABASE_URL", f"sqlite:///{tmp_path / 'telemetry.db'}")
    monkeypatch.setenv("TELEMETRY_INTERNAL_TOKEN", "test-internal-secret")
    import telemetry_service

    service = importlib.reload(telemetry_service)
    headers = {"X-Internal-Token": "test-internal-secret"}
    event = {
        "id": "event-1",
        "kind": "telemetry.recorded",
        "composter_id": "composter-1",
        "battery_level": 90,
        "fill_level": 20,
        "lock_state": "CLOSED",
    }
    with TestClient(service.app) as client:
        assert client.post("/v1/events", json=event).status_code == 403
        assert client.post("/v1/events", json=event, headers=headers).json()["status"] == "accepted"
        assert client.post("/v1/events", json=event, headers=headers).json()["status"] == "duplicate"
        rows = client.get("/v1/composters/composter-1/readings", headers=headers).json()
        assert len(rows) == 1
        assert rows[0]["battery_level"] == 90
        assert client.post("/v1/events", json={"id": "event-2", "kind": "composter.deleted", "composter_id": "composter-1"}, headers=headers).status_code == 200
        assert client.get("/v1/composters/composter-1/readings", headers=headers).json() == []


def test_telemetry_write_queues_history_in_same_transaction(client):
    headers = {"Authorization": f"Bearer {token(client, 'engineer@example.com')}"}
    response = client.post("/admin/composters/composter-1/telemetry", json={
        "battery_level": 88,
        "fill_level": 42,
        "lock_state": "CLOSED",
    }, headers=headers)
    assert response.status_code == 200
    assert "compost_outbox_pending 1" in client.get("/metrics").text
    with SessionLocal() as db:
        rows = db.query(OutboxEvent).all()
        assert len(rows) == 1
        assert rows[0].kind == "telemetry.recorded"
        assert db.query(Telemetry).count() == 0


def test_worker_marks_event_after_successful_delivery(client, monkeypatch):
    headers = {"Authorization": f"Bearer {token(client, 'engineer@example.com')}"}
    assert client.post("/admin/composters/composter-1/telemetry", json={
        "battery_level": 88, "fill_level": 42, "lock_state": "CLOSED",
    }, headers=headers).status_code == 200
    sent = []

    class Response:
        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, json, headers):
            sent.append(json)
            return Response()

    monkeypatch.setenv("TELEMETRY_SERVICE_URL", "http://telemetry-service:8004")
    monkeypatch.setenv("TELEMETRY_INTERNAL_TOKEN", "test-internal-secret")
    monkeypatch.setattr(telemetry_worker.httpx, "Client", FakeClient)
    assert telemetry_worker.deliver_once() == 1
    assert telemetry_worker.deliver_once() == 0
    assert len(sent) == 1
    with SessionLocal() as db:
        assert db.query(OutboxEvent).one().delivered_at is not None


def test_worker_heartbeat_is_visible_to_monitoring(client):
    write_heartbeat("telemetry-worker")
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert 'compost_worker_last_seen_timestamp_seconds{name="telemetry-worker"} 0' not in metrics.text
