"""
Pydantic v2 schema for every Presentation Agent output.

The Presentation Agent consumes upstream research (S4-DECK briefs, PW-3
municipality background, S1-4 contact background, etc.) and meeting
context (audience, champion, project focus) and emits a structured
PresentationOutline — a slide-by-slide plan that a human can polish or a
downstream renderer (python-pptx, Canva, Figma) can build into a real deck.

Architecture mirrors services/research_agent/schema.py:

  - PresentationOutline   : the strict envelope every run returns
  - SlideContent          : discriminated union of slide layouts
        TitleSlide          (deck cover)
        SectionDividerSlide (chapter break)
        ThreePillarSlide    (3-column "Why X / What we do" pattern)
        BulletSlide         (title + 2-6 bullets + speaker notes)
        TeamBioSlide        (named people with role + passion + fact)
        DataPointSlide      (statistic with plain-English framing)
        ComparableProjectSlide (Florida precedent)
        QuoteSlide          (pull quote + attribution)
        ClosingSlide        (CTA + contact line)
  - Findings              : discriminated union, typed per outline_type_id
        CuriosityMeetingFindings   -> PA-CURIOSITY
        Step4CustomDeckFindings    -> PA-STEP4
        KickoffDeckFindings        -> PA-KICKOFF

Adding a new outline type = add a new findings sub-model with
`outline_type: Literal["..."]` and append it to the Findings union.
No envelope changes needed.

Used by:
  - services/presentation_agent/runner.py     -> validates Claude's JSON response
  - services/presentation_agent/drive_sync.py -> renders to .pptx / .docx
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, HttpUrl, model_validator


# =============================================================================
# Atomic units
# =============================================================================

class Source(BaseModel):
    """A cited source. Used by data-bearing slides and the upstream brief list."""
    url: HttpUrl | str
    title: str | None = None
    reliability_score: float = Field(
        ge=0.0, le=1.0,
        description=(
            "0.0-1.0 source reliability. Use these anchors:\n"
            "  0.90-0.95: official statute, regulation, or the jurisdiction's "
            "OWN clerk/agency page documenting its own rules. A municipality's "
            "own .gov website IS authoritative for its own rules.\n"
            "  0.80-0.90: other .gov pages, .edu peer-reviewed publications, "
            "primary agency datasets (FDEP, USGS, NOAA).\n"
            "  0.60-0.80: established news, trade press.\n"
            "  0.40-0.60: opinion, advocacy-org summaries, secondary compilations.\n"
            "  <0.40: blogs, forums, AI summaries without a verifiable upstream "
            "source. Claims supported only by sources at this level are rejected."
        ),
    )


class VisualAsset(BaseModel):
    """Structured hint for a renderer about what image/diagram belongs on a slide.

    Renderers (python-pptx, Canva MCP, Figma MCP) can read this to fetch or
    place the right asset. `url` is optional because most assets are sourced
    at render time, not at outline time.
    """
    asset_type: Literal[
        "satellite_image", "photo", "diagram", "chart",
        "map", "icon", "logo", "headshot",
    ]
    description: str = Field(
        min_length=1,
        description="What the visual should show — specific enough for a designer or AI to find/build it",
    )
    suggested_source: str | None = Field(
        default=None,
        description="Where the asset can be sourced (e.g., 'USGS Earth Explorer, 2024')",
    )
    url: HttpUrl | str | None = None


class BriefReference(BaseModel):
    """Pointer to an upstream ResearchBrief this outline consumed.

    Keeps the outline auditable: every claim on every slide can be traced
    back to a research run, without duplicating the brief's full content.
    """
    research_type_id: str = Field(description="e.g. 'S4-DECK', 'PW-3'")
    run_id: str | None = Field(
        default=None,
        description="UUID of the upstream ResearchBrief run, if known",
    )
    summary: str | None = Field(
        default=None,
        description="One-line summary of what this brief contributed",
    )


# =============================================================================
# Slide layouts — one model per visual template
#
# Every slide carries:
#   - layout         : Literal discriminator
#   - slide_number   : 1-based position in the deck
#   - speaker_notes  : optional, used by both PPTX and Canva renderers
#
# Layout-specific fields capture only what that template needs.
# =============================================================================

class _SlideBase(BaseModel):
    slide_number: int = Field(ge=1, le=30)
    speaker_notes: str | None = Field(
        default=None,
        description="What to say while this slide is up — not what's on the slide",
    )


class TitleSlide(_SlideBase):
    """Deck cover. C-HAWQ's standard cover holds project + audience org + date."""
    layout: Literal["title"] = "title"
    title: str = Field(min_length=1, description="Main cover line")
    subtitle: str | None = Field(
        default=None,
        description="Audience or project subtitle, e.g. 'Rookery Bay National Estuarine Research Reserve'",
    )
    date_text: str | None = Field(
        default=None,
        description="Display date as it should appear on the cover, e.g. 'May 19, 2026'",
    )
    footer_text: str = Field(
        default="3375 Tamiami Trl E #100 | Naples, FL 34112 | CHAWQ.org",
        description="Brand footer line; keep default unless the deck calls for a custom one",
    )


