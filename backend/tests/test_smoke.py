"""
Smoke tests: the app imports, boots, and the basic routes respond.

Run from the `backend/` directory:
    pytest
"""
from fastapi.testclient import TestClient

from app.main import app


def test_app_imports():
    assert app is not None
    assert app.title == "C-HAWQ API"


def test_health_ok():
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


def test_ready_ok():
    with TestClient(app) as client:
        resp = client.get("/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["app"] == "chawq-api"


def test_agents_run_queues_a_run():
    with TestClient(app) as client:
        resp = client.post(
            "/agents/run",
            json={
                "agent": "email_drafter",
                "contact_id": "test-contact-1",
                "payload": {"tone": "simmer"},
            },
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["agent"] == "email_drafter"
        assert body["contact_id"] == "test-contact-1"
        assert body["status"] == "queued"
        assert body["run_id"]


def test_webhooks_return_202():
    with TestClient(app) as client:
        assert client.post("/webhooks/drive").status_code == 202
        assert client.post("/webhooks/ghl", json={}).status_code == 202
