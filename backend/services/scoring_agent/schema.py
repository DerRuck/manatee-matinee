"""
Pydantic v2 schema for the Scoring Agent.

The Scoring Agent reads every signal we have about a contact (Firestore
contact record, agent_runs history, recent communications, GHL tags) and
emits a structured assessment of where they are in C-HAWQ's Proven
Process and how hot the lead is.

Two distinct scores per the team's design discussion (transcript 2026-05-22):

  step_confidence  : float 0-1
      How sure the model is about its proven-process step placement.
      Used by the workbook UI to decide whether to flag for human review.

  lead_heat_score  : int 0-100
      The actual lead priority — independent of the model's confidence.
      Used to sort contacts in the workbook so staff work hottest leads first.

These are independent: a model can be VERY confident a lead is COLD
(high step_confidence, low lead_heat_score) or unsure where a hot lead is
in the pipeline (low step_confidence, high lead_heat_score).

Architecture mirrors services/research_agent/schema.py:

  - ScoringResult     : the strict envelope every run returns
  - PipelineScore...  : findings types — discriminated union by score_type_id
        PipelineScoreFindings  -> PIPELINE-SCORE  (default proven-process scoring)

Adding a new score type = add a new findings sub-model with
`score_type: Literal["..."]` and register in _FINDINGS_TYPE_MAP / Findings union.

Used by:
  - services/scoring_agent/runner.py    -> validates Claude's JSON response
  - services/scoring_agent/drive_sync.py (optional) -> renders a .docx review
  - The workbook UI reads ScoringResult.findings directly via Firestore
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, model_validator


# =============================================================================
# Vocabulary
# =============================================================================

# C-HAWQ's Boil/Simmer/Stall lead-heat framework (Marketing Guide §Module C).
LeadHeat = Literal["boil", "simmer", "stall", "cold", "won", "lost"]
#
#   boil    : hot — active project, real authority, momentum. Work today.
#   simmer  : warm — interested but not ready for full Step 4 commitment.
#   stall   : was warm, now blocked. Triage to re-engage or de-prioritize.
#   cold    : never engaged or went dark with no recent signal.
#   won     : project signed (Step 9+) — keep scoring for relationship maintenance.
#   lost    : explicit no-go. Do not contact.

# Boil-priority qualifying criteria from the binder (Step 3 Go/No-Go scorecard).
# A "yes" on most of these moves a lead from simmer → boil.
BoilCriterionKey = Literal[
    "has_real_authority",            # decision-maker (city/county manager, director, mayor)
    "specific_real_project_exists",  # not just "interest in water quality"
    "project_is_stalled_solvable",   # blocked by funding / engineering / political
    "champion_has_personal_passion", # not just professional duty
    "aligns_with_chawq_focus",       # coastal habitat, seagrass, water quality, canals
]


# =============================================================================
# Signals & blockers — building blocks for findings
# =============================================================================

class Signal(BaseModel):
    """One piece of evidence that influenced the score.

    Every score MUST be backed by at least one signal so the team can trust
    (and override) the model's reasoning in the workbook UI.
    """
    description: str = Field(
        min_length=1,
        description="What the signal is — one sentence the workbook UI can render.",
    )
    evidence_source: str = Field(
        min_length=1,
        description=(
            "Where it came from — e.g. 'firestore:contacts/{id}.tags', "
            "'agent_runs/{run_id}', 'plaud_transcript_2026-04-15', 'email_thread:abc'. "
            "Keep precise enough that a human can verify."
        ),
    )
    impact: Literal["positive", "negative", "neutral"] = Field(
        description="positive = moves toward boil/advance; negative = pulls toward stall.",
    )
    weight: float = Field(
        default=0.5, ge=0.0, le=1.0,
        description="How heavily this signal counted, 0-1.",
    )


class Blocker(BaseModel):
    """Something preventing advancement to the next proven-process step.

    Surfaced separately from negative signals because the workbook UI uses
    these to suggest follow-up actions.
    """
    description: str = Field(min_length=1)
    owner: str | None = Field(
        default=None,
        description="Who should resolve it — 'C-HAWQ', 'champion', 'commission', or a named person.",
    )
    severity: Literal["low", "medium", "high"] = "medium"


class RecommendedAction(BaseModel):
    """One concrete next step the model recommends.

    The workbook UI renders these as a checklist staff can act on. Keep each
    action specific and verifiable — 'send Template 2A to champion' beats
    'engage more'.
    """
    action: str = Field(min_length=1)
    owner: str = Field(
        min_length=1,
        description="Who does it — 'Emily', 'Logan', 'Ryan', 'AI:email_drafter', etc.",
    )
    due_within_days: int = Field(
        ge=0, le=90,
        description="Target turnaround. 0 = today.",
    )
    proven_process_step: int | None = Field(
        default=None, ge=1, le=10,
        description="Which step this action belongs to, when relevant.",
    )


class BoilCriterion(BaseModel):
    """One row of the binder's Boil-priority qualifying scorecard."""
    key: BoilCriterionKey
    answer: Literal["yes", "no", "unclear"]
    evidence: str | None = Field(
        default=None,
        description="One-sentence justification from the contact's record.",
    )