class SectionDividerSlide(_SlideBase):
    """A chapter break — used in longer decks (Proven Process step headers)."""
    layout: Literal["section_divider"] = "section_divider"
    section_number: int | None = Field(default=None, ge=1, le=20)
    section_title: str = Field(min_length=1)
    big_idea: str | None = Field(
        default=None,
        description="One-sentence framing of what this section will cover",
    )


class Pillar(BaseModel):
    """One of three columns in a ThreePillarSlide."""
    heading: str = Field(min_length=1, description="Short headline, ~5 words")
    body: str = Field(
        min_length=1,
        description="2-4 sentence supporting paragraph",
    )


class ThreePillarSlide(_SlideBase):
    """C-HAWQ's signature 3-column layout.

    Used for "Why Vital Projects Stall", "What We're Doing About It", and any
    other slide that breaks one big idea into three reasons / approaches /
    benefits. Always exactly 3 pillars — that's the visual template.
    """
    layout: Literal["three_pillar"] = "three_pillar"
    title: str = Field(min_length=1)
    pillars: list[Pillar] = Field(min_length=3, max_length=3)


class BulletSlide(_SlideBase):
    """Standard title + bullets slide. The workhorse of process / overview decks."""
    layout: Literal["bullet"] = "bullet"
    title: str = Field(min_length=1)
    subtitle: str | None = Field(
        default=None,
        description="Optional sub-headline below the title",
    )
    bullets: list[str] = Field(min_length=2, max_length=6)
    visual_assets: list[VisualAsset] = Field(
        default_factory=list,
        description="Optional imagery to anchor the slide visually",
    )


class TeamMember(BaseModel):
    """One person on a TeamBioSlide."""
    name: str = Field(min_length=1)
    role: str = Field(min_length=1)
    bio_one_liner: str | None = Field(
        default=None,
        description="One sentence on background and experience",
    )
    passion: str | None = Field(
        default=None,
        description="What drives them — used in the 'Passion:' line on C-HAWQ team slides",
    )
    fact: str | None = Field(
        default=None,
        description="Memorable personal detail — used in the 'Fact:' line",
    )
    headshot_hint: VisualAsset | None = None


class TeamBioSlide(_SlideBase):
    """Team intro slide. C-HAWQ uses 3-6 members per slide with bio + passion + fact."""
    layout: Literal["team_bio"] = "team_bio"
    title: str = Field(default="C-HAWQ Team")
    members: list[TeamMember] = Field(min_length=1, max_length=6)


class DataPointSlide(_SlideBase):
    """A single statistic with plain-English framing — high-impact data slide."""
    layout: Literal["data_point"] = "data_point"
    headline: str = Field(
        min_length=1,
        description="The number or finding as a bold headline, e.g. '18 acres of seagrass lost since 2018'",
    )
    plain_english_framing: str = Field(
        min_length=1,
        description="What this means for a non-scientist — the 'human-scale' translation",
    )
    sources: list[Source] = Field(
        min_length=1,
        description="Citation for the underlying data — required for data slides",
    )
    visual_assets: list[VisualAsset] = Field(default_factory=list)


class ComparableProjectSlide(_SlideBase):
    """A Florida precedent — used to show the approach has worked elsewhere."""
    layout: Literal["comparable_project"] = "comparable_project"
    project_name: str = Field(min_length=1)
    municipality: str = Field(min_length=1)
    year: int = Field(ge=1990, le=2100)
    outcome: str = Field(
        min_length=1,
        description="What happened — 1-2 sentences focused on the result",
    )
    why_relevant: str = Field(
        min_length=1,
        description="Why this precedent matters for the audience's project",
    )
    cost_usd: int | None = None
    visual_assets: list[VisualAsset] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)


class QuoteSlide(_SlideBase):
    """A pull quote — champion, expert, or community voice."""
    layout: Literal["quote"] = "quote"
    quote: str = Field(min_length=1)
    attribution: str = Field(min_length=1, description="Speaker + role/affiliation")
    context: str | None = Field(
        default=None,
        description="Where/when this was said, if useful for credibility",
    )


class ClosingSlide(_SlideBase):
    """Final slide — call to action + contact info."""
    layout: Literal["closing"] = "closing"
    call_to_action: str = Field(
        min_length=1,
        description="The specific ask — what does the audience do next?",
    )
    contact_line: str = Field(
        default="CHAWQ.org | hello@chawq.org",
        description="Closing brand line — keep default unless deck calls for a custom one",
    )
    leave_behind_summary: str | None = Field(
        default=None,
        description="One paragraph the audience should remember if they remember nothing else",
    )


