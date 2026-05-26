"""Communications collection — inbound + outbound signal per contact.

The scoring agent reads this collection through context_builder
(_fetch_communications) to detect engagement, stall, and recent project
language. Two real sources feed it today:

  1. GoHighLevel — inbound emails / SMS / notes routed through the
     /webhooks/ghl handler with agent_type='comm:ingest'.
  2. Plaud — voice transcripts from the recorder, uploaded via the
     scripts/ingest_communication.py CLI (event recordings, meeting
     transcripts, debrief audio).

Doc ID is deterministic so re-ingesting the same source row updates the
existing record instead of creating a duplicate:
    ghl_msg_{message_id}   for GoHighLevel messages
    plaud_{drive_file_id}  for Plaud transcripts uploaded from Drive
    manual_{sha1(...)}     for manually-written notes via the CLI

Read path is module-level so tests can patch _get_client out without
loading google-cloud-firestore.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from core.settings import get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

CommunicationChannel = Literal["email", "sms", "voice_transcript", "note", "call"]
CommunicationDirection = Literal["inbound", "outbound", "internal"]
CommunicationSource = Literal["ghl", "drive", "manual"]


class Communication(BaseModel):
    """One inbound or outbound communication tied to a contact.

    `body` is the full text the scoring agent sees. Truncation happens at
    read time — context_builder shows the scorer the first N chars per
    message so a long thread doesn't blow the prompt budget.
    """
    comm_id:       str = Field(min_length=1)
    contact_id:    str = Field(min_length=1)
    channel:       CommunicationChannel
    direction:     CommunicationDirection
    timestamp:     datetime
    subject:       str | None = None
    body:          str = Field(default="", description="Full message text or transcript")
    source:        CommunicationSource
    source_ref:    str | None = Field(
        default=None,
        description="External id — GHL messageId, Drive fileId, etc. Lets us de-dupe re-ingestions.",
    )
    author:        str | None = Field(
        default=None,
        description="Email/name of sender for outbound, contact for inbound, recorder for transcripts.",
    )
    ingested_at:   datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc),
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def put_communication(comm: Communication) -> str:
    """Upsert a Communication. Returns the Firestore doc id."""
    from services.firestore.client import _get_client

    client = _get_client()
    settings = get_settings()
    record = comm.model_dump(mode="json")
    record["timestamp"] = comm.timestamp
    record["ingested_at"] = comm.ingested_at
    client.collection(
        settings.firestore_communications_collection
    ).document(comm.comm_id).set(record)
    return comm.comm_id


def make_comm_id(source: CommunicationSource, source_ref: str | None, body: str) -> str:
    """Build a deterministic id so re-ingestion is idempotent.

    Prefers source + source_ref (e.g. ghl_msg_<id>). When no external ref
    is available (manual entry), hashes the body so identical text isn't
    written twice.
    """
    if source_ref:
        return f"{source}_{source_ref}".replace(" ", "_")
    digest = hashlib.sha1(body.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"{source}_{digest}"


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def list_communications(
    contact_id: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Recent communications for one contact, newest first.

    Returns raw dicts (not Communication models) so the context_builder
    can dump them straight into Claude's prompt without going through
    pydantic again.
    """
    from services.firestore.client import _get_client
    from google.cloud.firestore_v1 import Query

    client = _get_client()
    settings = get_settings()

    try:
        query = (
            client.collection(settings.firestore_communications_collection)
            .where("contact_id", "==", contact_id)
            .order_by("timestamp", direction=Query.DESCENDING)
            .limit(min(max(limit, 1), 100))
        )
        return [snap.to_dict() for snap in query.stream()]
    except Exception:
        logger.exception(
            "communications fetch failed — check firestore.indexes.json deployed",
            extra={"contact_id": contact_id},
        )
        return []
