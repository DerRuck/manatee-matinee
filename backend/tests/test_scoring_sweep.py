"""Tests for the daily scoring sweep.

Firestore and the Claude agent are patched so the suite exercises:
  - eligibility filtering (no-signal, lost-heat, scored-recently)
  - per-contact error isolation (one failure doesn't abort the sweep)
  - report shape + telemetry
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from services.scoring_agent.sweep import (
    ContactSweepOutcome,
    _eligibility_check,
    _has_signal,
    run_daily_sweep,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _contact(cid: str, **extras) -> dict:
    """Default has signal (one tag) — pass tags=[] to make a bare stub."""
    base = {
        "id": cid,
        "firstNameRaw": "Sample",
        "lastNameRaw": "Contact",
        "tags": ["intake-done"],
        "customFields": [],
        "city": "Sample City",
    }
    base.update(extras)
    return base


def _fake_run_result(heat="boil", score=85):
    """Stub result + meta returned by ScoringAgent.run()."""
    result = MagicMock()
    result.run_id = "run-fake"
    result.findings.lead_heat = heat
    result.findings.lead_heat_score = score
    result.findings.current_step = 4
    meta = {"input_tokens": 1000, "output_tokens": 500, "elapsed_sec": 12.3,
            "model": "claude-sonnet-4-6"}
    return result, meta


# ---------------------------------------------------------------------------
# _has_signal
# ---------------------------------------------------------------------------

def test_has_signal_true_when_tags_present():
    assert _has_signal({"tags": ["x"]}) is True


def test_has_signal_true_when_municipality_plus_name():
    c = {"city": "Tampa", "firstNameRaw": "Jamie", "tags": []}
    assert _has_signal(c) is True


def test_has_signal_true_when_populated_custom_field():
    c = {"tags": [], "customFields": [{"value": "some note"}]}
    assert _has_signal(c) is True


def test_has_signal_false_for_bare_stub():
    assert _has_signal({"tags": [], "customFields": [], "id": "x"}) is False


# ---------------------------------------------------------------------------
# _eligibility_check
# ---------------------------------------------------------------------------

def test_eligibility_skips_no_signal_zero_runs():
    bare = {"tags": [], "customFields": []}
    assert _eligibility_check(bare, None, 0, 18, True) == "no_signal"


def test_eligibility_keeps_no_signal_if_has_prior_runs():
    bare = {"tags": [], "customFields": []}
    # A contact with a prior agent_run is worth scoring even without tags.
    assert _eligibility_check(bare, None, 1, 18, True) is None


def test_eligibility_skips_lost_heat_by_default():
    c = _contact("c1")
    latest = {"lead_heat": "lost", "scored_at": datetime.now(tz=timezone.utc) - timedelta(days=2)}
    assert _eligibility_check(c, latest, 0, 18, True) == "lead_lost"


def test_eligibility_keeps_lost_when_skip_lost_false():
    c = _contact("c1")
    latest = {"lead_heat": "lost", "scored_at": datetime.now(tz=timezone.utc) - timedelta(days=2)}
    assert _eligibility_check(c, latest, 0, 18, False) is None


def test_eligibility_skips_recently_scored():
    c = _contact("c1")
    latest = {"lead_heat": "boil", "scored_at": datetime.now(tz=timezone.utc) - timedelta(hours=2)}
    assert _eligibility_check(c, latest, 0, 18, True) == "scored_recently"


def test_eligibility_allows_old_score():
    c = _contact("c1")
    latest = {"lead_heat": "boil", "scored_at": datetime.now(tz=timezone.utc) - timedelta(days=2)}
    assert _eligibility_check(c, latest, 0, 18, True) is None


def test_eligibility_handles_missing_scored_at():
    c = _contact("c1")
    latest = {"lead_heat": "boil"}  # no scored_at field
    assert _eligibility_check(c, latest, 0, 18, True) is None


# ---------------------------------------------------------------------------
# run_daily_sweep — end-to-end with all I/O patched
# ---------------------------------------------------------------------------

def _patch_sweep(contacts, scores_by_id=None, runs_by_id=None):
    """Common patching for sweep tests.

    Patches the Firestore-touching helpers AND the persist hook so no
    network call leaks during the sweep.
    """
    scores_by_id = scores_by_id or {}
    runs_by_id = runs_by_id or {}
    return [
        patch("services.scoring_agent.sweep._list_contacts", return_value=contacts),
        patch(
            "services.scoring_agent.sweep._get_latest_score",
            side_effect=lambda cid: scores_by_id.get(cid),
        ),
        patch(
            "services.scoring_agent.sweep._count_agent_runs",
            side_effect=lambda cid: runs_by_id.get(cid, 0),
        ),
        patch("services.scoring_agent.sweep._persist_sweep"),
    ]


def test_sweep_scores_eligible_contacts():
    contacts = [_contact("c1"), _contact("c2")]

    with _patch_sweep(contacts)[0], _patch_sweep(contacts)[1], \
         _patch_sweep(contacts)[2], _patch_sweep(contacts)[3]:
        with patch(
            "services.scoring_agent.context_builder.build_scoring_context",
            return_value={"contact_id": "c1", "agent_runs_summary": []},
        ), patch(
            "agents.scoring_agent.ScoringAgent",
        ) as AgentCls, patch(
            "services.scoring_agent.firestore_sync.persist_score",
        ) as persist:
            AgentCls.return_value.run.return_value = _fake_run_result()
            report = run_daily_sweep(
                max_contacts=10,
                triggered_by="daily",
                min_age_hours=0,
                persist_report=False,
            )

    assert report.total_eligible == 2
    assert report.total_scored == 2
    assert report.total_skipped == 0
    assert report.total_failed == 0
    assert persist.call_count == 2


def test_sweep_skips_bare_stubs_and_lost():
    contacts = [
        _contact("c_stub", tags=[], customFields=[], firstNameRaw=None,
                 lastNameRaw=None, city=None, companyName=None),
        _contact("c_lost"),
        _contact("c_keep"),
    ]
    scores_by_id = {
        "c_lost": {"lead_heat": "lost",
                   "scored_at": datetime.now(tz=timezone.utc) - timedelta(days=5)},
    }

    patches = _patch_sweep(contacts, scores_by_id=scores_by_id)
    with patches[0], patches[1], patches[2], patches[3], \
         patch("services.scoring_agent.context_builder.build_scoring_context",
               return_value={"contact_id": "c_keep"}), \
         patch("agents.scoring_agent.ScoringAgent") as AgentCls, \
         patch("services.scoring_agent.firestore_sync.persist_score"):
        AgentCls.return_value.run.return_value = _fake_run_result()
        report = run_daily_sweep(
            max_contacts=10,
            min_age_hours=0,
            persist_report=False,
        )

    assert report.total_skipped == 2
    assert report.total_scored == 1
    reasons = {o.contact_id: o.skipped_reason for o in report.outcomes
               if o.status == "skipped"}
    assert reasons["c_stub"] == "no_signal"
    assert reasons["c_lost"] == "lead_lost"


def test_sweep_isolates_per_contact_failures():
    contacts = [_contact("c_ok"), _contact("c_fail"), _contact("c_ok2")]

    def runner(contact_data):
        if contact_data["contact_id"] == "c_fail":
            raise RuntimeError("boom")
        return _fake_run_result()

    patches = _patch_sweep(contacts)
    with patches[0], patches[1], patches[2], patches[3], \
         patch(
            "services.scoring_agent.context_builder.build_scoring_context",
            side_effect=lambda cid, **kw: {"contact_id": cid},
         ), \
         patch("agents.scoring_agent.ScoringAgent") as AgentCls, \
         patch("services.scoring_agent.firestore_sync.persist_score"):
        AgentCls.return_value.run.side_effect = (
            lambda ctx, **kw: runner(ctx)
        )
        report = run_daily_sweep(
            max_contacts=10,
            min_age_hours=0,
            persist_report=False,
        )

    assert report.total_scored == 2
    assert report.total_failed == 1
    failed = next(o for o in report.outcomes if o.status == "failed")
    assert failed.contact_id == "c_fail"
    assert "boom" in (failed.error or "")


def test_sweep_dry_run_skips_llm_and_persist():
    contacts = [_contact("c1"), _contact("c2")]

    patches = _patch_sweep(contacts)
    with patches[0], patches[1], patches[2], patches[3], \
         patch("agents.scoring_agent.ScoringAgent") as AgentCls, \
         patch("services.scoring_agent.firestore_sync.persist_score") as persist:
        report = run_daily_sweep(
            max_contacts=10,
            min_age_hours=0,
            dry_run=True,
            persist_report=False,
        )

    AgentCls.assert_not_called()
    persist.assert_not_called()
    assert report.total_scored == 0
    assert report.total_skipped == 2
    assert all(o.skipped_reason == "dry_run" for o in report.outcomes)


def test_sweep_honors_custom_sweep_id():
    contacts = [_contact("c1")]
    patches = _patch_sweep(contacts)
    with patches[0], patches[1], patches[2], patches[3], \
         patch(
            "services.scoring_agent.context_builder.build_scoring_context",
            return_value={"contact_id": "c1"},
         ), \
         patch("agents.scoring_agent.ScoringAgent") as AgentCls, \
         patch("services.scoring_agent.firestore_sync.persist_score"):
        AgentCls.return_value.run.return_value = _fake_run_result()
        report = run_daily_sweep(
            max_contacts=1,
            min_age_hours=0,
            persist_report=False,
            sweep_id="caller-fixed-id",
        )

    assert report.sweep_id == "caller-fixed-id"