SlideContent = Annotated[
    Union[
        TitleSlide,
        SectionDividerSlide,
        ThreePillarSlide,
        BulletSlide,
        TeamBioSlide,
        DataPointSlide,
        ComparableProjectSlide,
        QuoteSlide,
        ClosingSlide,
    ],
    Field(discriminator="layout"),
]


# =============================================================================
# PA-CURIOSITY — Curiosity Meeting Deck
#
# Based on "Curiosity Meeting template.pptx" (5 slides):
# A short deck for the FIRST formal meeting between C-HAWQ and a municipal
# champion's wider staff. The goal is curiosity, not commitment — open the
# door, show what's possible, leave them wanting the next conversation.
# =============================================================================

class CuriosityMeetingFindings(BaseModel):
    outline_type: Literal["PA-CURIOSITY"] = "PA-CURIOSITY"

    audience: str = Field(
        min_length=1,
        description="Who is in the room — e.g. 'Rookery Bay Reserve Manager and field staff'",
    )
    meeting_objective: str = Field(
        min_length=1,
        description="What success looks like — usually 'secure a Step 4 follow-up meeting'",
    )
    champion_name: str | None = Field(
        default=None,
        description="The internal champion who arranged this meeting",
    )

    deck_title: str = Field(
        min_length=1,
        description="The cover title, e.g. 'A Partnership Proposal for Rookery Bay'",
    )
    deck_subtitle: str | None = Field(
        default=None,
        description="Cover subtitle — usually the audience organization name",
    )

    slides: list[SlideContent] = Field(
        min_length=5,
        max_length=10,
        description="5-10 slides. Most curiosity decks land at 7.",
    )

    suggested_next_step: str = Field(
        min_length=1,
        description="The concrete ask in the closing slide — what we want the audience to commit to",
    )


# =============================================================================
# PA-STEP4 — Custom Step 4 Deck
#
# The Step 4 deck Ryan builds after a successful intake meeting. Audience is
# the Champion + their wider team (department head, city manager, sometimes
# a commissioner). Built on the binder's 7-slide skeleton:
#   1. Cover
#   2. The Problem in Their Community  (localized data)
#   3. What's Possible                  (1-2 Florida comparables)
#   4. How C-HAWQ Helps
#   5. The Path Forward                 (conversation framework, not contract)
#   6. The Team
#   7. Next Step
# Slide count 6-9 leaves room for an optional data_point or quote.
# =============================================================================

class Step4CustomDeckFindings(BaseModel):
    outline_type: Literal["PA-STEP4"] = "PA-STEP4"

    audience: str = Field(
        min_length=1,
        description="Who is in the room — Champion + their team (city manager, department head, commissioner)",
    )
    meeting_objective: str = Field(
        min_length=1,
        description="What success looks like — usually 'align on the path forward and a concrete next step'",
    )
    champion_name: str | None = Field(
        default=None,
        description="The Champion who arranged this meeting",
    )

    deck_title: str = Field(min_length=1, description="Cover title")
    deck_subtitle: str | None = Field(
        default=None,
        description="Cover subtitle — usually the municipality or project name",
    )

    problem_area_focus: str = Field(
        min_length=1,
        description=(
            "The specific waterway/habitat/site the Champion named at intake. "
            "Drives every localized data point and visual on slide 2."
        ),
    )

    slides: list[SlideContent] = Field(
        min_length=6,
        max_length=9,
        description=(
            "6-9 slides. The binder skeleton lands at 7. Optional 8th/9th: an "
            "extra data_point or quote when a high-impact one is available."
        ),
    )

    suggested_next_step: str = Field(
        min_length=1,
        description="The concrete ask on the closing slide — a meeting, site visit, or document review",
    )


# =============================================================================
# PA-KICKOFF — Project Kickoff Deck (Step 9)
#
# The first formal meeting after the P3 agreement is signed. Attendees are
# the municipality, the C-HAWQ team, the engineering/GC partner, and (often)
# the grant administrator. Binder skeleton has 8 sections:
#   1. Welcome & Introductions
#   2. Project Overview
#   3. Funding Structure Confirmed       (actual figures)
#   4. Roles & Responsibilities
#   5. Project Timeline & Milestones
#   6. Communication Cadence
#   7. Open Items & Risk Register
#   8. Next Steps
# Slide count 7-12 — bigger than a curiosity or Step 4 deck because this one
# is operational, not persuasive.
# =============================================================================

