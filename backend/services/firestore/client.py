"""
Firestore client.

Collections (per settings.py):
  - contacts
  - agent_runs       (one document per agent invocation)
  - feedback
  - prompt_versions
  - documents        (one row per Drive file ingested)
  - chunks           (retrieval surface; native Firestore vector search)

Auth strategy: default ADC creds work in both environments without
impersonation. Locally, the user has Owner-level project access; in Cloud
Run, the runtime SA has roles/datastore.user (per SA inventory memory).
Drive is the special case that needs impersonation, not Firestore.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from google.cloud import firestore
from google.cloud.firestore_v1.vector import Vector

from core.settings import Settings, get_settings
from services.firestore.schema import Chunk, Document

logger = logging.getLogger(__name__)


_client: firestore.Client | None = None


def _get_client() -> firestore.Client:
    """Lazy-build (and cache) the Firestore client for the configured project."""
    global _client
    if _client is None:
        settings = get_settings()
        _client = firestore.Client(project=settings.gcp_project_id)
    return _client


# ---------------------------------------------------------------------------
# Contacts (GHL backfill)
# ---------------------------------------------------------------------------

def get_contact(contact_id: str) -> dict[str, Any] | None:
    """Fetch one row from the `contacts` collection by GHL contact id.

    Returns the raw document dict (GHL contact shape: firstName, lastName,
    email, customFields[], tags[], locationId, etc.) or None if missing.
    The document ID matches the GHL contact id.
    """
    client = _get_client()
    settings = get_settings()
    snap = (
        client.collection(settings.firestore_contacts_collection)
        .document(contact_id)
        .get()
    )
    if not snap.exists:
        return None
    doc = snap.to_dict() or {}
    doc.setdefault("id", snap.id)
    return doc


def list_contacts(limit: int = 25) -> list[dict[str, Any]]:
    """Stream up to `limit` contacts from the `contacts` collection.

    Used by browse scripts and smoke tests to discover which contact IDs
    exist in the backfill without hitting GHL directly.
    """
    client = _get_client()
    settings = get_settings()
    out: list[dict[str, Any]] = []
    for snap in (
        client.collection(settings.firestore_contacts_collection)
        .limit(limit)
        .stream()
    ):
        doc = snap.to_dict() or {}
        doc.setdefault("id", snap.id)
        out.append(doc)
    return out


def put_agent_run(run_id: str, record: dict[str, Any]) -> None:
    """
    Upsert one row in the `agent_runs` collection, keyed by run_id.

    The record dict shape is intentionally not enforced here — the runner that
    composes it owns the schema. Document ID = run_id for direct lookup later.
    """
    client = _get_client()
    settings = get_settings()
    collection = settings.firestore_agent_runs_collection
    client.collection(collection).document(run_id).set(record)


# ---------------------------------------------------------------------------
# Documents + chunks (vector ingest)
# ---------------------------------------------------------------------------

def put_document(document: Document) -> None:
    """
    Upsert one row in the `documents` collection, keyed by document.document_id.
    """
    client = _get_client()
    settings = get_settings()
    collection = settings.firestore_documents_collection
    record = document.model_dump(mode="json")
    # Pydantic dumps datetimes as ISO strings; Firestore stores them better as
    # native datetimes. Override the two timestamp fields after the dump.
    record["drive_modified_time"] = document.drive_modified_time
    if document.ingested_at is not None:
        record["ingested_at"] = document.ingested_at
    client.collection(collection).document(document.document_id).set(record)


def put_chunks_bulk(chunks: Iterable[Chunk], batch_size: int = 400) -> int:
    """
    Write many chunks at once. Embeddings are wrapped in firestore.Vector so
    Firestore's `find_nearest` can use the field as a vector index target.

    Firestore batched writes max out at 500 ops per batch; we use 400 for
    headroom. Returns the total count written.
    """
    client = _get_client()
    settings = get_settings()
    collection_name = settings.firestore_chunks_collection
    collection = client.collection(collection_name)

    batch = client.batch()
    in_batch = 0
    total = 0

    for chunk in chunks:
        record = chunk.model_dump(mode="json")
        # Replace the plain list with a real Vector so the vector index applies.
        record["embedding"] = Vector(chunk.embedding)
        ref = collection.document(chunk.chunk_id)
        batch.set(ref, record)
        in_batch += 1
        total += 1

        if in_batch >= batch_size:
            batch.commit()
            batch = client.batch()
            in_batch = 0

    if in_batch > 0:
        batch.commit()

    return total


def delete_chunks_for_document(document_id: str, batch_size: int = 400) -> int:
    """
    Delete every chunk row whose document_id matches. Used at the start of a
    re-ingest so a shrunk document doesn't leave orphan chunks behind.

    Returns the count deleted.
    """
    client = _get_client()
    settings = get_settings()
    collection_name = settings.firestore_chunks_collection
    collection = client.collection(collection_name)

    query = collection.where("document_id", "==", document_id)
    deleted = 0
    batch = client.batch()
    in_batch = 0

    for snap in query.stream():
        batch.delete(snap.reference)
        in_batch += 1
        deleted += 1
        if in_batch >= batch_size:
            batch.commit()
            batch = client.batch()
            in_batch = 0

    if in_batch > 0:
        batch.commit()

    return deleted


class FirestoreRepo:
    """Stateful wrapper for places that prefer object-style access. Sprint 2+."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._db = None

    async def put_agent_run(self, run_id: str, record: dict) -> None:
        raise NotImplementedError("Use module-level put_agent_run() for now.")

    async def get_agent_run(self, run_id: str) -> dict | None:
        raise NotImplementedError("Wire when something actually reads agent_runs.")
