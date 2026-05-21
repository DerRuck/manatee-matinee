"""
Schema validation + golden scenario tests for the Scoring Agent.

These tests exercise the Pydantic schema directly — no Claude calls — so
they run in CI. They cover:
  - Happy path: each of four golden scenarios (Step 1 cold, Step 4
    ready-to-advance, Step 7 mobilized, stalled lead) validates.
  - Invariant enforcement: lead_heat / lead_heat_score banding,
    ready_to_advance vs. blocker consistency, boil_criteria required at
    Step 3+, score_type_id ↔ findings.score_type agreement.
  - Schema narrow: json_schema_for_type returns a usable prompt block.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from services.scoring_agent.schema import (
    Blocker,
    BoilCriterion,
    GoNoGoScorecard,
    PipelineScoreFindings,
    PROVEN_PROCESS_STEPS,
    RecommendedAction,
    ScoringResult,
    Signal,
    json_schema_for_type,
    parse_response,
    phase_for_step,
    step_name,
)


# ---------------------------------------------------------------------------
# Golden scenarios — every scoring run should look like one of these
# ---------------------------------------------------------------------------

def _envelope(findings: dict, contact_id: str = "ghl_test") -> dict:
    return {
        "score_type_id": "PIPELINE-SCORE",
        "prompt_version": 1,
        "run_id": "00000000-0000-0000-0000-000000000001",
        "contact_id": contact_id,
        "municipality_name": "Sample City",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "triggered_by": "manual",
        "findings": findings,
    }


GOLDEN_STEP1_COLD = _envelope({
    "score_type": "PIPELINE-SCORE",
    "current_step": 1,
    "current_step_name": step_name(1),
    "current_phase": phase_for_step(1),
    "step_confidence": 0.72,
    "ready_to_advance": False,
    "next_step_blockers": [
        {"description": "No reply to first-touch email after 22 days",
         "owner": "champion", "severity": "medium"},
    ],
    "lead_heat": "cold",
    "lead_heat_score": 18,
    "boil_criteria": [],  # optional pre-Step 3
    "signals": [
        {"description": "First-touch email sent 2026-04-29, no reply",
         "evidence_source": "agent_runs/abc123 (hello_world)",
         "impact": "negative", "weight": 0.6},
    ],
    "recommended_actions": [
        {"action": "Send Template 1B follow-up", "owner": "Logan",
         "due_within_days": 2, "proven_process_step": 1},
    ],
    "summary_one_line": "Step 1, cold — no reply in 22 days; send Template 1B follow-up.",
    "days_since_last_signal": 22,
})


GOLDEN_STEP4_BOIL = _envelope({
    "score_type": "PIPELINE-SCORE",
    "current_step": 4,
    "current_step_name": step_name(4),
    "current_phase": phase_for_step(4),
    "step_confidence": 0.88,
    "ready_to_advance": True,
    "next_step_blockers": [],
    "lead_heat": "boil",
    "lead_heat_score": 84,
    "boil_criteria": [
        {"key": "has_real_authority", "answer": "yes",
         "evidence": "PW Director per S3-PREP brief"},
        {"key": "specific_real_project_exists", "answer": "yes",
         "evidence": "Seagrass loss in 3 named shoreline zones"},
        {"key": "project_is_stalled_solvable", "answer": "yes",
         "evidence": "Stalled on FDEP funding gap"},
        {"key": "champion_has_personal_passion", "answer": "yes",
         "evidence": "Quoted in intake: 'this is personal'"},
        {"key": "aligns_with_chawq_focus", "answer": "yes",
         "evidence": "Seagrass + nature-based shoreline = core fit"},
    ],
    "go_no_go": {
        "authority_score": 3, "project_specificity_score": 3, "solvability_score": 3,
        "champion_passion_score": 3, "chawq_fit_score": 3, "p3_candidacy_score": 2,
        "political_readiness_score": 2,
        "decision": "GO",
        "rationale": "Strong champion, named project, P3 fit. Commission read still uncertain.",
    },
    "signals": [
        {"description": "Intake meeting closed with Go decision",
         "evidence_source": "agent_runs/sarasota_intake_2026_04 (S3-PREP)",
         "impact": "positive", "weight": 0.95},
        {"description": "S4-LETTER drafted and sent within 48 hours",
         "evidence_source": "agent_runs/s4letter_2026_04_30",
         "impact": "positive", "weight": 0.8},
    ],
    "recommended_actions": [
        {"action": "Prepare PA-STEP4 deck for June 12 site walk",
         "owner": "AI:presentation_agent", "due_within_days": 5,
         "proven_process_step": 4},
    ],
    "summary_one_line": "Step 4 ready to advance — June 12 working session locked.",
    "days_since_last_signal": 3,
})


GOLDEN_STEP7_MOBILIZED = _envelope({
    "score_type": "PIPELINE-SCORE",
    "current_step": 7,
    "current_step_name": step_name(7),
    "current_phase": phase_for_step(7),
    "step_confidence": 0.81,
    "ready_to_advance": False,
    "next_step_blockers": [
        {"description": "Two skeptical commissioners still need 1-on-1 briefing",
         "owner": "C-HAWQ", "severity": "high"},
    ],
    "lead_heat": "boil",
    "lead_heat_score": 78,
    "boil_criteria": [
        {"key": "has_real_authority", "answer": "yes", "evidence": "Champion + PW Dir confirmed"},
        {"key": "specific_real_project_exists", "answer": "yes", "evidence": "Adopted on CIP"},
        {"key": "project_is_stalled_solvable", "answer": "yes", "evidence": "Political support, solvable"},
        {"key": "champion_has_personal_passion", "answer": "yes", "evidence": "Public-meeting testimony"},
        {"key": "aligns_with_chawq_focus", "answer": "yes", "evidence": "Stormwater + estuary"},
    ],
    "go_no_go": {
        "authority_score": 3, "project_specificity_score": 3, "solvability_score": 2,
        "champion_passion_score": 3, "chawq_fit_score": 3, "p3_candidacy_score": 3,
        "political_readiness_score": 2,
        "decision": "GO",
        "rationale": "Project mobilized; political socialization underway.",
    },
    "signals": [
        {"description": "PA-CURIOSITY deck delivered to wider staff",
         "evidence_source": "agent_runs/curiosity_2026_05 (PA-CURIOSITY)",
         "impact": "positive", "weight": 0.7},
        {"description": "S7-PLAN community event executed",
         "evidence_source": "agent_runs/s7plan_2026_05",
         "impact": "positive", "weight": 0.8},
    ],
    "recommended_actions": [
        {"action": "Brief Commissioner Jones 1-on-1 next week",
         "owner": "Emily", "due_within_days": 7, "proven_process_step": 8},
        {"action": "Brief Commissioner Park 1-on-1 next week",
         "owner": "Emily", "due_within_days": 7, "proven_process_step": 8},
    ],
    "summary_one_line": "Step 7 — two commissioners still need 1-on-1 briefings before vote.",
    "days_since_last_signal": 4,
})


GOLDEN_STALLED = _envelope({
    "score_type": "PIPELINE-SCORE",
    "current_step": 2,
    "current_step_name": step_name(2),
    "current_phase": phase_for_step(2),
    "step_confidence": 0.6,
    "ready_to_advance": False,
    "next_step_blockers": [
        {"description": "Champion stopped replying after intake reschedule",
         "owner": "champion", "severity": "high"},
    ],
    "lead_heat": "stall",
    "lead_heat_score": 28,
    "boil_criteria": [],
    "signals": [
        {"description": "No inbound signal in 47 days — was warm 2026-03",
         "evidence_source": "ghl_contact_notes:last_reply=2026-03-15",
         "impact": "negative", "weight": 0.9},
    ],
    "recommended_actions": [
        {"action": "Send Template 2C stall-rescue email",
         "owner": "Emily", "due_within_days": 3, "proven_process_step": 2},
    ],
    "summary_one_line": "Step 2 stalled — 47 days no reply; send Template 2C re-engagement.",
    "days_since_last_signal": 47,
})


GOLDEN_SCENARIOS = {
    "step1_cold":   GOLDEN_STEP1_COLD,
    "step4_boil":   GOLDEN_STEP4_BOIL,
    "step7_boil":   GOLDEN_STEP7_MOBILIZED,
    "step2_stall":  GOLDEN_STALLED,
}


@pytest.mark.parametrize("scenario_name", list(GOLDEN_SCENARIOS.keys()))
def test_golden_scenario_validates(scenario_name):
    payload = GOLDEN_SCENARIOS[scenario_name]
    result = ScoringResult.model_validate(payload)
    assert result.score_type_id == "PIPELINE-SCORE"
    assert result.findings.score_type == "PIPELINE-SCORE"
    assert result.findings.current_phase == phase_for_step(result.findings.current_step)


def test_golden_step4_decision_total_matches_decision():
    result = ScoringResult.model_validate(GOLDEN_STEP4_BOIL)
    gng = result.findings.go_no_go
    assert gng is not None
    assert gng.total == 19  # 3+3+3+3+3+2+2
    assert gng.decision == "GO"


def test_parse_response_round_trip():
    raw = ScoringResult.model_validate(GOLDEN_STEP4_BOIL).model_dump_json()
    result = parse_response(raw)
    assert result.findings.lead_heat == "boil"
    assert result.findings.lead_heat_score == 84


# ---------------------------------------------------------------------------
# Invariant enforcement
# ---------------------------------------------------------------------------

def test_score_type_id_must_match_findings():
    payload = json.loads(json.dumps(GOLDEN_STEP4_BOIL, default=str))
    payload["score_type_id"] = "WRONG-TYPE"
    with pytest.raises(ValidationError, match="does not match"):
        ScoringResult.model_validate(payload)


def test_lead_heat_score_must_match_band_boil():
    with pytest.raises(ValidationError, match="boil leads must score 70 or higher"):
        PipelineScoreFindings(
            current_step=1, current_step_name=step_name(1), current_phase=1,
            step_confidence=0.5, ready_to_advance=False,
            next_step_blockers=[Blocker(description="x")],
            lead_heat="boil", lead_heat_score=30,
            signals=[Signal(description="x", evidence_source="y", impact="positive")],
            summary_one_line="x",
        )


def test_lead_heat_score_must_match_band_cold():
    with pytest.raises(ValidationError, match="cold leads must score below 40"):
        PipelineScoreFindings(
            current_step=1, current_step_name=step_name(1), current_phase=1,
            step_confidence=0.5, ready_to_advance=False,
            next_step_blockers=[Blocker(description="x")],
            lead_heat="cold", lead_heat_score=85,
            signals=[Signal(description="x", evidence_source="y", impact="negative")],
            summary_one_line="x",
        )


def test_lost_leads_must_score_below_20():
    with pytest.raises(ValidationError, match="lost leads must score below 20"):
        PipelineScoreFindings(
            current_step=1, current_step_name=step_name(1), current_phase=1,
            step_confidence=0.95, ready_to_advance=False,
            next_step_blockers=[Blocker(description="x")],
            lead_heat="lost", lead_heat_score=45,
            signals=[Signal(description="x", evidence_source="y", impact="negative")],
            summary_one_line="x",
        )


def test_ready_to_advance_disallows_blockers():
    with pytest.raises(ValidationError, match="must be empty when ready_to_advance=True"):
        PipelineScoreFindings(
            current_step=2, current_step_name=step_name(2), current_phase=1,
            step_confidence=0.8, ready_to_advance=True,
            next_step_blockers=[Blocker(description="still blocked")],
            lead_heat="simmer", lead_heat_score=55,
            signals=[Signal(description="x", evidence_source="y", impact="positive")],
            summary_one_line="x",
        )


def test_not_ready_requires_a_blocker():
    with pytest.raises(ValidationError, match="must contain at least one"):
        PipelineScoreFindings(
            current_step=2, current_step_name=step_name(2), current_phase=1,
            step_confidence=0.8, ready_to_advance=False,
            next_step_blockers=[],
            lead_heat="simmer", lead_heat_score=55,
            signals=[Signal(description="x", evidence_source="y", impact="positive")],
            summary_one_line="x",
        )


def test_boil_criteria_required_at_step_3_plus():
    with pytest.raises(ValidationError, match="boil_criteria must be filled"):
        PipelineScoreFindings(
            current_step=3, current_step_name=step_name(3), current_phase=1,
            step_confidence=0.7, ready_to_advance=True,
            next_step_blockers=[], boil_criteria=[],
            lead_heat="simmer", lead_heat_score=55,
            signals=[Signal(description="x", evidence_source="y", impact="positive")],
            summary_one_line="x",
        )


def test_boil_criteria_optional_below_step_3():
    # Pre-Step-3 with empty boil_criteria validates fine
    f = PipelineScoreFindings(
        current_step=2, current_step_name=step_name(2), current_phase=1,
        step_confidence=0.7, ready_to_advance=True,
        next_step_blockers=[], boil_criteria=[],
        lead_heat="simmer", lead_heat_score=55,
        signals=[Signal(description="x", evidence_source="y", impact="positive")],
        summary_one_line="x",
    )
    assert f.boil_criteria == []


def test_signals_required():
    """Every score must cite at least one signal — non-negotiable."""
    with pytest.raises(ValidationError):
        PipelineScoreFindings(
            current_step=1, current_step_name=step_name(1), current_phase=1,
            step_confidence=0.7, ready_to_advance=False,
            next_step_blockers=[Blocker(description="x")],
            lead_heat="cold", lead_heat_score=15,
            signals=[],   # MUST have at least 1
            summary_one_line="x",
        )


def test_summary_line_length_capped():
    with pytest.raises(ValidationError):
        PipelineScoreFindings(
            current_step=1, current_step_name=step_name(1), current_phase=1,
            step_confidence=0.7, ready_to_advance=False,
            next_step_blockers=[Blocker(description="x")],
            lead_heat="cold", lead_heat_score=15,
            signals=[Signal(description="x", evidence_source="y", impact="positive")],
            summary_one_line="x" * 250,  # over 200 char cap
        )


def test_step_confidence_bounds():
    with pytest.raises(ValidationError):
        PipelineScoreFindings(
            current_step=1, current_step_name=step_name(1), current_phase=1,
            step_confidence=1.5, ready_to_advance=False,
            next_step_blockers=[Blocker(description="x")],
            lead_heat="cold", lead_heat_score=15,
            signals=[Signal(description="x", evidence_source="y", impact="positive")],
            summary_one_line="x",
        )


def test_go_no_go_total_property():
    gng = GoNoGoScorecard(
        authority_score=3, project_specificity_score=2, solvability_score=2,
        champion_passion_score=3, chawq_fit_score=3, p3_candidacy_score=1,
        political_readiness_score=2,
        decision="CONDITIONAL_GO",
        rationale="Strong champion, weaker P3 indicators.",
    )
    assert gng.total == 16


# ---------------------------------------------------------------------------
# Step / phase taxonomy
# ---------------------------------------------------------------------------

def test_step_name_format():
    assert step_name(1).startswith("Step 1:")
    assert step_name(10).startswith("Step 10:")


def test_phase_for_step_mapping():
    assert phase_for_step(1) == 1
    assert phase_for_step(4) == 1
    assert phase_for_step(5) == 2
    assert phase_for_step(7) == 3
    assert phase_for_step(10) == 3


def test_proven_process_has_all_10_steps():
    assert set(PROVEN_PROCESS_STEPS.keys()) == set(range(1, 11))


# ---------------------------------------------------------------------------
# Narrow JSON schema (used in the system prompt)
# ---------------------------------------------------------------------------

def test_json_schema_for_type_is_narrow():
    schema = json_schema_for_type("PIPELINE-SCORE")
    defs = schema.get("$defs", {})
    assert "PipelineScoreFindings" in defs
    assert "Signal" in defs
    assert "Blocker" in defs
    assert "BoilCriterion" in defs
    assert "GoNoGoScorecard" in defs
    assert "RecommendedAction" in defs


def test_json_schema_for_unknown_type_falls_back():
    schema = json_schema_for_type("PIPELINE-UNKNOWN")
    assert "properties" in schema