class KickoffDeckFindings(BaseModel):
    outline_type: Literal["PA-KICKOFF"] = "PA-KICKOFF"

    project_name: str = Field(
        min_length=1,
        description="The official project name as it appears in the signed P3 agreement",
    )
    audience: str = Field(
        min_length=1,
        description="All parties present — municipality, C-HAWQ, GC, grant admin",
    )

    deck_title: str = Field(min_length=1)
    deck_subtitle: str | None = None

    slides: list[SlideContent] = Field(
        min_length=7,
        max_length=12,
        description="7-12 slides. Most kickoff decks land at 8 — one per section.",
    )

    communication_cadence: str = Field(
        min_length=1,
        description=(
            "Plain-language summary of how the core team will operate — "
            "meeting frequency, who's on the call, how decisions escalate."
        ),
    )

    top_risks: list[str] = Field(
        default_factory=list,
        max_length=8,
        description=(
            "Surfaced from the risk register slide. Each entry is one sentence: "
            "the risk + who owns mitigation. Empty list is allowed if there are none."
        ),
    )

    suggested_next_step: str = Field(
        min_length=1,
        description="The concrete next action — usually the date of the first project-cadence call",
    )


# =============================================================================
# Discriminated union — Pydantic auto-routes by outline_type literal
# =============================================================================

Findings = Annotated[
    Union[
        CuriosityMeetingFindings,
        Step4CustomDeckFindings,
        KickoffDeckFindings,
    ],
    Field(discriminator="outline_type"),
]


# =============================================================================
# The envelope
# =============================================================================

class PresentationOutline(BaseModel):
    """The canonical output of any Presentation Agent run.

    Mirrors ResearchBrief: typed envelope + discriminated findings union.
    Renderers downstream (python-pptx, Canva, Figma) read .findings.slides
    and translate each layout into a real slide.
    """

    outline_type_id: str = Field(
        description="Outline type, e.g. 'PA-CURIOSITY'. Must equal findings.outline_type.",
    )
    prompt_version: int = Field(ge=1)
    run_id: str = Field(description="UUID generated at run start")

    contact_id: str | None = Field(default=None, description="GHL contact id")
    municipality_name: str | None = None
    triggering_event: str | None = Field(
        default=None,
        description="GHL event that fired this run, if applicable",
    )

    generated_at: datetime

    overall_confidence: float = Field(
        ge=0.0, le=1.0,
        description="Holistic confidence in the outline's quality. <0.5 flag for human review.",
    )

    upstream_briefs: list[BriefReference] = Field(
        default_factory=list,
        description="Research briefs this outline drew from — for traceability",
    )

    findings: Findings

    notes: str | None = Field(
        default=None,
        description="Free-form caveats, design hints, or context for the human deck builder",
    )

    @model_validator(mode="after")
    def _outline_type_matches_findings(self) -> "PresentationOutline":
        if self.outline_type_id != self.findings.outline_type:
            raise ValueError(
                f"outline_type_id {self.outline_type_id!r} does not match "
                f"findings.outline_type {self.findings.outline_type!r}"
            )
        return self

    @model_validator(mode="after")
    def _slide_numbers_are_sequential(self) -> "PresentationOutline":
        nums = [s.slide_number for s in self.findings.slides]
        if nums != list(range(1, len(nums) + 1)):
            raise ValueError(
                f"slide_number values must be 1..N with no gaps. Got: {nums}"
            )
        return self


# =============================================================================
# Helpers used by runner and prompt builder
# =============================================================================

_FINDINGS_TYPE_MAP: dict[str, type] = {
    "PA-CURIOSITY": CuriosityMeetingFindings,
    "PA-STEP4":     Step4CustomDeckFindings,
    "PA-KICKOFF":   KickoffDeckFindings,
}


def json_schema_for_type(outline_type_id: str) -> dict[str, Any]:
    """PresentationOutline schema narrowed to one outline type.

    Smaller prompt block than the full union. Falls back to the full schema
    for unknown types.
    """
    from pydantic import create_model

    findings_cls = _FINDINGS_TYPE_MAP.get(outline_type_id)
    if findings_cls is None:
        return PresentationOutline.model_json_schema()

    NarrowOutline = create_model(
        "PresentationOutline",
        outline_type_id=(str, Field(description="Must equal findings.outline_type")),
        prompt_version=(int, Field(ge=1)),
        run_id=(str, Field(description="UUID")),
        contact_id=(str | None, Field(default=None)),
        municipality_name=(str | None, Field(default=None)),
        triggering_event=(str | None, Field(default=None)),
        generated_at=(datetime, ...),
        overall_confidence=(float, Field(ge=0.0, le=1.0)),
        upstream_briefs=(list[BriefReference], Field(default_factory=list)),
        findings=(findings_cls, ...),
        notes=(str | None, Field(default=None)),
    )
    return NarrowOutline.model_json_schema()


def parse_response(raw: str) -> PresentationOutline:
    """Validate Claude's JSON response. Raises ValidationError on schema violations."""
    return PresentationOutline.model_validate_json(raw)
