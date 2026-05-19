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
        description=(
            "0.0-1.0 source reliability. Use these anchors:\n"
            "  0.90-0.95: official statute, regulation, court ruling, or the "
            "jurisdiction's OWN clerk/agency page documenting its own rules. "
            "A municipality's own .gov website IS authoritative for its own "
            "rules — even small towns. Size of jurisdiction does not lower "
            "the score.\n"
            "  0.80-0.90: other .gov pages, .edu peer-reviewed publications, "
            "primary agency datasets (FDEP, USGS, NOAA).\n"
            "  0.60-0.80: established news (Tampa Bay Times, Miami Herald), "
            "trade press (Florida Trend, Governing).\n"
            "  0.40-0.60: opinion pieces, advocacy-org summaries, secondary "
            "compilations, .org explainers that don't link to a primary source.\n"
            "  <0.40: blogs, forums, social-media posts, AI-generated summaries "
            "without a verifiable upstream source. Claims supported only by "
            "sources at this level will be rejected — re-source or drop the claim."
        ),
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



class ConferenceAttendeeFindings(BaseModel):
    research_type: Literal["PW-1"] = "PW-1"
    conference_name: str
    top_priority_contacts: list[PriorityContact] = Field(min_length=1, max_length=5)


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
# S7-1 — Post-Event Debrief
# =============================================================================

class FollowUpEmailStarter(BaseModel):
    contact_name: str
    contact_role_or_affiliation: str
    suggested_subject: str
    email_starter: str = Field(
        description="2-3 personalized opening sentences referencing something specific from the conversation",
    )


class NextAction(BaseModel):
    action: str
    owner: Literal["c-hawq", "champion", "shared"]
    deadline_hours: int = Field(
        ge=1,
        description="Hours from event end by which this action must be completed",
    )


class EventImprovementSuggestion(BaseModel):
    observation: str
    suggested_improvement: str


class PostEventDebriefFindings(BaseModel):
    research_type: Literal["S7-1"] = "S7-1"
    internal_summary: str = Field(
        description="2-3 paragraph summary for the C-HAWQ team — what happened, who engaged, what it means",
    )
    follow_up_email_starters: list[FollowUpEmailStarter] = Field(
        min_length=1, max_length=5,
    )
    next_48h_actions: list[NextAction] = Field(min_length=1)
    next_event_improvements: list[EventImprovementSuggestion] = Field(
        default_factory=list,
    )


# =============================================================================
# S8-2 — Community Support Letter
# =============================================================================

class CommunityLetterFindings(BaseModel):
    research_type: Literal["S8-2"] = "S8-2"
    support_letter: str = Field(
        description="Full letter template (max 1 page) for organizations to sign and submit to the commission",
    )
    public_comment_statement: str = Field(
        description="2-3 minute spoken adaptation for commission meeting public comment",
    )


# =============================================================================
# S8-3 — Politician-Friendly Briefing
# =============================================================================

class TalkingPointSlide(BaseModel):
    slide_number: int = Field(ge=1, le=5)
    title: str
    talking_points: list[str] = Field(min_length=1, max_length=4)


class PoliticalObjectionResponse(BaseModel):
    objection: str
    response: str


class PoliticianBriefingFindings(BaseModel):
    research_type: Literal["S8-3"] = "S8-3"
    five_slide_outline: list[TalkingPointSlide] = Field(min_length=5, max_length=5)
    political_objection_responses: list[PoliticalObjectionResponse] = Field(
        min_length=3, max_length=3,
    )
    local_angle: str = Field(
        description="Compelling sourced statistic or story tied to this official's district or constituents",
    )
    leave_behind: str = Field(
        description="~300-400 word plain-language one-pager written for a generalist politician",
    )
    suggested_ask: str = Field(
        description="One specific, low-friction next step to request by end of the 10-minute meeting",
    )


# =============================================================================
# S5-1 — Internal Presentation Prep
# =============================================================================

class ObjectionResponse(BaseModel):
    objection: str
    likely_source: str | None = Field(
        default=None,
        description="Department or role most likely to raise this objection",
    )
    response: str


class DepartmentInterest(BaseModel):
    department: str
    primary_concerns: list[Literal[
        "budget", "liability", "public_perception",
        "logistics", "staffing", "regulatory",
    ]]
    key_talking_points: list[str] = Field(min_length=1, max_length=4)


class PresentationDataVisual(BaseModel):
    description: str
    why_effective_for_this_audience: str


class InternalPresentationPrepFindings(BaseModel):
    research_type: Literal["S5-1"] = "S5-1"
    opening_narrative: str = Field(
        description="Science story in human terms, specific to this community",
    )
    anticipated_objections: list[ObjectionResponse] = Field(min_length=3, max_length=7)
    department_interests: list[DepartmentInterest] = Field(min_length=1)
    closing_statement: str
    recommended_data_visuals: list[PresentationDataVisual] = Field(
        min_length=2, max_length=4,
    )