class GoNoGoScorecard(BaseModel):
    """The binder's 21-point Go/No-Go scorecard (Step 3 close-out).

    Auto-filled by the scoring agent when the contact has reached Step 3+
    so the workbook UI can display the scorecard the team would have
    filled in by hand. Skipped (None) for contacts not yet at Step 3.

    Each criterion is scored 1, 2, or 3 → max 21 points.
    """
    authority_score:    int = Field(ge=1, le=3, description="Real decision-making authority")
    project_specificity_score: int = Field(ge=1, le=3, description="Specific real project, not vague interest")
    solvability_score:  int = Field(ge=1, le=3, description="Stalled by a solvable problem (funding/engineering/political)")
    champion_passion_score: int = Field(ge=1, le=3, description="Champion has personal passion, not just duty")
    chawq_fit_score:    int = Field(ge=1, le=3, description="Project aligns with C-HAWQ's coastal/water focus")
    p3_candidacy_score: int = Field(ge=1, le=3, description="Strong P3 indicators (cost >$500K, multi-source funding, etc.)")
    political_readiness_score: int = Field(ge=1, le=3, description="Champion + commission appetite")

    @property
    def total(self) -> int:
        return (
            self.authority_score
            + self.project_specificity_score
            + self.solvability_score
            + self.champion_passion_score
            + self.chawq_fit_score
            + self.p3_candidacy_score
            + self.political_readiness_score
        )

    decision: Literal["GO", "CONDITIONAL_GO", "PAUSE", "NO_GO"] = Field(
        description="The team's Go/No-Go decision. Maps to total score: "
                    ">=17 GO, 13-16 CONDITIONAL_GO, 9-12 PAUSE, <9 NO_GO. "
                    "Model may deviate when justified."
    )
    rationale: str = Field(
        min_length=1,
        description="2-3 sentence justification for the decision.",
    )


# =============================================================================
# PIPELINE-SCORE — the default proven-process scoring
# =============================================================================

