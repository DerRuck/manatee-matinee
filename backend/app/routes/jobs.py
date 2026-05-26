"""Scheduled-job endpoints.

Cloud Scheduler hits these once a day (or on whatever cron the SRE config
sets). Each endpoint returns 202 immediately and runs the job in a FastAPI
BackgroundTask so Cloud Scheduler's 30-second HTTP timeout doesn't kill the
sweep mid-flight.

Endpoints:
    POST /jobs/scoring/daily       run the daily scoring sweep
    GET  /jobs/scoring/sweeps/{id} read a sweep's audit doc

The scoring sweep itself is in services/scoring_agent/sweep.py — these
handlers are thin adapters so the same code path runs from the CLI.

Auth: open in dev. In production, Cloud Run rejects unauthenticated
requests so only the Cloud Scheduler service account can hit these.
A defense-in-depth check on a shared header is a future hardening pass.
"""
from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class DailyScoringRequest(BaseModel):
    max_contacts: int = Field(default=100, ge=1, le=1000)
    min_age_hours: int = Field(
        default=18, ge=0, le=168,
        description="Skip contacts scored within this window.",
    )
    skip_lost: bool = True
    triggered_by: Literal["daily", "manual", "webhook", "new_data"] = "daily"
    dry_run: bool = Field(
        default=False,
        description=(
            "Build eligibility + count what WOULD be scored, but skip the "
            "Claude calls and Firestore writeback."
        ),
    )


class DailyScoringResponse(BaseModel):
    sweep_id: str
    status: Literal["queued"] = "queued"
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post(
    "/scoring/daily",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DailyScoringResponse,
    summary="Kick off a daily scoring sweep (background)",
)
def trigger_daily_scoring(
    background_tasks: BackgroundTasks,
    body: DailyScoringRequest | None = None,
) -> DailyScoringResponse:
    """Queue a scoring sweep and return immediately.

    The sweep ID can be polled at GET /jobs/scoring/sweeps/{id}. Per-contact
    outcomes are written to Firestore as the sweep progresses so the audit
    doc reflects work-in-flight, not just the final state.
    """
    import uuid

    req = body or DailyScoringRequest()
    sweep_id = str(uuid.uuid4())

    logger.info(
        "daily scoring sweep queued",
        extra={
            "sweep_id": sweep_id,
            "max_contacts": req.max_contacts,
            "triggered_by": req.triggered_by,
            "dry_run": req.dry_run,
        },
    )

    background_tasks.add_task(
        _run_sweep_in_background,
        sweep_id=sweep_id,
        max_contacts=req.max_contacts,
        min_age_hours=req.min_age_hours,
        skip_lost=req.skip_lost,
        triggered_by=req.triggered_by,
        dry_run=req.dry_run,
    )

    return DailyScoringResponse(sweep_id=sweep_id, dry_run=req.dry_run)


@router.get(
    "/scoring/sweeps/{sweep_id}",
    summary="Read a sweep's audit doc",
)
def get_sweep(sweep_id: str) -> dict[str, Any]:
    from services.scoring_agent.sweep import get_sweep_doc

    doc = get_sweep_doc(sweep_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"sweep {sweep_id!r} not found",
        )
    return doc


# ---------------------------------------------------------------------------
# Background entry point — picks up the caller's sweep_id so the response
# returned to Cloud Scheduler matches the doc the sweep writes.
# ---------------------------------------------------------------------------

def _run_sweep_in_background(
    sweep_id: str,
    max_contacts: int,
    min_age_hours: int,
    skip_lost: bool,
    triggered_by: str,
    dry_run: bool,
) -> None:
    from services.scoring_agent.sweep import run_daily_sweep

    try:
        run_daily_sweep(
            sweep_id=sweep_id,
            max_contacts=max_contacts,
            triggered_by=triggered_by,
            min_age_hours=min_age_hours,
            skip_lost=skip_lost,
            dry_run=dry_run,
        )
    except Exception:
        logger.exception(
            "background sweep failed",
            extra={"sweep_id": sweep_id, "triggered_by": triggered_by},
        )
