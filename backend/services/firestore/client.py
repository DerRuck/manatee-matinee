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
from google.cloud.firestore_v1.base_query import FieldFilter
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


def put_agent_run(run_id: str, record: dict[str, Any]) -> None:
    """
    Upsert one row in the `agent_runs` collection, keyed by run_id.

    Uses set(merge=True) so a runner's terminal write layers cleanly on top
    of a pending stub written by POST /agents/run — fields like `created_at`,
    `triggered_by`, and `inputs` survive the terminal write.

    The record dict shape is intentionally not enforced here — the runner
    that composes it owns the schema. Document ID = run_id for direct lookup.
    """
    client = _get_client()
    settings = get_settings()
    collection = settings.firestore_agent_runs_collection
    client.collection(collection).document(run_id).set(record, merge=True)


def get_agent_run(run_id: str) -> dict[str, Any] | None:
    """
    Read one row from `agent_runs` by run_id. Returns the raw dict or None
    if no document exists. Used by GET /agents/runs/{run_id} for polling.
    """
    client = _get_client()
    settings = get_settings()
    collection = settings.firestore_agent_runs_collection
    snap = client.collection(collection).document(run_id).get()
    if not snap.exists:
        return None
    return snap.to_dict()


def update_agent_run(run_id: str, fields: dict[str, Any]) -> None:
    """
    Partial update to one `agent_runs` row. Use for status transitions
    (pending → running → completed) where only a few fields change.

    Implemented as set(merge=True) so the call is upsert-safe: if the stub
    write failed for any reason, the runner's terminal write still lands a
    record rather than silently dropping the run.
    """
    client = _get_client()
    settings = get_settings()
    collection = settings.firestore_agent_runs_collection
    client.collection(collection).document(run_id).set(fields, merge=True)


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


def get_document_state(document_id: str) -> dict[str, Any] | None:
    """
    Cheap read of a `documents`-collection row, returned as a raw dict.

    Used by the ingest script to decide whether a file has already been
    fully ingested at its current `drive_modified_time` so it can skip the
    download + chunk + embed cycle. Returns None if no row exists.

    Kept dict-shaped on purpose — we don't want to reconstruct a full
    Document model just to read two fields, and partial rows (status=
    "processing", missing optional fields) shouldn't fail Pydantic
    validation.
    """
    client = _get_client()
    settings = get_settings()
    collection = settings.firestore_documents_collection
    snap = client.collection(collection).document(document_id).get()
    if not snap.exists:
        return None
    return snap.to_dict()


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

    query = collection.where(filter=FieldFilter("document_id", "==", document_id))
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


def find_chunks_by_filters(
    *,
    contact_ids: Iterable[str] | None = None,
    municipalities: Iterable[str] | None = None,
    document_types: Iterable[str] | None = None,
    data_sources: Iterable[str] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """
    Identity-filtered chunk retrieval for agent context assembly.

    Pre-filtered vector search is the V1 retrieval strategy (locked
    2026-04-23), but the Emailer's Simmer flow doesn't need a semantic
    query — recent context about THIS contact is the right shape. So
    this helper is the filter-only path: callers pass any combination of
    contact_id, municipality, document_type, or data_source filters and
    get back a bounded set of raw chunk dicts.

    Filter semantics:
      - Within one filter dimension, multiple values are an OR via
        Firestore's `array-contains-any` (chunks store identity fields
        as arrays, even when single-valued).
      - Across dimensions, results are AND-ed.
      - Firestore allows at most one `array-contains-any` per query, so
        contact_ids and municipalities can't both be passed today; pick
        the one that matches the V1 ingest reality (municipality, since
        contact_id isn't populated yet).

    Returns:
        list of raw chunk dicts (Firestore document data). Empty list if
        no chunks match. Order is whatever Firestore returns — V1
        accepts that; recency ordering is a V2 enhancement once chunks
        carry a denormalized ingested_at field.

    Raises:
        ValueError if both contact_ids and municipalities are passed
        (Firestore can't do two array-contains-any in one query).
    """
    client = _get_client()
    settings = get_settings()
    collection = client.collection(settings.firestore_chunks_collection)

    contact_list = list(contact_ids) if contact_ids else []
    municipality_list = list(municipalities) if municipalities else []
    if contact_list and municipality_list:
        raise ValueError(
            "Pass contact_ids OR municipalities, not both — Firestore "
            "supports at most one array-contains-any per query."
        )

    query = collection
    if contact_list:
        query = query.where(
            filter=FieldFilter("contact_id", "array_contains_any", contact_list)
        )
    elif municipality_list:
        query = query.where(
            filter=FieldFilter("municipality", "array_contains_any", municipality_list)
        )

    if document_types:
        doc_type_list = list(document_types)
        if len(doc_type_list) == 1:
            query = query.where(
                filter=FieldFilter("document_type", "==", doc_type_list[0])
            )
        else:
            query = query.where(
                filter=FieldFilter("document_type", "in", doc_type_list)
            )

    if data_sources:
        ds_list = list(data_sources)
        if len(ds_list) == 1:
            query = query.where(
                filter=FieldFilter("data_source", "==", ds_list[0])
            )
        else:
            query = query.where(
                filter=FieldFilter("data_source", "in", ds_list)
            )

    query = query.limit(limit)
    return [snap.to_dict() for snap in query.stream()]


class FirestoreRepo:
    """Stateful wrapper for places that prefer object-style access. Sprint 2+."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._db = None

    async def put_agent_run(self, run_id: str, record: dict) -> None:
        raise NotImplementedError("Use module-level put_agent_run() for now.")

    async def get_agent_run(self, run_id: str) -> dict | None:
        raise NotImplementedError("Wire when something actually reads agent_runs.")
