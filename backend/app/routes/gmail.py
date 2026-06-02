"""
Gmail thread search endpoint for the workbook reply flow (5/30).

POST /gmail/threads/search
    body: {contact_email, from_user?, max_results?}
    auth: X-CHAWQ-Secret header
    returns: {threads: [{thread_id, subject, from, date, snippet}], count, from_user}

Lets the workbook present reply-thread candidates BEFORE drafting, so the
user picks the right thread instead of the reply agent auto-guessing the
newest message. Surfaced via the chawq_thread_search MCP tool. Requires the
gmail.modify DWD scope (thread read) on the runtime SA.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from core.auth import CHAWQ_SECRET_HEADER, verify_chawq_shared_secret
from services.gmail import resolve_from_user, search_contact_threads

logger = logging.getLogger(__name__)

router = APIRouter()


class ThreadSearchRequest(BaseModel):
    contact_email: str = Field(
        ..., description="Counterparty email to find threads with."
    )
    from_user: str | None = Field(
        default=None,
        description="Mailbox to search; defaults to the simmer default user.",
    )
    max_results: int = Field(default=10, ge=1, le=25)


class ThreadSearchResponse(BaseModel):
    threads: list[dict[str, Any]]
    count: int
    from_user: str


@router.post("/threads/search", response_model=ThreadSearchResponse)
async def threads_search(
    body: ThreadSearchRequest,
    x_chawq_secret: str | None = Header(default=None, alias=CHAWQ_SECRET_HEADER),
) -> ThreadSearchResponse:
    """Return recent threads involving contact_email (newest first) for the
    workbook to surface as reply candidates."""
    verify_chawq_shared_secret(x_chawq_secret, route_name="gmail")

    mailbox = resolve_from_user(body.from_user)
    if body.contact_email.strip().lower() == mailbox.lower():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "contact_email must differ from the mailbox (from_user); "
                "searching your own address matches every thread"
            ),
        )

    try:
        threads = search_contact_threads(
            mailbox, body.contact_email, max_results=body.max_results
        )
    except Exception as exc:
        logger.exception("gmail thread search failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"gmail thread search failed: {type(exc).__name__}: {exc}",
        )

    logger.info(
        "gmail.threads.search count=%d contact=%s", len(threads), body.contact_email
    )
    return ThreadSearchResponse(threads=threads, count=len(threads), from_user=mailbox)
