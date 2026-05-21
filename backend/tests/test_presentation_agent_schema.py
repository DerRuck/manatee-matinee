"""
Schema validation tests for the Presentation Agent.

These tests exercise the Pydantic schema directly — no Claude calls — so
they're fast and run in CI. They verify:
  - A well-formed PA-CURIOSITY outline validates
  - Slide layout discrimination works
  - Slide numbering must be sequential
  - outline_type_id and findings.outline_type must agree
  - DataPointSlide requires at least one source
  - The narrowed json_schema_for_type emits a usable schema
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from services.presentation_agent.schema import (
    BulletSlide,
    ClosingSlide,
    CuriosityMeetingFindings,
    DataPointSlide,
    Pillar,
    PresentationOutline,
    Source,
    ThreePillarSlide,
    TitleSlide,
    json_schema_for_type,
    parse_response,
)


def _valid_outline_payload() -> dict:
    return {
        "outline_type_id": "PA-CURIOSITY",
        "prompt_version": 1,
        "run_id": "00000000-0000-0000-0000-000000000001",
        "contact_id": "ghl_test",
        "municipality_name": "Rookery Bay NERR",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall_confidence": 0.7,
        "upstream_briefs": [],
        "findings": {
            "outline_type": "PA-CURIOSITY",
            "audience": "Reserve Manager and field staff",
            "meeting_objective": "Secure a Step 4 working session",
            "champion_name": "Dr. Sarah Chen",
            "deck_title": "A Partnership Proposal for Rookery Bay",
            "deck_subtitle": "Rookery Bay National Estuarine Research Reserve",
            "slides": [
                {
                    "layout": "title",
                    "slide_number": 1,
                    "title": "A Partnership Proposal for Rookery Bay",
                    "subtitle": "Rookery Bay National Estuarine Research Reserve",
                    "date_text": "May 26, 2026",
                },
                {
                    "layout": "three_pillar",
                    "slide_number": 2,
                    "title": "Why Tidal Creek Restoration Stalls",
                    "pillars": [
                        {"heading": "Engineering cost", "body": "Initial feasibility runs into hundreds of thousands of dollars before any work begins."},
                        {"heading": "Independent science gap", "body": "Reserves need objective data they can defend in front of funders."},
                        {"heading": "Political friction", "body": "Multi-agency permitting stalls projects for years."},
                    ],
                },
                {
                    "layout": "bullet",
                    "slide_number": 3,
                    "title": "What C-HAWQ Brings",
                    "bullets": [
                        "Exploration grants up to $200K at no cost to the reserve",
                        "Independent academic partnerships through USF and FGCU",
                        "P3 procurement support and engineering coordination",
                    ],
                },
                {
                    "layout": "bullet",
                    "slide_number": 4,
                    "title": "Why This Project Fits",
                    "bullets": [
                        "Tidal-creek connectivity matches the reserve's research mandate",
                        "Localized data from Henderson Creek already documented",
                        "Funding pathway via FDEP Section 320 is open through Q4",
                    ],
                },
                {
                    "layout": "closing",
                    "slide_number": 5,
                    "call_to_action": "Schedule a Step 4 working session for the week of June 9.",
                    "leave_behind_summary": "C-HAWQ funds feasibility, lines up academic partners, and walks the project through P3 finance.",
                },
            ],
            "suggested_next_step": "Schedule a Step 4 working session for the week of June 9.",
        },
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_valid_outline_validates():
    outline = PresentationOutline.model_validate(_valid_outline_payload())
    assert outline.outline_type_id == "PA-CURIOSITY"
    assert outline.findings.outline_type == "PA-CURIOSITY"
    assert len(outline.findings.slides) == 5
    assert isinstance(outline.findings.slides[0], TitleSlide)
    assert isinstance(outline.findings.slides[1], ThreePillarSlide)
    assert isinstance(outline.findings.slides[-1], ClosingSlide)


def test_parse_response_round_trip():
    raw = PresentationOutline.model_validate(_valid_outline_payload()).model_dump_json()
    outline = parse_response(raw)
    assert outline.findings.deck_title == "A Partnership Proposal for Rookery Bay"


# ---------------------------------------------------------------------------
# Schema enforcement
# ---------------------------------------------------------------------------

def test_outline_type_id_must_match_findings():
    payload = _valid_outline_payload()
    payload["outline_type_id"] = "PA-COMMISSION"
    with pytest.raises(ValidationError, match="does not match"):
        PresentationOutline.model_validate(payload)


def test_slide_numbers_must_be_sequential():
    payload = _valid_outline_payload()
    payload["findings"]["slides"][1]["slide_number"] = 5
    with pytest.raises(ValidationError, match="sequential|1..N"):
        PresentationOutline.model_validate(payload)


def test_title_slide_requires_title():
    with pytest.raises(ValidationError):
        TitleSlide(slide_number=1, title="")


def test_three_pillar_requires_exactly_three():
    with pytest.raises(ValidationError):
        ThreePillarSlide(
            slide_number=2,
            title="Why",
            pillars=[
                Pillar(heading="A", body="one"),
                Pillar(heading="B", body="two"),
            ],
        )


def test_data_point_slide_requires_source():
    with pytest.raises(ValidationError):
        DataPointSlide(
            slide_number=2,
            headline="18 acres lost",
            plain_english_framing="Habitat for 4M blue crabs gone.",
            sources=[],
        )


def test_data_point_slide_accepts_valid_source():
    slide = DataPointSlide(
        slide_number=2,
        headline="18 acres lost since 2018",
        plain_english_framing="Roughly the footprint of 14 football fields of habitat gone.",
        sources=[Source(url="https://fldatamart.org", title="FDEP", reliability_score=0.9)],
    )
    assert slide.layout == "data_point"


def test_bullet_slide_min_max_bullets():
    BulletSlide(slide_number=1, title="OK", bullets=["one", "two"])
    with pytest.raises(ValidationError):
        BulletSlide(slide_number=1, title="OK", bullets=["only one"])
    with pytest.raises(ValidationError):
        BulletSlide(
            slide_number=1, title="OK",
            bullets=["1", "2", "3", "4", "5", "6", "7"],
        )


# ---------------------------------------------------------------------------
# Findings min/max + curiosity-specific shape
# ---------------------------------------------------------------------------

def test_findings_requires_at_least_five_slides():
    """Curiosity decks below 5 slides fail validation — the deck needs
    enough room to land the Why/What/How beats per the binder skeleton."""
    with pytest.raises(ValidationError):
        CuriosityMeetingFindings(
            audience="staff",
            meeting_objective="curiosity",
            deck_title="Test",
            slides=[
                TitleSlide(slide_number=1, title="x"),
                BulletSlide(slide_number=2, title="s2", bullets=["a", "b"]),
                BulletSlide(slide_number=3, title="s3", bullets=["a", "b"]),
                ClosingSlide(slide_number=4, call_to_action="meet next week"),
            ],
            suggested_next_step="meet next week",
        )


def test_findings_caps_at_ten_slides():
    slides = [TitleSlide(slide_number=1, title="x")]
    slides.extend(
        BulletSlide(slide_number=i, title=f"s{i}", bullets=["a", "b"])
        for i in range(2, 11)
    )
    slides.append(ClosingSlide(slide_number=12, call_to_action="next"))
    with pytest.raises(ValidationError):
        CuriosityMeetingFindings(
            audience="staff",
            meeting_objective="curiosity",
            deck_title="Test",
            slides=slides,
            suggested_next_step="meet next week",
        )


# ---------------------------------------------------------------------------
# Narrowed JSON schema (used in the system prompt)
# ---------------------------------------------------------------------------

def test_json_schema_for_type_is_narrow():
    schema = json_schema_for_type("PA-CURIOSITY")
    defs = schema.get("$defs", {})
    assert "CuriosityMeetingFindings" in defs
    assert "TitleSlide" in defs
    assert "ThreePillarSlide" in defs
    assert "ClosingSlide" in defs
    assert schema["properties"]["outline_type_id"]["type"] == "string"


def test_json_schema_for_unknown_type_falls_back():
    schema = json_schema_for_type("PA-UNKNOWN")
    assert "properties" in schema
