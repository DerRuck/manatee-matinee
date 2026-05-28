"""
Contact search endpoint for the workbook contact-lookup pivot (5/28).

POST /contacts/search
    body: {first_name?, last_name?, municipality_slug?, query?, limit?}
    auth: X-CHAWQ-Secret header
    returns: {matches: [...], count}

Replaces the `demo_contacts.yaml` lookup in `arch/workbook_mvp.md` with a real
Firestore query against the contacts mirror. The endpoint joins each contact
with its municipality server-side so the workbook gets display_name, county,
and jurisdiction_type in one round-trip.

Surfaced through the `chawq_contact_lookup` MCP tool in chawq_mcp_server.py.

V1 search rules:
  - first_name / last_name match case-sensitively on the GHL fields (the
    workbook lowercases before calling).
  - municipality_slug is an exact match.
  - query is free-text fallback — treated as a first_name match.
  - Ambiguity surfaces as a multi-element matches list; the workbook asks
    the user to disambiguate.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from core.auth import CHAWQ_SECRET_HEADER, verify_chawq_shared_secret
from services.firestore.client import get_municipality, search_contacts

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ContactSearchRequest(BaseModel):
    first_name: str | None = Field(
        default=None,
        description="Case-sensitive first-name match. Lowercase before calling.",
    )
    last_name: str | None = Field(
        default=None,
        description="Case-sensitive last-name match.",
    )
    municipality_slug: str | None = Field(
        default=None,
        description="Exact slug match (e.g. 'rookery_bay').",
    )
    query: str | None = Field(
        default=None,
        description=(
            "Free-text fallback when the workbook can't cleanly split the "
            "user's phrasing. Treated as a first_name match."
        ),
    )
    limit: int = Field(default=10, ge=1, le=50)


class ContactMatch(BaseModel):
    contact_id: str
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    job_title: str | None = None
    tags: list[str] = []
    is_lead_candidate: bool = False

    # Municipality fields (joined). Null when contact has no resolved municipality.
    municipality_slug: str | None = None
    municipality_display: str | None = None
    state: str | None = None
    county: str | None = None
    jurisdiction_type: str | None = None

    # Forward-compat for multi-project.
    active_project_slug: str | None = None


class ContactSearchResponse(BaseModel):
    matches: list[ContactMatch]
    count: int


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@router.post("/search", response_model=ContactSearchResponse)
async def search(
    body: ContactSearchRequest,
    x_chawq_secret: str | None = Header(
        default=None, alias=CHAWQ_SECRET_HEADER,
    ),
) -> ContactSearchResponse:
    """Look up contacts by name + optional municipality.

    The workbook calls this when the user mentions a contact in chat
    ("nick from rookery bay"). Returns a list — the workbook disambiguates
    if more than one row comes back.
    """
    verify_chawq_shared_secret(x_chawq_secret, route_name="contacts")

    if not any(
        [body.first_name, body.last_name, body.municipality_slug, body.query]
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "at least one of first_name, last_name, municipality_slug, "
                "or query is required"
            ),
        )

    contacts = search_contacts(
        first_name=body.first_name,
        last_name=body.last_name,
        municipality_slug=body.municipality_slug,
        query=body.query,
        limit=body.limit,
    )

    # Join municipalities. Cache per-slug so two contacts at the same
    # municipality don't double-read.
    muni_cache: dict[str, dict[str, Any] | None] = {}
    matches: list[ContactMatch] = []
    for c in contacts:
        slug = c.get("municipality_slug")
        muni: dict[str, Any] | None = None
        if slug:
            if slug not in muni_cache:
                muni_cache[slug] = get_municipality(slug)
            muni = muni_cache[slug]

        matches.append(
            ContactMatch(
                contact_id=c.get("ghl_contact_id") or "",
                first_name=c.get("first_name"),
                last_name=c.get("last_name"),
                email=c.get("email"),
                phone=c.get("phone"),
                job_title=c.get("job_title"),
                tags=c.get("tags") or [],
                is_lead_candidate=bool(c.get("is_lead_candidate")),
                municipality_slug=slug,
                municipality_display=(muni or {}).get("display_name"),
                state=(muni or {}).get("state"),
                county=(muni or {}).get("county"),
                jurisdiction_type=(muni or {}).get("jurisdiction_type"),
                active_project_slug=c.get("active_project_slug"),
            )
        )

    logger.info(
        "contacts.search count=%d first_name=%s muni=%s",
        len(matches),
        body.first_name or "-",
        body.municipality_slug or "-",
    )

    return ContactSearchResponse(matches=matches, count=len(matches))