class PipelineScoreFindings(BaseModel):
    score_type: Literal["PIPELINE-SCORE"] = "PIPELINE-SCORE"

    # ---- Proven-process step placement -------------------------------------
    current_step: int = Field(
        ge=1, le=10,
        description="Which Proven Process step this contact is currently in (1-10).",
    )
    current_step_name: str = Field(
        min_length=1,
        description="Human-readable step label, e.g. 'Step 4: Schedule the Next Stage'.",
    )
    current_phase: int = Field(
        ge=1, le=3,
        description="Proven Process phase — 1: Discover/Qualify, 2: Develop, 3: Mobilize/Close.",
    )
    step_confidence: float = Field(
        ge=0.0, le=1.0,
        description=(
            "How sure the model is about the step placement. <0.5 flags for "
            "human review in the workbook UI. Independent of lead_heat_score."
        ),
    )

    # ---- Advancement readiness ---------------------------------------------
    ready_to_advance: bool = Field(
        description="Has the contact met the binder's checklist for moving to current_step + 1?",
    )
    next_step_blockers: list[Blocker] = Field(
        default_factory=list,
        description="What's keeping them from advancing. Empty when ready_to_advance=True.",
    )

    # ---- Lead heat (priority) ---------------------------------------------
    lead_heat: LeadHeat = Field(
        description="Boil/Simmer/Stall/Cold/Won/Lost — sorts contacts in the workbook.",
    )
    lead_heat_score: int = Field(
        ge=0, le=100,
        description=(
            "Numeric lead priority 0-100. Independent of step_confidence. "
            "Anchors: 90+ urgent, 70-89 hot, 40-69 warm, 20-39 cool, <20 cold."
        ),
    )
    boil_criteria: list[BoilCriterion] = Field(
        default_factory=list,
        description=(
            "The 5 boil-priority criteria from the binder. Required (all 5) "
            "once the contact has reached Step 3. Optional for Steps 1-2."
        ),
    )
    go_no_go: GoNoGoScorecard | None = Field(
        default=None,
        description="21-point scorecard. Required at Step 3 close-out, optional earlier.",
    )

    # ---- Evidence & next moves --------------------------------------------
    signals: list[Signal] = Field(
        min_length=1,
        description="At least one signal must back the score. Cited evidence is non-negotiable.",
    )
    recommended_actions: list[RecommendedAction] = Field(
        default_factory=list,
        description="Concrete next steps the workbook UI renders as a checklist.",
    )

    # ---- Summary line (workbook UI primary surface) -----------------------
    summary_one_line: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "The one-line digest the workbook UI shows next to the contact name. "
            "Example: 'Step 4, ready to advance — Champion confirmed June 12 site walk.'"
        ),
    )

    # ---- Stall / dormancy --------------------------------------------------
    days_since_last_signal: int | None = Field(
        default=None, ge=0,
        description=(
            "Days since the most recent inbound signal from the contact. "
            "Trigger for stall detection — >30 days at Step 1-4 typically warrants follow-up."
        ),
    )

    @model_validator(mode="after")
    def _boil_criteria_required_at_step_3_plus(self) -> "PipelineScoreFindings":
        if self.current_step >= 3 and len(self.boil_criteria) == 0:
            raise ValueError(
                "boil_criteria must be filled for contacts at Step 3 or later. "
                f"current_step={self.current_step}"
            )
        return self

    @model_validator(mode="after")
    def _no_blockers_when_ready(self) -> "PipelineScoreFindings":
        if self.ready_to_advance and self.next_step_blockers:
            raise ValueError(
                "next_step_blockers must be empty when ready_to_advance=True. "
                f"Got {len(self.next_step_blockers)} blockers."
            )
        if not self.ready_to_advance and not self.next_step_blockers:
            raise ValueError(
                "next_step_blockers must contain at least one entry when "
                "ready_to_advance=False — explain what's blocking advancement."
            )
        return self

    @model_validator(mode="after")
    def _lead_heat_score_matches_band(self) -> "PipelineScoreFindings":
        # Coarse sanity check: explicit 'cold' shouldn't carry a 90+ score, etc.
        if self.lead_heat == "cold" and self.lead_heat_score >= 40:
            raise ValueError(
                f"lead_heat='cold' but lead_heat_score={self.lead_heat_score} — "
                "cold leads must score below 40."
            )
        if self.lead_heat == "boil" and self.lead_heat_score < 70:
            raise ValueError(
                f"lead_heat='boil' but lead_heat_score={self.lead_heat_score} — "
                "boil leads must score 70 or higher."
            )
        if self.lead_heat == "lost" and self.lead_heat_score >= 20:
            raise ValueError(
                f"lead_heat='lost' but lead_heat_score={self.lead_heat_score} — "
                "lost leads must score below 20."
            )
        return self


# =============================================================================
# Discriminated union
# =============================================================================

Findings = Annotated[
    Union[
        PipelineScoreFindings,
    ],
    Field(discriminator="score_type"),
]


# =============================================================================
# Envelope
# =============================================================================

