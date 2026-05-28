"""
GHL → Firestore contacts sync.

Single source of truth for the sync logic. Both the CLI script
(backend/scripts/sync_ghl_contacts.py) and the manual endpoint
(backend/app/routes/sync.py) import run_sync from here.

Phase 1 (5/28): backfill + manual-trigger endpoint.
Phase 2: Cloud Scheduler hits the endpoint nightly with {since: <last_run_at>}.

One-way GHL → Firestore. No write-back through this path. The /webhooks/ghl
handler still owns event-driven write paths if any exist.
"""
from __future__ import annotations

import logging
from typing import Any

from google.cloud import firestore as gcf

from services.firestore.client import (
    get_municipality,
    iter_contacts,
    upsert_contact,
    upsert_municipality,
)
from services.ghl.client import _sync_client
from services.ghl.contacts import (
    fetch_contact,
    fetch_opportunities_for_contact,
    paginate_contacts,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mapping tables (locked 2026-05-28)
# ---------------------------------------------------------------------------

# Custom field IDs from the 4/21 GHL inventory.
CF_JOB_TITLE = "EKgjj9fx0jWrOzrV3IAp"
CF_CONTACT_NOTES = "u7nkCuvWJdcfe4mZLqjR"

# Pipelines whose membership marks a contact as a real lead candidate.
LEAD_PIPELINE_IDS = {
    "fvsGdShMCE9vHmVDQX9D",  # Conferences
    "XzMFX3KQkOKBs23U2OQE",  # Project Pipeline
}

# Tag → municipality metadata.
# Tuple shape: (slug, display_name, state, county, jurisdiction_type).
# Slugs follow the 4/23 lowercase-underscore identity convention so they
# align with chunks.municipality[].
# Add an entry per municipality entering the pilot; mapping changes propagate
# to existing Firestore rows on the next re-sync.
TAG_TO_MUNICIPALITY: dict[str, tuple[str, str, str, str, str]] = {
    "rookery-bay":  ("rookery_bay",     "Rookery Bay, FL",                          "FL", "Collier", "state"),
    "rookery_bay":  ("rookery_bay",     "Rookery Bay, FL",                          "FL", "Collier", "state"),
    "sfwmd":        ("sfwmd",           "South Florida Water Management District", "FL",
                     "Broward, Collier, Glades, Hendry, Lee, Martin, Miami-Dade, Monroe, Palm Beach, St. Lucie (+ portions of Charlotte, Highlands, Okeechobee, Orange, Osceola, Polk)",
                     "wmd"),
    "naples":       ("naples_fl",       "Naples, FL",                               "FL", "Collier", "city"),
    "naples-fl":    ("naples_fl",       "Naples, FL",                               "FL", "Collier", "city"),
    "marco-island": ("marco_island_fl", "Marco Island, FL",                         "FL", "Collier", "city"),
    "marco_island": ("marco_island_fl", "Marco Island, FL",                         "FL", "Collier", "city"),
}


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------

def resolve_municipality(
    tags: list[str],
) -> tuple[str, str, str, str, str] | None:
    """Match the first known tag in the contact's tag list."""
    for tag in tags or []:
        normalized = tag.lower().strip()
        if normalized in TAG_TO_MUNICIPALITY:
            return TAG_TO_MUNICIPALITY[normalized]
    return None


def derive_lead_candidate(opportunities: list[dict[str, Any]]) -> bool:
    """V1: lead candidate iff contact has an opportunity in a pilot pipeline."""
    return any(
        opp.get("pipelineId") in LEAD_PIPELINE_IDS for opp in opportunities
    )


def extract_custom_field(contact: dict[str, Any], field_id: str) -> Any:
    """GHL returns customFields as a list of {id, value} maps."""
    for cf in contact.get("customFields", []) or []:
        if cf.get("id") == field_id:
            return cf.get("value")
    return None


# ---------------------------------------------------------------------------
# Doc builders
# ---------------------------------------------------------------------------

def build_contact_doc(
    contact: dict[str, Any],
    opportunities: list[dict[str, Any]],
    municipality_slug: str | None,
    sync_source: str,
) -> dict[str, Any]:
    primary = opportunities[0] if opportunities else {}
    return {
        "ghl_contact_id": contact.get("id"),
        "first_name": contact.get("firstName"),
        "last_name": contact.get("lastName"),
        "email": contact.get("email"),
        "phone": contact.get("phone"),
        "tags": contact.get("tags") or [],
        "job_title": extract_custom_field(contact, CF_JOB_TITLE),
        "contact_notes": extract_custom_field(contact, CF_CONTACT_NOTES),
        "municipality_slug": municipality_slug,
        # active_project_slug reserved for the multi-project case (5/15
        # forward-compat decision). Null in V1.
        "active_project_slug": None,
        "opportunity_id": primary.get("id"),
        "pipeline_id": primary.get("pipelineId"),
        "pipeline_stage_id": primary.get("pipelineStageId"),
        "is_lead_candidate": derive_lead_candidate(opportunities),
        "date_added": contact.get("dateAdded"),
        "date_updated": contact.get("dateUpdated"),
        "last_synced_at": gcf.SERVER_TIMESTAMP,
        "sync_source": sync_source,
        "raw": contact,
    }


def _seed_municipality_doc(
    slug: str, display: str, state: str, county: str, jurisdiction_type: str,
) -> dict[str, Any]:
    now = gcf.SERVER_TIMESTAMP
    return {
        "slug": slug,
        "display_name": display,
        "state": state,
        "county": county,
        "jurisdiction_type": jurisdiction_type,
        "status": "prospect",
        "contact_count": 0,
        "created_at": now,
        "updated_at": now,
    }


def _refresh_municipality_fields(
    county: str, jurisdiction_type: str,
) -> dict[str, Any]:
    """Fields refreshed on re-sync — propagates mapping corrections without
    clobbering status / contact_count."""
    return {
        "county": county,
        "jurisdiction_type": jurisdiction_type,
        "updated_at": gcf.SERVER_TIMESTAMP,
    }


# ---------------------------------------------------------------------------
# Sync core
# ---------------------------------------------------------------------------

def _sync_one(
    ghl_client,
    contact: dict[str, Any],
    dry_run: bool,
    sync_source: str,
) -> tuple[bool, bool, bool]:
    """Sync one contact + its municipality.

    Returns (created, updated, has_municipality).
    """
    contact_id = contact.get("id")
    if not contact_id:
        raise ValueError("contact has no id")

    opportunities = fetch_opportunities_for_contact(
        contact_id, client=ghl_client,
    )
    resolved = resolve_municipality(contact.get("tags") or [])

    municipality_slug: str | None = None
    has_municipality = False
    if resolved:
        slug, display, state, county, jurisdiction_type = resolved
        municipality_slug = slug
        if dry_run:
            logger.info(
                "[dry-run] would upsert municipality slug=%s county=%s jurisdiction=%s",
                slug, county, jurisdiction_type,
            )
        else:
            existing = get_municipality(slug)
            if existing:
                upsert_municipality(
                    slug, _refresh_municipality_fields(county, jurisdiction_type),
                )
            else:
                upsert_municipality(
                    slug, _seed_municipality_doc(
                        slug, display, state, county, jurisdiction_type,
                    ),
                )
        has_municipality = True

    doc = build_contact_doc(contact, opportunities, municipality_slug, sync_source)

    if dry_run:
        logger.info(
            "[dry-run] would upsert contact id=%s muni=%s lead=%s",
            contact_id, municipality_slug, doc["is_lead_candidate"],
        )
        # Without an existence check we can't tell created vs updated; report
        # as updated so dry-run counts don't over-promise creation.
        return (False, True, has_municipality)

    created = upsert_contact(contact_id, doc)
    return (created, not created, has_municipality)


def refresh_municipality_counts(dry_run: bool = False) -> None:
    """Recompute contact_count for every municipality based on current contacts.

    Run after a full pass — not after a single-contact resync (the count
    change would over- or under-report when called inside a single-contact
    flow that doesn't see the rest of the contacts).
    """
    counts: dict[str, int] = {}
    for c in iter_contacts():
        slug = c.get("municipality_slug")
        if slug:
            counts[slug] = counts.get(slug, 0) + 1
    for slug, count in counts.items():
        if dry_run:
            logger.info(
                "[dry-run] would set municipality %s contact_count=%d",
                slug, count,
            )
            continue
        upsert_municipality(
            slug,
            {
                "contact_count": count,
                "updated_at": gcf.SERVER_TIMESTAMP,
            },
        )


def run_sync(
    *,
    contact_id: str | None = None,
    since: str | None = None,
    dry_run: bool = False,
    sync_source: str = "backfill",
) -> dict[str, int]:
    """
    Core sync function. Reused by the CLI script and the manual sync endpoint.

    Args:
        contact_id: If set, sync this one contact and skip everything else.
        since: ISO timestamp filter on dateUpdated (incremental mode).
        dry_run: Log intended writes without performing them.
        sync_source: Recorded on each contact doc as sync_source. One of
            "backfill", "scheduled", "manual", "webhook".

    Returns:
        Dict with synced_count, created, updated, no_municipality, errors.
    """
    created_count = 0
    updated_count = 0
    no_municipality = 0
    errors = 0

    with _sync_client() as ghl:
        if contact_id:
            try:
                contact = fetch_contact(contact_id, client=ghl)
                c, u, hm = _sync_one(ghl, contact, dry_run, sync_source)
                created_count += int(c)
                updated_count += int(u)
                if not hm:
                    no_municipality += 1
            except Exception:
                logger.exception(
                    "sync.run_sync: failed contact %s", contact_id,
                )
                errors += 1
        else:
            for contact in paginate_contacts(client=ghl, since_iso=since):
                try:
                    c, u, hm = _sync_one(ghl, contact, dry_run, sync_source)
                    created_count += int(c)
                    updated_count += int(u)
                    if not hm:
                        no_municipality += 1
                except Exception:
                    logger.exception(
                        "sync.run_sync: failed contact %s",
                        contact.get("id"),
                    )
                    errors += 1

    # Only refresh rollups after a full pass.
    if not contact_id:
        try:
            refresh_municipality_counts(dry_run=dry_run)
        except Exception:
            logger.exception("sync.run_sync: failed to refresh municipality counts")
            # Don't fail the whole run — the contact writes already landed.

    return {
        "synced_count": created_count + updated_count,
        "created": created_count,
        "updated": updated_count,
        "no_municipality": no_municipality,
        "errors": errors,
    }
