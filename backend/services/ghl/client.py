"""
GoHighLevel v2 API client (skeleton).

This is a stub.

Auth model: Private Integration Token (PIT), location-scoped.
Base URL:   https://services.leadconnectorhq.com
Required:   Authorization: Bearer <pit>, Version: 2021-07-28
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from core.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class GHLClient:
    """
    Thin wrapper around the GHL v2 REST API.

    Usage:
        ghl = GHLClient()
        contact = await ghl.get_contact("abc123")
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.ghl_pit:
            logger.warning(
                "GHLClient initialized without a PIT — all requests will fail. "
                "Set GHL_PIT in the environment."
            )

        self._client = httpx.AsyncClient(
            base_url=self.settings.ghl_base_url,
            headers={
                "Authorization": f"Bearer {self.settings.ghl_pit}",
                "Version": self.settings.ghl_api_version_header,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(10.0, read=30.0),
        )

    async def close(self) -> None:
        await self._client.aclose()

    # -------------------- Contacts --------------------

    async def get_contact(self, contact_id: str) -> dict[str, Any]:
        """GET /contacts/{contactId}"""
        resp = await self._client.get(f"/contacts/{contact_id}")
        resp.raise_for_status()
        return resp.json()

    async def create_contact(self, contact: dict[str, Any]) -> dict[str, Any]:
        """POST /contacts/"""
        payload = {"locationId": self.settings.ghl_location_id, **contact}
        resp = await self._client.post("/contacts/", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def update_contact(
        self, contact_id: str, updates: dict[str, Any]
    ) -> dict[str, Any]:
        """PUT /contacts/{contactId}"""
        resp = await self._client.put(f"/contacts/{contact_id}", json=updates)
        resp.raise_for_status()
        return resp.json()

    # -------------------- Opportunities --------------------

    async def update_opportunity_stage(
        self, opportunity_id: str, stage_id: str
    ) -> dict[str, Any]:
        """PUT /opportunities/{opportunityId} with stageId in body."""
        resp = await self._client.put(
            f"/opportunities/{opportunity_id}",
            json={"stageId": stage_id},
        )
        resp.raise_for_status()
        return resp.json()

    async def list_pipelines(self) -> list[dict[str, Any]]:
        """GET /opportunities/pipelines?locationId=..."""
        resp = await self._client.get(
            "/opportunities/pipelines",
            params={"locationId": self.settings.ghl_location_id},
        )
        resp.raise_for_status()
        return resp.json().get("pipelines", [])

    # -------------------- Custom Fields --------------------

    async def list_custom_fields(self) -> list[dict[str, Any]]:
        """
        GET /locations/{locationId}/customFields

        Returns list of {id, name, fieldKey, dataType, ...}. Cache the result
        and build a name -> id map; the API wants opaque IDs on writes.
        """
        resp = await self._client.get(
            f"/locations/{self.settings.ghl_location_id}/customFields"
        )
        resp.raise_for_status()
        return resp.json().get("customFields", [])


# ---------------------------------------------------------------------------
# Sync helpers — for callers running outside an event loop (e.g. FastAPI
# BackgroundTasks, which run sync functions in a threadpool). The async
# GHLClient above is the long-term home; these are intentional duplicates
# kept lean so the Hello World runner can hit GHL without async plumbing.
# ---------------------------------------------------------------------------

def _sync_client() -> httpx.Client:
    settings = get_settings()
    return httpx.Client(
        base_url=settings.ghl_base_url,
        headers={
            "Authorization": f"Bearer {settings.ghl_pit}",
            "Version": settings.ghl_api_version_header,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=httpx.Timeout(10.0, read=30.0),
    )


def update_contact_sync(contact_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    """
    Sync mirror of GHLClient.update_contact. PUT /contacts/{contactId}.

    `updates` is passed directly as the request body — typically a dict like:
        {"customFields": [{"id": "<field_id>", "value": "..."}]}
    or any other partial contact update GHL accepts.
    """
    with _sync_client() as client:
        resp = client.put(f"/contacts/{contact_id}", json=updates)
        resp.raise_for_status()
        return resp.json()