ScoreTrigger = Literal["daily", "new_data", "manual", "webhook"]


class ScoringResult(BaseModel):
    """The canonical output of any Scoring Agent run.

    Persists to Firestore agent_runs (run_id-keyed) AND to a per-contact
    rollup so the workbook UI can read the latest score for a contact
    with one read.
    """

    score_type_id: str = Field(
        description="Score type, e.g. 'PIPELINE-SCORE'. Must equal findings.score_type.",
    )
    prompt_version: int = Field(ge=1)
    run_id: str = Field(description="UUID generated at run start.")

    contact_id: str = Field(
        min_length=1,
        description="GHL contact id — scoring is always per-contact.",
    )
    contact_name: str | None = Field(
        default=None,
        description=(
            "Human-readable label for the contact (e.g. 'Jamie Sheehan'). "
            "Populated by the runner from the contact record so file names "
            "and document headers can use it instead of the opaque GHL id."
        ),
    )
    contact_email: str | None = Field(
        default=None,
        description="Contact email — second-choice display fallback after contact_name.",
    )
    municipality_name: str | None = None

    generated_at: datetime
    triggered_by: ScoreTrigger = Field(
        description="What kicked this run off. Useful for stall-detection telemetry."
    )

    findings: Findings

    notes: str | None = Field(
        default=None,
        description="Free-form caveats for human reviewers (data quality, missing inputs, edge cases).",
    )

    @model_validator(mode="after")
    def _score_type_matches_findings(self) -> "ScoringResult":
        if self.score_type_id != self.findings.score_type:
            raise ValueError(
                f"score_type_id {self.score_type_id!r} does not match "
                f"findings.score_type {self.findings.score_type!r}"
            )
        return self


# =============================================================================
# Helpers used by runner and prompt builder
# =============================================================================

_FINDINGS_TYPE_MAP: dict[str, type] = {
    "PIPELINE-SCORE": PipelineScoreFindings,
}


def json_schema_for_type(score_type_id: str) -> dict[str, Any]:
    """ScoringResult schema narrowed to one score type.

    Smaller prompt block than the full union. Falls back to the full schema
    for unknown types.
    """
    from pydantic import create_model

    findings_cls = _FINDINGS_TYPE_MAP.get(score_type_id)
    if findings_cls is None:
        return ScoringResult.model_json_schema()

    NarrowResult = create_model(
        "ScoringResult",
        score_type_id=(str, Field(description="Must equal findings.score_type")),
        prompt_version=(int, Field(ge=1)),
        run_id=(str, Field(description="UUID")),
        contact_id=(str, Field(min_length=1)),
        municipality_name=(str | None, Field(default=None)),
        generated_at=(datetime, ...),
        triggered_by=(ScoreTrigger, ...),
        findings=(findings_cls, ...),
        notes=(str | None, Field(default=None)),
    )
    return NarrowResult.model_json_schema()


def parse_response(raw: str) -> ScoringResult:
    """Validate Claude's JSON response. Raises ValidationError on schema violations."""
    return ScoringResult.model_validate_json(raw)


# =============================================================================
# Proven Process step taxonomy — drives prompt construction and validation
# =============================================================================

PROVEN_PROCESS_STEPS: dict[int, tuple[str, int]] = {
    # step_number -> (label, phase)
    1:  ("Discover the Municipal Champion",          1),
    2:  ("First Contact & Curiosity",                1),
    3:  ("Capture & Qualify the Vision",             1),
    4:  ("Schedule the Next Stage",                  1),
    5:  ("Build the Internal Coalition",             2),
    6:  ("Package the Project",                      2),
    7:  ("Place the Project",                        3),
    8:  ("Provide Political Tailwind",               3),
    9:  ("Commit to Funding & Partnership",          3),
    10: ("Close & Hand-Off",                         3),
}


def step_name(step: int) -> str:
    """Canonical 'Step N: Label' string."""
    label, _ = PROVEN_PROCESS_STEPS[step]
    return f"Step {step}: {label}"


def phase_for_step(step: int) -> int:
    return PROVEN_PROCESS_STEPS[step][1]
