"""Tests for the human-readable label used by the scoring drive_sync.

The Drive filename and the docx header should never fall back to the
opaque GHL contact_id when a person's name or email is available.
"""
from __future__ import annotations

from datetime import datetime, timezone

from services.scoring_agent.drive_sync import (
    _slug_for_filename,
    display_label,
    filename_for,
)
from services.scoring_agent.schema import (
    PipelineScoreFindings,
    ScoringResult,
    Signal,
)


def _result(**overrides) -> ScoringResult:
    findings = PipelineScoreFindings(
        current_step=1,
        current_step_name="Step 1: Discover the Municipal Champion",
        current_phase=1,
        step_confidence=0.7,
        ready_to_advance=False,
        next_step_blockers=[
            __import__("services.scoring_agent.schema", fromlist=["Blocker"]).Blocker(
                description="No champion yet",
            )
        ],
        lead_heat="cold",
        lead_heat_score=15,
        signals=[
            Signal(
                description="Stub contact",
                evidence_source="firestore:contacts/x",
                impact="neutral",
                weight=0.1,
            ),
        ],
        summary_one_line="Cold lead, awaiting first contact.",
    )
    payload = {
        "score_type_id":     "PIPELINE-SCORE",
        "prompt_version":    1,
        "run_id":            "abcd1234-0000-0000-0000-000000000000",
        "contact_id":        "0I21saCPXJVEbdncGXEW",
        "contact_name":      None,
        "contact_email":     None,
        "municipality_name": None,
        "generated_at":      datetime.now(timezone.utc),
        "triggered_by":      "manual",
        "findings":          findings,
    }
    payload.update(overrides)
    return ScoringResult(**payload)


# ---------------------------------------------------------------------------
# display_label priority chain
# ---------------------------------------------------------------------------

def test_display_label_prefers_contact_name():
    r = _result(
        contact_name="Jamie Sheehan",
        contact_email="jamie@floridaenet.com",
        municipality_name="Tallahassee",
    )
    assert display_label(r) == "Jamie Sheehan"


def test_display_label_falls_back_to_email():
    r = _result(
        contact_name=None,
        contact_email="jamie@floridaenet.com",
        municipality_name="Tallahassee",
    )
    assert display_label(r) == "jamie@floridaenet.com"


def test_display_label_falls_back_to_municipality():
    r = _result(
        contact_name=None,
        contact_email=None,
        municipality_name="Cedar Key",
    )
    assert display_label(r) == "Cedar Key"


def test_display_label_last_resort_is_contact_id():
    r = _result(
        contact_name=None,
        contact_email=None,
        municipality_name=None,
    )
    assert display_label(r) == "0I21saCPXJVEbdncGXEW"


def test_display_label_ignores_whitespace_only_values():
    r = _result(contact_name="   ", contact_email="jamie@x.com")
    assert display_label(r) == "jamie@x.com"


# ---------------------------------------------------------------------------
# filename_for slug
# ---------------------------------------------------------------------------

def test_filename_uses_readable_name():
    r = _result(contact_name="Jamie Sheehan")
    assert filename_for(r, "docx") == "pipeline_score_jamie_sheehan_abcd1234.docx"


def test_filename_handles_email_safely():
    r = _result(contact_name=None, contact_email="jamie@floridaenet.com")
    assert filename_for(r, "docx") == "pipeline_score_jamie_at_floridaenet.com_abcd1234.docx"


def test_filename_uses_municipality_when_no_person():
    r = _result(municipality_name="Cedar Key")
    assert filename_for(r, "json") == "pipeline_score_cedar_key_abcd1234.json"


def test_filename_strips_special_characters():
    r = _result(contact_name="O'Hara & Sons, LLC")
    fn = filename_for(r, "docx")
    assert fn.startswith("pipeline_score_o_hara_sons_llc_")
    assert fn.endswith("_abcd1234.docx")


def test_slug_collapses_consecutive_unsafe_chars():
    assert _slug_for_filename("a!!!@b") == "a_at_b"
    assert _slug_for_filename("   ") == "contact"
