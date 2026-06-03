"""
Smoke tests: the app imports, boots, and the basic routes respond.

Run from the `backend/` directory:
    pytest

Tests that touch /agents/* mock Firestore and the dispatcher registry so
they don't reach out to GCP from the CI container (which has no Firestore
credentials).

Shared-secret auth is disabled for all tests in this module via an autouse
fixture below. Auth correctness is verified manually + via the unset-warning
log path; these tests focus on route behavior, not the auth check itself.
This also keeps tests env-agnostic: they pass whether or not the developer
has CHAWQ_SHARED_SECRET / GHL_WEBHOOK_SECRET set in their local .env.
"""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(autouse=True)
def _disable_shared_secret_checks(monkeypatch):
    """Bypass both shared-secret middleware for the duration of each test."""
    monkeypatch.setattr(
        "app.routes.agents._verify_chawq_shared_secret",
        lambda provided: None,
    )
    monkeypatch.setattr(
        "app.routes.webhooks._verify_ghl_shared_secret",
        lambda provided: None,
    )


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


# --- /agents/run ------------------------------------------------------------
#
# Mocks:
#   - put_agent_run         (pending stub write — would hit Firestore)
#   - AGENT_DISPATCH entry  (BackgroundTask would call the real runner —
#                            Anthropic + Gmail + Drive + Firestore)


@patch.dict(
    "app.routes.agents.AGENT_DISPATCH",
    {"email_drafter": lambda run_id, inputs: None},
    clear=False,
)
@patch("app.routes.agents.put_agent_run")
def test_agents_run_queues_a_run(mock_put):
    with TestClient(app) as client:
        resp = client.post(
            "/agents/run",
            json={
                "agent": "email_drafter",
                "inputs": {
                    "contact_id": "test-contact-1",
                    "contact_email": "test@chawq.org",
                },
            },
        )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "pending"
    assert body["run_id"]

    # The pending stub should have been written with the locked shape.
    mock_put.assert_called_once()
    run_id_arg, record = mock_put.call_args.args
    assert run_id_arg == body["run_id"]
    assert record["agent"] == "email_drafter"
    assert record["status"] == "pending"
    assert record["triggered_by"] == "workbook"
    assert record["contact_id"] == "test-contact-1"
    assert record["inputs"]["contact_email"] == "test@chawq.org"


@patch.dict(
    "app.routes.agents.AGENT_DISPATCH",
    {"feedback": lambda run_id, inputs: None},
    clear=False,
)
@patch("app.routes.agents.put_agent_run")
def test_agents_run_accepts_feedback_agent(mock_put):
    with TestClient(app) as client:
        resp = client.post(
            "/agents/run",
            json={
                "agent": "feedback",
                "inputs": {
                    "run_id": "orig-deliverable-1",
                    "contact_id": "contact-9",
                    "reaction": "edits_requested",
                    "note": "too formal",
                },
            },
        )
    assert resp.status_code == 202
    assert resp.json()["status"] == "pending"
    mock_put.assert_called_once()
    _run_id, record = mock_put.call_args.args
    assert record["agent"] == "feedback"
    assert record["contact_id"] == "contact-9"


@patch("app.routes.agents.put_agent_run")
def test_agents_run_rejects_unknown_agent(mock_put):
    with TestClient(app) as client:
        resp = client.post(
            "/agents/run",
            json={"agent": "nonexistent_agent", "inputs": {}},
        )
    assert resp.status_code == 400
    # Should be rejected before any Firestore write.
    mock_put.assert_not_called()


# --- /agents/runs/{run_id} -------------------------------------------------


@patch("app.routes.agents.get_agent_run", return_value=None)
def test_agents_get_run_404_when_missing(_mock_get):
    with TestClient(app) as client:
        resp = client.get("/agents/runs/nonexistent-run-id")
    assert resp.status_code == 404


@patch(
    "app.routes.agents.get_agent_run",
    return_value={
        "run_id": "fake-uuid",
        "agent": "email_drafter",
        "status": "completed",
        "subject": "Test draft",
    },
)
def test_agents_get_run_returns_doc(_mock_get):
    with TestClient(app) as client:
        resp = client.get("/agents/runs/fake-uuid")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["subject"] == "Test draft"


def test_webhooks_return_202():
    with TestClient(app) as client:
        resp = client.post(
            "/webhooks/drive",
            headers={"X-Goog-Resource-State": "sync"},
        )
        assert resp.status_code == 202
        assert client.post("/webhooks/ghl", json={}).status_code == 202
