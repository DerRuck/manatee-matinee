"""
Manual sync endpoint for the GHL → Firestore contacts pipeline.

POST /sync/ghl/contacts
    body: {contact_id?, since?, dry_run?}
    auth: X-CHAWQ-Secret header

Wraps services.ghl.sync.run_sync — same core function the CLI script uses.

Phase 1 (5/28): manual trigger only.
Phase 2: Cloud Scheduler hits this nightly with {since: <last_run_at>}.
    A `sync_state` doc on the `system` collection holds the watermark.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from core.auth import CHAWQ_SECRET_HEADER, verify_chawq_shared_secret
from services.ghl.sync import run_sync

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class SyncContactsRequest(BaseModel):
    contact_id: str | None = Field(
        default=None,
        description="If set, sync only this GHL contact. Skips municipality count refresh.",
    )
    since: str | None = Field(
        default=None,
        description=(
            "ISO timestamp filter on dateUpdated (incremental mode, Phase 2)."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="Log intended writes without performing them.",
    )


class SyncContactsResponse(BaseModel):
    synced_count: int
    created: int
    updated: int
    no_municipality: int
    errors: int


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@router.post("/ghl/contacts", response_model=SyncContactsResponse)
async def sync_ghl_contacts(
    body: SyncContactsRequest,
    x_chawq_secret: str | None = Header(
        default=None, alias=CHAWQ_SECRET_HEADER,
    ),
) -> SyncContactsResponse:
    """Trigger a GHL → Firestore contacts sync.

    Returns a count summary. Synchronous on V1 dataset size; switch to a
    BackgroundTask pattern (mirroring /agents/run) once full-backfill takes
    long enough that the HTTP request times out.
    """
    verify_chawq_shared_secret(x_chawq_secret, route_name="sync")

    if body.contact_id is None and body.since is None:
        sync_source = "backfill"
    elif body.contact_id is not None:
        sync_source = "manual"
    else:
        sync_source = "scheduled"

    try:
        result = run_sync(
            contact_id=body.contact_id,
            since=body.since,
            dry_run=body.dry_run,
            sync_source=sync_source,
        )
    except Exception:
        logger.exception("sync.run_sync raised — failing endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="sync failed; see server logs",
        )

    logger.info(
        "sync complete source=%s created=%d updated=%d no_muni=%d errors=%d",
        sync_source,
        result["created"],
        result["updated"],
        result["no_municipality"],
        result["errors"],
    )

    return SyncContactsResponse(**result)
