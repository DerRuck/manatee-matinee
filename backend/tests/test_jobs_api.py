"""HTTP tests for /jobs.

The sweep itself is exercised in tests/test_scoring_sweep.py — these tests
only check that the route queues the background task, returns 202 fast,
and reads the audit doc back.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app


def test_trigger_daily_scoring_returns_202_and_queues_background():
    with patch("app.routes.jobs._run_sweep_in_background") as runner:
        with TestClient(app) as client:
            resp = client.post(
                "/jobs/scoring/daily",
                json={"max_contacts": 5, "dry_run": True},
            )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["sweep_id"]
    assert body["dry_run"] is True
    # BackgroundTasks run after the response — TestClient awaits them on context exit
    runner.assert_called_once()
    kwargs = runner.call_args.kwargs
    assert kwargs["sweep_id"] == body["sweep_id"]
    assert kwargs["max_contacts"] == 5
    assert kwargs["dry_run"] is True


def test_trigger_daily_scoring_uses_defaults_with_no_body():
    with patch("app.routes.jobs._run_sweep_in_background") as runner:
        with TestClient(app) as client:
            resp = client.post("/jobs/scoring/daily")
    assert resp.status_code == 202
    kwargs = runner.call_args.kwargs
    assert kwargs["max_contacts"] == 100
    assert kwargs["min_age_hours"] == 18
    assert kwargs["triggered_by"] == "daily"
    assert kwargs["dry_run"] is False


def test_trigger_daily_scoring_rejects_bad_max_contacts():
    with TestClient(app) as client:
        resp = client.post("/jobs/scoring/daily", json={"max_contacts": 100000})
    assert resp.status_code == 422


def test_get_sweep_returns_doc_when_present():
    payload = {"sweep_id": "sweep-1", "status": "completed", "total_scored": 7}
    with patch("services.scoring_agent.sweep.get_sweep_doc", return_value=payload):
        with TestClient(app) as client:
            resp = client.get("/jobs/scoring/sweeps/sweep-1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["sweep_id"] == "sweep-1"
    assert body["status"] == "completed"
    assert body["total_scored"] == 7


def test_get_sweep_404_when_missing():
    with patch("services.scoring_agent.sweep.get_sweep_doc", return_value=None):
        with TestClient(app) as client:
            resp = client.get("/jobs/scoring/sweeps/missing")
    assert resp.status_code == 404
