"""
Pydantic v2 schema for every Research Agent output.

Architecture:
  - ResearchBrief  : the strict envelope every run returns
  - Claim          : a factual statement with sources + confidence
  - Source         : a cited URL with reliability_score
  - Findings       : discriminated union, typed per research_type
        GrantFindings                -> S6-1
        MunicipalityFindings         -> PW-3
        IntakePrepFindings           -> S3-PREP
        PoliticalFindings            -> S8-1
        LobbyistFindings             -> LOBBY-1
        ConferenceAttendeeFindings   -> PW-1
        LinkedInFindings             -> S1-2
        ContactBackgroundFindings    -> S1-4
        CommissionMeetingPrepFindings-> S3-3
        DeckResearchFindings         -> S4-DECK

Adding a new research type = add a new findings sub-model with
`research_type: Literal["..."]` and append it to the Findings union.
No envelope changes needed.

Used by:
  - services/research_agent/runner.py     -> validates Claude's JSON response
  - services/research_agent/drive_sync.py -> writes the brief to Drive
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


# =============================================================================
# Atomic units — used inside every research type's findings
# =============================================================================

class Source(BaseModel):
    """A cited source with reliability score.

    URL is loose because not every source is web-accessible (e.g. a binder
    chunk reference, a meeting minutes PDF behind auth).
    """
    url: HttpUrl | str
    title: str | None = None
    reliability_score: float = Field(
        ge=0.0, le=1.0,
        description="0.0-1.0. Authoritative >=0.8, reject <0.4.",
    )
    fetched_at: datetime | None = None


class Claim(BaseModel):
    """A single factual claim backed by at least one source above the reliability floor."""
    statement: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    sources: list[Source] = Field(min_length=1)

    @field_validator("sources")
    @classmethod
    def reject_all_low_reliability(cls, v: list[Source]) -> list[Source]:
        if all(s.reliability_score < 0.4 for s in v):
            raise ValueError(
                "Every source for this claim is below the 0.4 reliability "
                "floor. Re-source or drop the claim."
            )
        return v


# =============================================================================
# S6-1 — Grant Opportunity Research
# =============================================================================

class FloridaPrecedent(BaseModel):
    project: str
    municipality: str | None = None
    year: int = Field(ge=1990, le=2100)
    outcome: str
    award_usd: int | None = None
    url: HttpUrl | str | None = None


class GrantOpportunity(BaseModel):
    name: str
    administering_agency: str
    program_url: HttpUrl | str
    typical_award_usd_min: int | None = None
    typical_award_usd_max: int | None = None
    eligibility_summary: str
    deadline_or_cycle: str
    p3_compatible: Literal["yes", "with_caveats", "no"] = Field(
        description=(
            "P3 compatibility tier. "
            "'yes' = clean P3 fit, no material restrictions. "
            "'with_caveats' = compatible but has structural restrictions "
            "(loan-not-grant, municipality must be applicant, mandatory MOU, "
            "matching-fund constraints). "
            "'no' = structurally incompatible with P3."
        ),
    )
    documentation_required: list[str] = Field(default_factory=list)
    florida_precedents: list[FloridaPrecedent] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    sources: list[Source] = Field(min_length=1)


class Risk(BaseModel):
    description: str
    severity: Literal["low", "medium", "high"]
    mitigation: str | None = None
    sources: list[Source] = Field(default_factory=list)


class P3Contractor(BaseModel):
    firm_name: str
    project_executed: str
    municipality: str | None = None
    year: int = Field(ge=1990, le=2100)
    outcome: str
    sources: list[Source] = Field(min_length=1)


class GrantFindings(BaseModel):
    research_type: Literal["S6-1"] = "S6-1"
    grants: list[GrantOpportunity] = Field(min_length=1)
    risks_and_disqualifiers: list[Risk] = Field(default_factory=list)
    p3_contractors: list[P3Contractor] = Field(default_factory=list)


# =============================================================================
# PW-3 — Municipality Background Research
# =============================================================================

class LeadershipPerson(BaseModel):
    name: str
    role: str
    tenure_years: int | None = None
    notes: str | None = None
    sources: list[Source] = Field(min_length=1)


ProjectStatus = Literal["active", "stalled", "completed", "proposed", "unknown"]


class MunicipalProject(BaseModel):
    project_name: str
    status: ProjectStatus
    issue_type: str
    last_known_update: str | None = None
    sources: list[Source] = Field(min_length=1)


class EnvironmentalIssue(BaseModel):
    issue: str
    waterbody_or_location: str | None = None
    severity: Literal["low", "medium", "high"]
    recurring: bool = False
    sources: list[Source] = Field(min_length=1)


CommissionStance = Literal[
    "advocate", "supportive", "neutral", "skeptic", "opponent", "unknown"
]


class CommissionMember(BaseModel):
    name: str
    seat_or_district: str | None = None
    environmental_stance: CommissionStance
    notes: str | None = None
    sources: list[Source] = Field(default_factory=list)


class MunicipalityFindings(BaseModel):
    research_type: Literal["PW-3"] = "PW-3"
    leadership: list[LeadershipPerson] = Field(default_factory=list)
    active_or_stalled_projects: list[MunicipalProject] = Field(default_factory=list)
    recent_budget_news: list[Claim] = Field(default_factory=list)
    environmental_issues: list[EnvironmentalIssue] = Field(default_factory=list)
    commission_makeup: list[CommissionMember] = Field(default_factory=list)
    peer_comparables: list[Claim] = Field(default_factory=list)


# =============================================================================
# S3-PREP — Pre-Meeting Research Package (Intake Meeting)
# =============================================================================

class ChampionProfile(BaseModel):
    name: str
    role: str
    background_summary: str
    public_statements: list[Claim] = Field(default_factory=list)
    prior_environmental_involvement: list[Claim] = Field(default_factory=list)


class DiscoveryQuestion(BaseModel):
    question: str
    rationale: str


class IntakePrepFindings(BaseModel):
    research_type: Literal["S3-PREP"] = "S3-PREP"
    municipality_overview: str
    champion_profile: ChampionProfile
    project_intelligence: list[Claim] = Field(default_factory=list)
    funding_landscape_summary: str
    political_landscape_summary: str
    tailored_discovery_questions: list[DiscoveryQuestion] = Field(
        min_length=3, max_length=7,
    )
    custom_chawq_intro: str = Field(
        description="2-sentence intro tuned for this audience",
    )


# =============================================================================
# S8-1 — Political Landscape Mapping
# =============================================================================

RiskLevel = Literal["low", "medium", "high"]


class CommissionerProfile(BaseModel):
    name: str
    seat: str | None = None
    risk_level: RiskLevel
    most_likely_objection: str
    most_effective_counter: str
    sources: list[Source] = Field(default_factory=list)


class ActionItem(BaseModel):
    week: int = Field(ge=1, le=8)
    owner: Literal["c-hawq", "champion", "shared"]
    task: str


class CommunityVoice(BaseModel):
    name_or_org: str
    leverage_summary: str
    suggested_action: str


class PoliticalFindings(BaseModel):
    research_type: Literal["S8-1"] = "S8-1"
    commissioner_profiles: list[CommissionerProfile] = Field(min_length=1)
    three_week_action_plan: list[ActionItem]
    high_leverage_community_voices: list[CommunityVoice] = Field(default_factory=list)
    top_derailers_and_mitigations: list[Risk] = Field(min_length=1, max_length=5)


# =============================================================================
# LOBBY-1 — New Jurisdiction Lobbyist Registration Research
# =============================================================================

JurisdictionType = Literal["city", "county", "wmd", "state"]


class LobbyistFindings(BaseModel):
    research_type: Literal["LOBBY-1"] = "LOBBY-1"
    jurisdiction_name: str
    jurisdiction_type: JurisdictionType
    registration_required: bool
    registration_form_url: HttpUrl | str | None = None
    submission_office: str | None = None
    submission_method: str | None = None
    fees: str | None = None
    timing_requirement: str
    reporting_requirements: list[str] = Field(default_factory=list)
    county_covers_municipalities: bool | None = None
    sources: list[Source] = Field(min_length=1)


# =============================================================================
# PW-1 — Conference Attendee Research
# =============================================================================

class PriorityContact(BaseModel):
    name: str
    title: str
    organization: str
    why_priority: str = Field(description="Why C-HAWQ should prioritize this contact")
    relevance_to_chawq: Literal["high", "medium", "low"]
    suggested_opener: str = Field(description="One-sentence personalized opener")
    sources: list[Source] = Field(min_length=1)


class ConferenceSession(BaseModel):
    title: str
    presenter: str | None = None
    time: str | None = None
    why_attend: str
    sources: list[Source] = Field(default_factory=list)


class ReferralPartnerCandidate(BaseModel):
    name_or_org: str
    domain: str = Field(description="e.g., engineering firm, nonprofit, academic")
    why_potential_partner: str
    sources: list[Source] = Field(default_factory=list)


class ConferenceAttendeeFindings(BaseModel):
    research_type: Literal["PW-1"] = "PW-1"
    conference_name: str
    top_priority_contacts: list[PriorityContact] = Field(min_length=1, max_length=15)
    sessions_to_attend: list[ConferenceSession] = Field(default_factory=list)
    referral_partner_candidates: list[ReferralPartnerCandidate] = Field(default_factory=list)
    pre_conference_outreach_drafts: list[str] = Field(default_factory=list)


# =============================================================================
# S1-2 — LinkedIn Research (per-contact, manual)
# =============================================================================

class LinkedInFindings(BaseModel):
    research_type: Literal["S1-2"] = "S1-2"
    contact_name: str
    common_ground_hooks: list[Claim] = Field(min_length=1)
    environmental_signals: list[Claim] = Field(default_factory=list)
    connection_request_message: str = Field(max_length=300)
    follow_up_message_if_no_reply: str


# =============================================================================
# S1-4 — Full Internet Research on a New Contact
# =============================================================================

class ProfessionalRole(BaseModel):
    title: str
    organization: str
    start_year: int | None = None
    end_year: int | None = None
    notes: str | None = None
    sources: list[Source] = Field(min_length=1)


class PublicStatement(BaseModel):
    statement: str
    venue: str
    date: str | None = None
    relevance: Literal["water_quality", "habitat", "stormwater", "funding",
                       "p3", "general_environment", "other"]
    sources: list[Source] = Field(min_length=1)


class MutualConnection(BaseModel):
    name: str
    relationship: str
    intro_potential: Literal["strong", "warm", "cold"]
    sources: list[Source] = Field(default_factory=list)


class ContactBackgroundFindings(BaseModel):
    research_type: Literal["S1-4"] = "S1-4"
    contact_name: str
    current_role: ProfessionalRole
    professional_history: list[ProfessionalRole] = Field(default_factory=list)
    tenure_in_current_role_years: float | None = None
    public_statements_on_environment: list[PublicStatement] = Field(default_factory=list)
    projects_or_initiatives_led: list[Claim] = Field(default_factory=list)
    municipality_environmental_challenges: list[Claim] = Field(default_factory=list)
    mutual_connections: list[MutualConnection] = Field(default_factory=list)
    personalized_opening_line: str


# =============================================================================
# S3-3 — Commission Meeting Preparation Research
# =============================================================================

class AgendaItemImpact(BaseModel):
    agenda_item: str
    item_number: str | None = None
    affects_chawq_project: bool
    impact_description: str
    sources: list[Source] = Field(default_factory=list)


class CommissionerBriefShort(BaseModel):
    name: str
    likely_position_on_relevant_items: str
    talking_points_to_emphasize: list[str] = Field(default_factory=list)
    talking_points_to_avoid: list[str] = Field(default_factory=list)


class CommissionMeetingPrepFindings(BaseModel):
    research_type: Literal["S3-3"] = "S3-3"
    municipality_name: str
    meeting_date: str
    meeting_goal: Literal["observe", "present", "support_vote",
                          "gather_intel", "champion_handoff"]
    agenda_summary_plain_language: str
    agenda_items_affecting_chawq: list[AgendaItemImpact] = Field(default_factory=list)
    per_commissioner_briefs: list[CommissionerBriefShort] = Field(default_factory=list)
    anticipated_objections: list[Claim] = Field(default_factory=list)
    things_not_to_say: list[str] = Field(default_factory=list)


# =============================================================================
# S4-DECK — Custom Deck Research Brief
# =============================================================================

class SatelliteImagerySource(BaseModel):
    waterbody_or_location: str
    imagery_provider: str
    url: HttpUrl | str
    suggested_view: str
    sources: list[Source] = Field(default_factory=list)


class LocalizedDataPoint(BaseModel):
    metric: str
    value: str
    location: str
    year: int
    interpretation: str
    sources: list[Source] = Field(min_length=1)


class ComparableProjectExample(BaseModel):
    project_name: str
    municipality: str
    year: int
    cost_usd: int | None = None
    outcome_summary: str
    why_relevant: str
    visual_assets_url: HttpUrl | str | None = None
    sources: list[Source] = Field(min_length=1)


class HumanScaleStatistic(BaseModel):
    statistic: str
    context: str
    sources: list[Source] = Field(min_length=1)


class VisualStorytellingSuggestion(BaseModel):
    slide_concept: str
    description: str
    suggested_visual_assets: list[str] = Field(default_factory=list)


class DeckResearchFindings(BaseModel):
    research_type: Literal["S4-DECK"] = "S4-DECK"
    municipality_name: str
    project_focus: str
    satellite_imagery_sources: list[SatelliteImagerySource] = Field(default_factory=list)
    localized_environmental_data: list[LocalizedDataPoint] = Field(default_factory=list)
    comparable_project_examples: list[ComparableProjectExample] = Field(min_length=1, max_length=5)
    human_scale_statistics: list[HumanScaleStatistic] = Field(default_factory=list)
    visual_storytelling_suggestions: list[VisualStorytellingSuggestion] = Field(min_length=2, max_length=8)


# =============================================================================
# Discriminated union — Pydantic auto-routes by research_type literal
# =============================================================================

Findings = Annotated[
    Union[
        GrantFindings,                      # S6-1
        MunicipalityFindings,               # PW-3
        IntakePrepFindings,                 # S3-PREP
        PoliticalFindings,                  # S8-1
        LobbyistFindings,                   # LOBBY-1
        ConferenceAttendeeFindings,         # PW-1
        LinkedInFindings,                   # S1-2
        ContactBackgroundFindings,          # S1-4
        CommissionMeetingPrepFindings,      # S3-3
        DeckResearchFindings,               # S4-DECK
    ],
    Field(discriminator="research_type"),
]


# =============================================================================
# The envelope
# =============================================================================

class ResearchBrief(BaseModel):
    """The canonical output of any Research Agent run."""

    research_type_id: str = Field(
        description="Research type, e.g. 'S6-1'. Must equal findings.research_type.",
    )
    prompt_version: int = Field(ge=1)
    run_id: str = Field(description="UUID generated at run start")

    contact_id: str | None = Field(default=None, description="GHL contact id")
    municipality_name: str | None = None
    triggering_event: str | None = Field(
        default=None,
        description="GHL event that fired this run (e.g. 'boil_contact_created')",
    )

    generated_at: datetime

    overall_confidence: float = Field(
        ge=0.0, le=1.0,
        description="Holistic confidence. <0.5 should be flagged for review.",
    )

    claims: list[Claim] = Field(
        default_factory=list,
        description="Cross-cutting factual claims supporting findings.",
    )

    findings: Findings

    sources_consulted: list[Source] = Field(
        default_factory=list,
        description="All sources the agent accessed, including non-cited ones.",
    )
    notes: str | None = Field(
        default=None,
        description="Free-form caveats or context the agent wants to surface.",
    )

    @model_validator(mode="after")
    def _research_type_matches_findings(self) -> "ResearchBrief":
        if self.research_type_id != self.findings.research_type:
            raise ValueError(
                f"research_type_id {self.research_type_id!r} does not match "
                f"findings.research_type {self.findings.research_type!r}"
            )
        return self


# =============================================================================
# Helpers used by runner and prompt builder
# =============================================================================

_FINDINGS_TYPE_MAP: dict[str, type] = {
    "S6-1":    GrantFindings,
    "PW-3":    MunicipalityFindings,
    "S3-PREP": IntakePrepFindings,
    "S8-1":    PoliticalFindings,
    "LOBBY-1": LobbyistFindings,
    "PW-1":    ConferenceAttendeeFindings,
    "S1-2":    LinkedInFindings,
    "S1-4":    ContactBackgroundFindings,
    "S3-3":    CommissionMeetingPrepFindings,
    "S4-DECK": DeckResearchFindings,
}


def json_schema_for_type(research_type_id: str) -> dict[str, Any]:
    """ResearchBrief schema narrowed to one research type.

    Injects only the $defs needed for the specific findings type, keeping the
    system prompt schema block ~70-80% smaller than the full 10-type union.
    Falls back to the full schema for unknown types.
    """
    from pydantic import create_model

    findings_cls = _FINDINGS_TYPE_MAP.get(research_type_id)
    if findings_cls is None:
        return json_schema_for_prompt()

    NarrowBrief = create_model(
        "ResearchBrief",
        research_type_id=(str, Field(description="Must equal findings.research_type")),
        prompt_version=(int, Field(ge=1)),
        run_id=(str, Field(description="UUID")),
        contact_id=(str | None, Field(default=None, description="GHL contact id")),
        municipality_name=(str | None, Field(default=None)),
        triggering_event=(str | None, Field(default=None)),
        generated_at=(datetime, ...),
        overall_confidence=(float, Field(ge=0.0, le=1.0,
                                         description="0-1. <0.5 flag for review.")),
        claims=(list[Claim], Field(default_factory=list)),
        findings=(findings_cls, ...),
        sources_consulted=(list[Source], Field(default_factory=list)),
        notes=(str | None, Field(default=None)),
    )
    return NarrowBrief.model_json_schema()


def json_schema_for_prompt() -> dict[str, Any]:
    """Full ResearchBrief schema with all 10 research types (used as fallback)."""
    return ResearchBrief.model_json_schema()


def parse_response(raw: str) -> ResearchBrief:
    """Validate Claude's JSON response. Raises ValidationError on schema violations."""
    return ResearchBrief.model_validate_json(raw)