# =============================================================================
# S5-2 — Post-Internal Meeting Debrief
# =============================================================================

class StakeholderRead(BaseModel):
    name_or_role: str
    disposition: Literal["ally", "skeptic", "neutral", "unknown"]
    rationale: str


class InternalBlocker(BaseModel):
    concern: str
    likely_owner: str | None = None
    recommended_resolution: str


class EmailDraft(BaseModel):
    subject: str
    body: str


class PostMeetingDebriefFindings(BaseModel):
    research_type: Literal["S5-2"] = "S5-2"
    stakeholder_reads: list[StakeholderRead] = Field(min_length=1)
    key_concerns: list[InternalBlocker] = Field(default_factory=list)
    thank_you_email: EmailDraft
    recommended_next_action: str
    remaining_blockers_before_step_6: list[str] = Field(default_factory=list)


# =============================================================================
# S6-2 — Project Narrative Draft
# =============================================================================

class UrgentStatistic(BaseModel):
    statistic: str
    human_framing: str = Field(
        description="Plain-language translation a non-scientist emotionally connects with",
    )
    sources: list[Source] = Field(min_length=1)


class ProjectNarrativeFindings(BaseModel):
    research_type: Literal["S6-2"] = "S6-2"
    executive_summary: str = Field(
        description="~400-600 words, plain language, written for a commissioner reading once before a vote",
    )
    grant_narrative: str = Field(
        description="~300 words, written for a federal or state grant reviewer",
    )
    urgent_statistics: list[UrgentStatistic] = Field(min_length=3, max_length=5)
    before_after_story: str = Field(
        description="150-250 words, general public audience, specific to this place",
    )


# =============================================================================
# S6-3 — Commission Presentation Prep
# =============================================================================

class CommissionObjection(BaseModel):
    objection: str
    commissioner_type_likely: str | None = Field(
        default=None,
        description="Type of commissioner most likely to raise this (e.g., fiscal hawk, skeptic)",
    )
    response: str


class CommissionPresentationPrepFindings(BaseModel):
    research_type: Literal["S6-3"] = "S6-3"
    opening_script: str = Field(
        description="Verbatim 5-minute opening, first person as Emily speaks it",
    )
    top_objections: list[CommissionObjection] = Field(min_length=3, max_length=7)
    things_not_to_say: list[str] = Field(min_length=3, max_length=7)
    closing_statement: str
    why_chawq_answer: str = Field(
        description="Prepared response to 'Why C-HAWQ and not a normal engineering firm?'",
    )


# =============================================================================
# S9-1 — Project Kickoff Deck
# =============================================================================

class KickoffDeckSlide(BaseModel):
    slide_number: int = Field(ge=1, le=10)
    section_title: str
    bullet_points: list[str] = Field(min_length=2, max_length=6)
    speaker_notes: str


class KickoffDeckFindings(BaseModel):
    research_type: Literal["S9-1"] = "S9-1"
    slides: list[KickoffDeckSlide] = Field(min_length=7, max_length=10)
    open_items_register: list[str] = Field(
        default_factory=list,
        description="Unresolved items that need an owner and deadline assigned at the kickoff meeting",
    )


# =============================================================================
# S9-2 — Media and Reporter Research
# =============================================================================

MediaOutletType = Literal[
    "local_news", "regional_environmental", "municipal_trade", "wire_service",
]


class MediaOutlet(BaseModel):
    outlet_name: str
    outlet_type: MediaOutletType
    coverage_area: str
    relevant_beat: str | None = None
    sources: list[Source] = Field(min_length=1)


class BeatReporter(BaseModel):
    name: str
    outlet: str
    beat: str
    recent_relevant_byline: str | None = None
    sources: list[Source] = Field(min_length=1)


class MediaPitchEmail(BaseModel):
    target_outlet_or_reporter: str
    subject: str
    body: str = Field(description="150 words max; written from the Champion's office")


class MediaResearchFindings(BaseModel):
    research_type: Literal["S9-2"] = "S9-2"
    local_outlets: list[MediaOutlet] = Field(default_factory=list)
    regional_environmental_press: list[MediaOutlet] = Field(default_factory=list)
    municipal_trade_press: list[MediaOutlet] = Field(default_factory=list)
    beat_reporters: list[BeatReporter] = Field(default_factory=list)
    wire_syndication_assessment: str
    pitch_emails: list[MediaPitchEmail] = Field(min_length=1, max_length=3)


# =============================================================================
# S9-3 — Grant Compliance Checklist
# =============================================================================

