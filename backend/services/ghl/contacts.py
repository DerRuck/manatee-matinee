"""
GHL contacts + opportunities API helpers (sync versions).

The existing GHLClient in services/ghl/client.py is the async long-term home.
These sync helpers are for use by callers that run outside an event loop:
FastAPI BackgroundTasks (which execute sync callables in a threadpool) and
CLI scripts (which don't run an event loop at all).

Mirrors the `update_contact_sync` pattern already in services/ghl/client.py.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterator

import httpx

from core.settings import get_settings
from services.ghl.client import _sync_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_iso(value: str | None) -> datetime | None:
    """Parse GHL's ISO timestamps. Handles trailing Z."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

def paginate_contacts(
    *,
    client: httpx.Client | None = None,
    since_iso: str | None = None,
) -> Iterator[dict[str, Any]]:
    """
    Yield contacts from GHL, paginated.

    Optional `since_iso` filters client-side by `dateUpdated`. Server-side
    incremental filtering on the search-contacts endpoint exists but is more
    complex; client-side is fine for V1 dataset size.

    `client` lets callers pass an open httpx client so a single sync run
    shares one connection across all pages + per-contact opportunity lookups.
    """
    settings = get_settings()
    owned_client = client is None
    c = client or _sync_client()
    since_dt = _parse_iso(since_iso) if since_iso else None
    try:
        next_url: str | None = "/contacts/"
        params: dict[str, Any] | None = {
            "locationId": settings.ghl_location_id,
            "limit": 100,
        }
        while next_url:
            resp = c.get(next_url, params=params)
            resp.raise_for_status()
            data = resp.json()
            for contact in data.get("contacts", []) or []:
                if since_dt is not None:
                    updated = _parse_iso(
                        contact.get("dateUpdated") or contact.get("dateAdded")
                    )
                    if updated and updated < since_dt:
                        continue
                yield contact
            next_url = data.get("meta", {}).get("nextPageUrl")
            # nextPageUrl has params baked in; don't pass our own again.
            params = None
    finally:
        if owned_client:
            c.close()


def fetch_contact(
    contact_id: str,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """GET /contacts/{contact_id} — returns the contact dict."""
    owned_client = client is None
    c = client or _sync_client()
    try:
        resp = c.get(f"/contacts/{contact_id}")
        resp.raise_for_status()
        return resp.json().get("contact", {}) or {}
    finally:
        if owned_client:
            c.close()


# ---------------------------------------------------------------------------
# Opportunities
# ---------------------------------------------------------------------------

def fetch_opportunities_for_contact(
    contact_id: str,
    *,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """
    GET /opportunities/search?location_id=...&contact_id=...

    Used to derive lead-candidate status (any opportunity in a pilot pipeline?)
    and to capture the contact's current pipeline + stage. Cheap call —
    a contact with no opportunities returns `{"opportunities": []}`.
    """
    settings = get_settings()
    owned_client = client is None
    c = client or _sync_client()
    try:
        resp = c.get(
            "/opportunities/search",
            params={
                "location_id": settings.ghl_location_id,
                "contact_id": contact_id,
            },
        )
        resp.raise_for_status()
        return resp.json().get("opportunities", []) or []
    finally:
        if owned_client:
            c.close()
