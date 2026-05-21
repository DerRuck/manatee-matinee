"""
Tests for the scoring agent context builder.

These verify the aggregation layer that turns a contact_id into a complete
agent input: contact record + agent_runs history + days-since-signal.
Firestore is patched out so the tests run without google-cloud-firestore.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from services.scoring_agent.context_builder import (
    _AGENT_TO_STEP,
    _days_since_last_signal,
    _extract_key_finding,
    _summarize_agent_runs,
    build_scoring_context,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_CONTACT_DOC = {
    "id": "0I21saCPXJVEbdncGXEW",
    "firstNameRaw": "Jamie",
    "lastNameRaw": "Sheehan",
    "contactName": "Jamie Sheehan",
    "email": "jamie@floridaenet.com",
    "companyName": "Florida Environmental Network",
    "city": None,
    "state": None,
    "tags": ["intake-done", "boil"],
    "customFields": [
        {"id": "u7nkCuvWJdcfe4mZLqjR", "fieldKey": "contact.contact_notes",
         "value": "Strong intake on 2/11."},
    ],
}


def _agent_run(agent_type: str, days_ago: int, **extras) -> dict:
    ts = datetime.now(tz=timezone.utc) - timedelta(days=days_ago)
    base = {
        "run_id": f"run_{agent_type}_{days_ago}",
        "research_type_id": agent_type if agent_type.startswith(("S", "L", "P")) else None,
        "outline_type_id": agent_type if agent_type.startswith("PA-") else None,
        "agent": "research" if not agent_type.startswith("PA-") else "presentation",
        "contact_id": "0I21saCPXJVEbdncGXEW",
        "finished_at": ts,
        "generated_at": ts,
        "model": "claude-sonnet-4-6",
        "status": "succeeded",
    }
    base.update(extras)
    return base


# ---------------------------------------------------------------------------
# build_scoring_context — the public entry point
# ---------------------------------------------------------------------------

def test_build_context_assembles_full_record():
    runs = [
        _agent_run("S3-PREP", days_ago=10),
        _agent_run("S4-LETTER", days_ago=8),
        _agent_run("PA-STEP4", days_ago=3,
                   findings={"suggested_next_step": "Site walk June 12"}),
    ]
    with patch(
        "services.scoring_agent.context_builder._fetch_contact",
        return_value=_CONTACT_DOC,
    ), patch(
        "services.scoring_agent.context_builder._fetch_agent_runs",
        return_value=runs,
    ):
        ctx = build_scoring_context("0I21saCPXJVEbdncGXEW", triggered_by="manual")

    assert ctx["contact_id"] == "0I21saCPXJVEbdncGXEW"
    # contact_record is the flattened GHL doc — with company fallback for null city
    assert ctx["contact_record"]["municipality_name"] == "Florida Environmental Network"
    assert ctx["contact_record"]["contact_notes"].startswith("Strong intake")
    # agent_runs summarized with proven-process step tags
    summaries = ctx["agent_runs_summary"]
    assert len(summaries) == 3
    assert summaries[0]["agent_type"] == "S3-PREP"
    assert summaries[0]["proven_process_step"] == 3
    assert summaries[2]["agent_type"] == "PA-STEP4"
    assert summaries[2]["proven_process_step"] == 4
    assert summaries[2]["key_finding"] == "Site walk June 12"
    # Most recent agent run was 3 days ago
    assert ctx["days_since_last_signal"] == 3
    assert ctx["triggered_by"] == "manual"


def test_build_context_raises_when_contact_missing():
    with patch(
        "services.scoring_agent.context_builder._fetch_contact",
        return_value=None,
    ):
        with pytest.raises(ValueError, match="not found in Firestore"):
            build_scoring_context("ghl_nonexistent")


def test_build_context_with_no_agent_runs():
    """Brand-new Step 1 contact — should still produce a usable context."""
    with patch(
        "services.scoring_agent.context_builder._fetch_contact",
        return_value=_CONTACT_DOC,
    ), patch(
        "services.scoring_agent.context_builder._fetch_agent_runs",
        return_value=[],
    ):
        ctx = build_scoring_context("0I21saCPXJVEbdncGXEW")

    assert ctx["agent_runs_summary"] == []
    assert ctx["days_since_last_signal"] is None


def test_build_context_default_trigger_is_manual():
    with patch(
        "services.scoring_agent.context_builder._fetch_contact",
        return_value=_CONTACT_DOC,
    ), patch(
        "services.scoring_agent.context_builder._fetch_agent_runs",
        return_value=[],
    ):
        ctx = build_scoring_context("0I21saCPXJVEbdncGXEW")
    assert ctx["triggered_by"] == "manual"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def test_summarize_agent_runs_handles_mixed_agent_types():
    runs = [
        _agent_run("S6-1", days_ago=15),
        _agent_run("PA-CURIOSITY", days_ago=12),
        # Unknown agent type — proven_process_step should be None
        {"run_id": "rx", "agent": "hello_world", "finished_at": datetime.now(tz=timezone.utc),
         "content_preview": "Hello world output preview"},
    ]
    summaries = _summarize_agent_runs(runs)
    assert summaries[0]["proven_process_step"] == 6
    assert summaries[1]["proven_process_step"] == 5
    assert summaries[2]["proven_process_step"] is None
    # Unknown agent still gets a key_finding from content_preview
    assert summaries[2]["key_finding"].startswith("Hello world")


def test_extract_key_finding_prefers_summary_one_line():
    run = {"findings": {"summary_one_line": "Strong fit",
                        "executive_summary": "longer text"}}
    assert _extract_key_finding(run) == "Strong fit"


def test_extract_key_finding_falls_back_to_executive_summary():
    run = {"findings": {"executive_summary": "Executive summary text"}}
    assert _extract_key_finding(run) == "Executive summary text"


def test_extract_key_finding_handles_no_findings():
    assert _extract_key_finding({}) is None
    assert _extract_key_finding({"findings": {}}) is None


def test_days_since_last_signal_uses_newest_timestamp():
    now = datetime.now(tz=timezone.utc)
    runs = [
        {"finished_at": now - timedelta(days=20)},
        {"finished_at": now - timedelta(days=3)},
        {"finished_at": now - timedelta(days=45)},
    ]
    assert _days_since_last_signal([], runs) == 3


def test_days_since_last_signal_returns_none_when_empty():
    assert _days_since_last_signal([], []) is None


def test_days_since_last_signal_handles_naive_datetimes():
    """Firestore timestamps without tzinfo should be treated as UTC."""
    naive = (datetime.now(tz=timezone.utc) - timedelta(days=7)).replace(tzinfo=None)
    assert _days_since_last_signal([], [{"finished_at": naive}]) >= 6


def test_agent_to_step_covers_all_proven_process_steps():
    # Every Proven Process step (1-10) should have at least one agent
    # that maps to it. Without this, a contact at e.g. Step 10 with one
    # agent_run would never be auto-tagged.
    mapped_steps = set(_AGENT_TO_STEP.values())
    assert mapped_steps >= set(range(1, 11)), (
        f"Missing agents for steps {set(range(1, 11)) - mapped_steps}"
    )