class ComplianceRequirement(BaseModel):
    requirement: str
    timing: str = Field(description="When this is due — e.g., 'quarterly', 'at 6 months', 'at closeout'")
    documentation_needed: list[str] = Field(min_length=1)


class GrantCompliancePitfall(BaseModel):
    pitfall: str
    prevention: str


class GrantComplianceFindings(BaseModel):
    research_type: Literal["S9-3"] = "S9-3"
    program_requirements_summary: str = Field(
        description="2-3 paragraphs: reporting structure, key obligations, hot spots",
    )
    compliance_checklist: list[ComplianceRequirement] = Field(min_length=1)
    day_one_records: list[str] = Field(min_length=1)
    common_pitfalls: list[GrantCompliancePitfall] = Field(min_length=3, max_length=7)
    six_month_checkin_email: EmailDraft


# =============================================================================
# S9-4 — P3 Proposal & RFP Drafting Assistant
# =============================================================================

class ProposalSection(BaseModel):
    section_title: str
    content: str


class P3ComplexityFactor(BaseModel):
    factor: str
    special_contract_language_needed: str


class GCEvaluationCriterion(BaseModel):
    criterion: str
    weight_or_priority: Literal["critical", "high", "medium"]
    rationale: str


class ProcurementConflict(BaseModel):
    standard_term: str
    conflict_description: str
    suggested_alternative_language: str


class P3ProposalFindings(BaseModel):
    research_type: Literal["S9-4"] = "S9-4"
    proposal_sections: list[ProposalSection] = Field(min_length=7)
    complexity_factors: list[P3ComplexityFactor] = Field(min_length=3, max_length=5)
    gc_evaluation_criteria: list[GCEvaluationCriterion] = Field(
        min_length=5, max_length=5,
    )
    procurement_conflicts: list[ProcurementConflict] = Field(default_factory=list)


# =============================================================================
# S9-5 — Partnership Agreement Plain-Language Summary
# =============================================================================

class AgreementRiskFlag(BaseModel):
    term_or_section: str
    concern: str
    severity: Literal["low", "medium", "high"]


class AttorneyQuestion(BaseModel):
    question: str
    why_important: str


ComplexityFactorStatus = Literal["adequately_addressed", "partially_addressed", "not_addressed"]


class ComplexityFactorCoverage(BaseModel):
    factor: str
    status: ComplexityFactorStatus
    notes: str | None = None


class AgreementSummaryFindings(BaseModel):
    research_type: Literal["S9-5"] = "S9-5"
    plain_language_summary: str = Field(
        description="2-minute read for a non-attorney commissioner",
    )
    risk_flags: list[AgreementRiskFlag] = Field(default_factory=list)
    attorney_questions: list[AttorneyQuestion] = Field(min_length=1)
    one_sided_terms: list[str] = Field(default_factory=list)
    complexity_factors_covered: list[ComplexityFactorCoverage] = Field(min_length=4, max_length=4)


# =============================================================================
# S10-1 — Project Case Study
# =============================================================================

class CaseStudyFindings(BaseModel):
    research_type: Literal["S10-1"] = "S10-1"
    leave_behind_case_study: str = Field(
        description="~400-500 words, professional, outcome-focused, for a city manager reading in 90 seconds",
    )
    website_highlight: str = Field(
        description="~300 words, third person, human story first, suitable for chawq.org",
    )
    press_pitch: str = Field(
        description="2 paragraphs explaining why this project is worth covering nationally",
    )
    pull_quotes: list[str] = Field(
        min_length=3, max_length=3,
        description="Exactly 3 headline-worthy, self-contained sentences",
    )
    before_after_narrative: str = Field(
        description="150-250 words, general public audience, specific to this place",
    )


# =============================================================================
# S10-2 — Referral Outreach Research
# =============================================================================

class ReferralResearchFindings(BaseModel):
    research_type: Literal["S10-2"] = "S10-2"
    contact_overview: str = Field(
        description="2-3 paragraphs on who this person or organization is and their environmental challenges",
    )
    recent_projects_or_proposals: list[Claim] = Field(default_factory=list)
    warm_intro_email: EmailDraft
    best_chawq_talking_point: str = Field(
        description="Single most compelling hook for this specific audience, grounded in their challenges",
    )


# =============================================================================
# S4-LETTER — Champion Briefing Letter (post-intake, pre-deck)
# =============================================================================

class ChampionBriefingLetterFindings(BaseModel):
    research_type: Literal["S4-LETTER"] = "S4-LETTER"
    subject_line: str = Field(description="Email subject referencing the project and meeting")
    briefing_letter: str = Field(
        description=(
            "~300-400 word ready-to-send letter from C-HAWQ to the Champion. "
            "Opens with appreciation, recaps the vision, confirms the problem, "
            "outlines next steps, ends with a clear call to action."
        ),
    )
    key_project_framing: str = Field(
        description="2-3 sentences (internal use) capturing how C-HAWQ will position this project to funders",
    )
    agreed_next_steps: list[str] = Field(
        min_length=1,
        description="Concrete commitments both parties made at the intake meeting",
    )


