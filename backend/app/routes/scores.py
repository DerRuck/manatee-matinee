"""Workbook score-list API.

The workbook UI's lead-prioritization view reads from the `contact_scores`
collection — one row per contact, kept fresh by the scoring agent. These
endpoints expose that collection with the filters the UI needs:

  GET /scores                       list rows sorted by lead_heat_score DESC
  GET /scores/{contact_id}          full latest score for one contact
  GET /scores/{contact_id}/history  recent scoring runs (audit / debug)

Filters on the list endpoint are AND'd:
  ?heat=boil&heat=simmer            include multiple heats (≤30)
  ?step=4                           only Step 4 contacts
  ?ready_to_advance=true            only contacts ready for the next step
  ?min_score=70                     only contacts at or above a heat threshold
  ?limit=25                         default 50, capped at 200
  ?cursor=<lead_heat_score>         paginate (last row's lead_heat_score)

The list response is intentionally slim — the workbook renders one summary
row per contact (heat badge, step, summary_one_line, ready chip). The full
findings tree is only returned by the detail endpoint.
"""
from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

LeadHeat = Literal["boil", "simmer", "stall", "cold", "won", "lost"]


class ScoreListItem(BaseModel):
    """One row in the lead-prioritization list. Slim by design.

    The workbook UI renders these directly — every field maps to something
    visible on the row. Detail fields (signals, blockers, scorecard) only
    come back from /scores/{contact_id}.
    """
    contact_id: str
    municipality_name: str | None = None
    score_type_id: str
    latest_run_id: str | None = None
    scored_at: Any | None = None  # Firestore returns datetime; FastAPI JSON-serializes

    current_step: int
    current_step_name: str
    current_phase: int
    step_confidence: float
    ready_to_advance: bool

    lead_heat: LeadHeat
    lead_heat_score: int = Field(ge=0, le=100)
    summary_one_line: str

    blocker_count: int = 0
    recommended_action_count: int = 0
    days_since_last_signal: int | None = None
    triggered_by: str | None = None


class ScoreListResponse(BaseModel):
    items: list[ScoreListItem]
    count: int
    next_cursor: int | None = Field(
        default=None,
        description=(
            "Pass this back as ?cursor=<value> to fetch the next page. "
            "Null when there are no more rows."
        ),
    )


class ScoreDetail(BaseModel):
    """Full score doc including findings. Used by the detail view."""
    contact_id: str
    municipality_name: str | None = None
    score_type_id: str
    prompt_version: int | None = None
    latest_run_id: str | None = None
    scored_at: Any | None = None
    triggered_by: str | None = None
    model: str | None = None

    current_step: int
    current_step_name: str
    current_phase: int
    step_confidence: float
    ready_to_advance: bool

    lead_heat: LeadHeat
    lead_heat_score: int
    summary_one_line: str

    blocker_count: int = 0
    recommended_action_count: int = 0
    days_since_last_signal: int | None = None

    findings: dict[str, Any] = Field(
        default_factory=dict,
        description="Full PipelineScoreFindings dict — signals, blockers, actions, scorecard.",
    )


class ScoreHistoryItem(BaseModel):
    run_id: str
    score_type_id: str | None = None
    finished_at: Any | None = None
    triggered_by: str | None = None
    current_step: int | None = None
    current_step_name: str | None = None
    lead_heat: str | None = None
    lead_heat_score: int | None = None
    step_confidence: float | None = None
    ready_to_advance: bool | None = None
    summary_one_line: str | None = None
    model: str | None = None
    status: str | None = None


class ScoreHistoryResponse(BaseModel):
    contact_id: str
    items: list[ScoreHistoryItem]
    count: int


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get(
    "",
    response_model=ScoreListResponse,
    summary="List scored contacts, sorted by lead heat",
)
def list_scores(
    heat: list[LeadHeat] | None = Query(
        default=None,
        description="Filter by lead heat. Pass multiple times to include several (boil + simmer).",
    ),
    step: int | None = Query(
        default=None, ge=1, le=10,
        description="Filter to one Proven Process step.",
    ),
    ready_to_advance: bool | None = Query(
        default=None,
        description="Filter to contacts whose checklist for current_step+1 is met.",
    ),
    min_score: int | None = Query(
        default=None, ge=0, le=100,
        description="Floor on lead_heat_score (0-100).",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: int | None = Query(
        default=None,
        description="Pass the lead_heat_score of the last row in the previous page.",
    ),
) -> ScoreListResponse:
    from services.firestore.scores import list_contact_scores

    try:
        rows = list_contact_scores(
            limit=limit,
            lead_heat=list(heat) if heat else None,
            current_step=step,
            ready_to_advance=ready_to_advance,
            min_score=min_score,
            start_after_score=cursor,
        )
    except Exception as exc:
        logger.exception("list_scores failed", extra={"limit": limit, "heat": heat})
        # Surface Firestore index-missing errors verbatim so the team can
        # click the console URL the SDK includes in the message.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"scores query failed: {exc}",
        ) from exc

    items = [ScoreListItem.model_validate(_coerce_list_row(r)) for r in rows]
    next_cursor: int | None = None
    if len(items) == limit:
        next_cursor = items[-1].lead_heat_score

    return ScoreListResponse(items=items, count=len(items), next_cursor=next_cursor)


@router.get(
    "/{contact_id}",
    response_model=ScoreDetail,
    summary="Latest score for one contact, including full findings",
)
def get_score(contact_id: str) -> ScoreDetail:
    from services.firestore.scores import get_contact_score

    row = get_contact_score(contact_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no score found for contact {contact_id!r}",
        )
    return ScoreDetail.model_validate(_coerce_detail_row(row))


@router.get(
    "/{contact_id}/history",
    response_model=ScoreHistoryResponse,
    summary="Recent scoring runs for one contact (debug / audit view)",
)
def get_score_history(
    contact_id: str,
    limit: int = Query(default=25, ge=1, le=100),
) -> ScoreHistoryResponse:
    from services.firestore.scores import list_score_history

    runs = list_score_history(contact_id, limit=limit)
    items = [ScoreHistoryItem.model_validate(_coerce_history_row(r)) for r in runs]
    return ScoreHistoryResponse(
        contact_id=contact_id, items=items, count=len(items)
    )


# ---------------------------------------------------------------------------
# Row coercion — pull only the fields the response models expect
# ---------------------------------------------------------------------------

_LIST_FIELDS = (
    "contact_id", "municipality_name", "score_type_id", "latest_run_id",
    "scored_at", "current_step", "current_step_name", "current_phase",
    "step_confidence", "ready_to_advance", "lead_heat", "lead_heat_score",
    "summary_one_line", "blocker_count", "recommended_action_count",
    "days_since_last_signal", "triggered_by",
)


def _coerce_list_row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row.get(k) for k in _LIST_FIELDS}


_DETAIL_FIELDS = _LIST_FIELDS + ("prompt_version", "model", "findings")


def _coerce_detail_row(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: row.get(k) for k in _DETAIL_FIELDS}
    out["findings"] = out.get("findings") or {}
    return out


_HISTORY_FIELDS = (
    "run_id", "score_type_id", "finished_at", "triggered_by",
    "current_step", "current_step_name", "lead_heat", "lead_heat_score",
    "step_confidence", "ready_to_advance", "summary_one_line", "model", "status",
)


def _coerce_history_row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row.get(k) for k in _HISTORY_FIELDS}
