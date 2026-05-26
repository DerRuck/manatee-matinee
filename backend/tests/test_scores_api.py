"""HTTP tests for /scores.

The Firestore reader functions are patched so these don't need a real
Firestore client. Verifies the router translates query params into reader
args, paginates, and shapes the response correctly.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _row(contact_id: str, heat: str, score: int, step: int = 4,
         ready: bool = False, **extras) -> dict:
    base = {
        "contact_id":             contact_id,
        "municipality_name":      "Sample City",
        "score_type_id":          "PIPELINE-SCORE",
        "prompt_version":         1,
        "latest_run_id":          f"run_{contact_id}",
        "scored_at":              datetime.now(tz=timezone.utc),
        "triggered_by":           "daily",
        "current_step":           step,
        "current_step_name":      f"Step {step}: Sample",
        "current_phase":          1 if step <= 4 else 2 if step <= 6 else 3,
        "step_confidence":        0.8,
        "ready_to_advance":       ready,
        "lead_heat":              heat,
        "lead_heat_score":        score,
        "summary_one_line":       f"{heat} lead at step {step}",
        "blocker_count":          0,
        "recommended_action_count": 2,
        "days_since_last_signal": 3,
        "model":                  "claude-sonnet-4-6",
        "findings":               {"score_type": "PIPELINE-SCORE", "current_step": step,
                                   "signals": [{"description": "x", "evidence_source": "y",
                                                "impact": "positive", "weight": 0.5}]},
    }
    base.update(extras)
    return base


# ---------------------------------------------------------------------------
# /scores — list
# ---------------------------------------------------------------------------

def test_list_scores_default_sorts_by_heat_desc():
    rows = [
        _row("c_boil_1", "boil",   90),
        _row("c_boil_2", "boil",   75),
        _row("c_simmer", "simmer", 55),
    ]
    with patch("services.firestore.scores.list_contact_scores", return_value=rows):
        with TestClient(app) as client:
            resp = client.get("/scores")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 3
    assert [item["contact_id"] for item in body["items"]] == [
        "c_boil_1", "c_boil_2", "c_simmer",
    ]
    # next_cursor null because page wasn't full
    assert body["next_cursor"] is None


def test_list_scores_passes_filters_through():
    with patch("services.firestore.scores.list_contact_scores") as reader:
        reader.return_value = []
        with TestClient(app) as client:
            resp = client.get(
                "/scores",
                params=[
                    ("heat", "boil"), ("heat", "simmer"),
                    ("step", "4"),
                    ("ready_to_advance", "true"),
                    ("min_score", "70"),
                    ("limit", "25"),
                    ("cursor", "80"),
                ],
            )
    assert resp.status_code == 200
    reader.assert_called_once()
    kwargs = reader.call_args.kwargs
    assert kwargs["lead_heat"] == ["boil", "simmer"]
    assert kwargs["current_step"] == 4
    assert kwargs["ready_to_advance"] is True
    assert kwargs["min_score"] == 70
    assert kwargs["limit"] == 25
    assert kwargs["start_after_score"] == 80


def test_list_scores_returns_cursor_when_page_full():
    rows = [_row(f"c_{i}", "boil", 95 - i) for i in range(3)]
    with patch("services.firestore.scores.list_contact_scores", return_value=rows):
        with TestClient(app) as client:
            resp = client.get("/scores?limit=3")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 3
    assert body["next_cursor"] == rows[-1]["lead_heat_score"]


def test_list_scores_503_on_firestore_failure():
    with patch(
        "services.firestore.scores.list_contact_scores",
        side_effect=RuntimeError("index missing — https://console..."),
    ):
        with TestClient(app) as client:
            resp = client.get("/scores")
    assert resp.status_code == 503
    assert "index missing" in resp.json()["detail"]


def test_list_scores_rejects_invalid_step():
    with TestClient(app) as client:
        resp = client.get("/scores?step=99")
    assert resp.status_code == 422  # FastAPI validation


def test_list_scores_caps_limit():
    with TestClient(app) as client:
        resp = client.get("/scores?limit=500")
    # > 200 should reject at the route level via Query(le=200)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# /scores/{contact_id}
# ---------------------------------------------------------------------------

def test_get_score_returns_full_findings():
    row = _row("c_detail", "boil", 92, ready=True)
    with patch(
        "services.firestore.scores.get_contact_score",
        return_value=row,
    ):
        with TestClient(app) as client:
            resp = client.get("/scores/c_detail")
    assert resp.status_code == 200
    body = resp.json()
    assert body["contact_id"] == "c_detail"
    assert body["lead_heat"] == "boil"
    assert body["lead_heat_score"] == 92
    assert body["findings"]["score_type"] == "PIPELINE-SCORE"


def test_get_score_404_when_missing():
    with patch("services.firestore.scores.get_contact_score", return_value=None):
        with TestClient(app) as client:
            resp = client.get("/scores/c_nope")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /scores/{contact_id}/history
# ---------------------------------------------------------------------------

def test_score_history_returns_runs():
    runs = [
        {
            "run_id": "run_a", "score_type_id": "PIPELINE-SCORE",
            "finished_at": datetime.now(tz=timezone.utc),
            "triggered_by": "daily",
            "current_step": 4, "current_step_name": "Step 4: Sample",
            "lead_heat": "boil", "lead_heat_score": 88,
            "step_confidence": 0.9, "ready_to_advance": False,
            "summary_one_line": "Hot lead.",
            "model": "claude-sonnet-4-6", "status": "succeeded",
        },
    ]
    with patch("services.firestore.scores.list_score_history", return_value=runs):
        with TestClient(app) as client:
            resp = client.get("/scores/c_hist/history")
    assert resp.status_code == 200
    body = resp.json()
    assert body["contact_id"] == "c_hist"
    assert body["count"] == 1
    assert body["items"][0]["run_id"] == "run_a"