# =============================================================================
# S7-PLAN — Community Event Plan (pre-event, before S7-1 debrief)
# =============================================================================

class CommunityPartner(BaseModel):
    name: str
    partner_type: str = Field(description="e.g., conservation org, HOA, neighborhood association")
    role_in_event: str
    contact_notes: str | None = None


class VolunteerRole(BaseModel):
    role: str
    count_needed: int = Field(ge=1)
    responsibilities: list[str] = Field(min_length=1)


class EventSegment(BaseModel):
    time_slot: str = Field(description="e.g., '9:00–9:30 AM'")
    activity: str
    lead: Literal["c-hawq", "municipality", "partner", "volunteer"]
    materials_needed: list[str] = Field(default_factory=list)


class CommunityEventPlanFindings(BaseModel):
    research_type: Literal["S7-PLAN"] = "S7-PLAN"
    event_overview: str = Field(
        description="2 paragraphs: what kind of event fits this community and what success looks like",
    )
    community_partners: list[CommunityPartner] = Field(
        min_length=1,
        description="Real organizations identified by research",
    )
    volunteer_framework: list[VolunteerRole] = Field(default_factory=list)
    event_run_of_show: list[EventSegment] = Field(
        min_length=3,
        description="Timed segments from setup through breakdown",
    )
    outreach_channels: list[str] = Field(
        min_length=2,
        description="Specific channels for this municipality — local Facebook groups, NextDoor, HOA lists, etc.",
    )
    permits_or_approvals_needed: list[str] = Field(default_factory=list)
    success_metrics: list[str] = Field(
        min_length=2,
        description="Measurable outcomes to track on the day",
    )


# =============================================================================
# Discriminated union — Pydantic auto-routes by research_type literal
# =============================================================================

Findings = Annotated[
    Union[
        GrantFindings,                          # S6-1
        MunicipalityFindings,                   # PW-3
        IntakePrepFindings,                     # S3-PREP
        PoliticalFindings,                      # S8-1
        LobbyistFindings,                       # LOBBY-1
        ConferenceAttendeeFindings,             # PW-1
        LinkedInFindings,                       # S1-2
        ContactBackgroundFindings,              # S1-4
        CommissionMeetingPrepFindings,          # S3-3
        DeckResearchFindings,                   # S4-DECK
        ChampionBriefingLetterFindings,         # S4-LETTER
        InternalPresentationPrepFindings,       # S5-1
        PostMeetingDebriefFindings,             # S5-2
        ProjectNarrativeFindings,               # S6-2
        CommissionPresentationPrepFindings,     # S6-3
        CommunityEventPlanFindings,             # S7-PLAN
        PostEventDebriefFindings,               # S7-1
        CommunityLetterFindings,                # S8-2
        PoliticianBriefingFindings,             # S8-3
        KickoffDeckFindings,                    # S9-1
        MediaResearchFindings,                  # S9-2
        GrantComplianceFindings,                # S9-3
        P3ProposalFindings,                     # S9-4
        AgreementSummaryFindings,               # S9-5
        CaseStudyFindings,                      # S10-1
        ReferralResearchFindings,               # S10-2
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
    "S6-1":     GrantFindings,
    "PW-3":     MunicipalityFindings,
    "S3-PREP":  IntakePrepFindings,
    "S8-1":     PoliticalFindings,
    "LOBBY-1":  LobbyistFindings,
    "PW-1":     ConferenceAttendeeFindings,
    "S1-2":     LinkedInFindings,
    "S1-4":     ContactBackgroundFindings,
    "S3-3":     CommissionMeetingPrepFindings,
    "S4-DECK":  DeckResearchFindings,
    "S4-LETTER": ChampionBriefingLetterFindings,
    "S5-1":     InternalPresentationPrepFindings,
    "S5-2":     PostMeetingDebriefFindings,
    "S6-2":     ProjectNarrativeFindings,
    "S6-3":     CommissionPresentationPrepFindings,
    "S7-PLAN":  CommunityEventPlanFindings,
    "S7-1":     PostEventDebriefFindings,
    "S8-2":     CommunityLetterFindings,
    "S8-3":     PoliticianBriefingFindings,
    "S9-1":     KickoffDeckFindings,
    "S9-2":     MediaResearchFindings,
    "S9-3":     GrantComplianceFindings,
    "S9-4":     P3ProposalFindings,
    "S9-5":     AgreementSummaryFindings,
    "S10-1":    CaseStudyFindings,
    "S10-2":    ReferralResearchFindings,
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
