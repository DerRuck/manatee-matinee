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
    # native timestamps (for ordering + range queries). Override after dump.
    record["drive_modified_time"] = document.drive_modified_time
    if document.ingested_at is not None:
        record["ingested_at"] = document.ingested_at
    if document.event_time is not None:
        record["event_time"] = document.event_time
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
        # Override event_time so Firestore stores it as a native timestamp
        # (model_dump(mode="json") would serialize it to an ISO string).
        if chunk.event_time is not None:
            record["event_time"] = chunk.event_time
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


def iter_documents() -> Iterable[dict[str, Any]]:
    """
    Yield raw document dicts from the `documents` collection.

    Returns each doc's stored data with `document_id` populated from the
    Firestore document ID (in case the field is missing on legacy rows).
    Used by maintenance scripts (backfills, audits). No ordering guarantee.
    """
    client = _get_client()
    settings = get_settings()
    collection = client.collection(settings.firestore_documents_collection)
    for snap in collection.stream():
        data = snap.to_dict() or {}
        data.setdefault("document_id", snap.id)
        yield data


def update_document_fields(document_id: str, fields: dict[str, Any]) -> None:
    """
    Partial update to one `documents` row via set(merge=True). Used by
    maintenance scripts that need to add or correct individual fields
    without rewriting the whole document.
    """
    client = _get_client()
    settings = get_settings()
    collection = settings.firestore_documents_collection
    client.collection(collection).document(document_id).set(fields, merge=True)


def update_chunks_for_document(
    document_id: str, fields: dict[str, Any], batch_size: int = 400
) -> int:
    """
    Apply a partial update (set merge=True) of `fields` to every chunk row
    whose document_id matches. Returns the count updated.

    Symmetric with delete_chunks_for_document — used by maintenance scripts
    to backfill or correct chunk metadata without re-embedding.
    """
    client = _get_client()
    settings = get_settings()
    collection = client.collection(settings.firestore_chunks_collection)

    query = collection.where(filter=FieldFilter("document_id", "==", document_id))
    updated = 0
    batch = client.batch()
    in_batch = 0

    for snap in query.stream():
        batch.set(snap.reference, fields, merge=True)
        in_batch += 1
        updated += 1
        if in_batch >= batch_size:
            batch.commit()
            batch = client.batch()
            in_batch = 0

    if in_batch > 0:
        batch.commit()

    return updated


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


def get_contact_by_email(email: str) -> dict[str, Any] | None:
    """
    Find one contact in `contacts` by email address. Returns the doc dict
    (including doc.id as ghl_contact_id if not already on the record) or None.

    Lookup is case-insensitive on the email field — the GHL sync stores
    emails as-received, so we lowercase both sides for matching.
    """
    if not email:
        return None
    client = _get_client()
    settings = get_settings()
    coll = client.collection(settings.firestore_contacts_collection)

    # Try exact match first (fast path when both sides happen to match case).
    snapshots = list(
        coll.where(filter=FieldFilter("email", "==", email))
        .limit(1)
        .stream()
    )
    if not snapshots:
        snapshots = list(
            coll.where(filter=FieldFilter("email", "==", email.lower()))
            .limit(1)
            .stream()
        )
    if not snapshots:
        return None

    doc = snapshots[0]
    data = doc.to_dict() or {}
    data.setdefault("ghl_contact_id", doc.id)
    return data


def get_municipality(slug: str) -> dict[str, Any] | None:
    """Read one row from `municipalities` by slug doc-ID. None if missing."""
    if not slug:
        return None
    client = _get_client()
    settings = get_settings()
    snap = (
        client.collection(settings.firestore_municipalities_collection)
        .document(slug)
        .get()
    )
    if not snap.exists:
        return None
    return snap.to_dict()


def upsert_municipality(slug: str, data: dict[str, Any]) -> bool:
    """
    Upsert one `municipalities` row, keyed by slug. Set(merge=True) so partial
    updates are safe — used by the GHL sync to refresh county/jurisdiction_type
    from the TAG_TO_MUNICIPALITY mapping on every re-sync without clobbering
    contact_count or status.

    Returns True if the doc was created (didn't exist before), False if updated.
    """
    if not slug:
        raise ValueError("upsert_municipality requires a non-empty slug")
    client = _get_client()
    settings = get_settings()
    doc_ref = (
        client.collection(settings.firestore_municipalities_collection)
        .document(slug)
    )
    created = not doc_ref.get().exists
    doc_ref.set(data, merge=True)
    return created


def upsert_contact(contact_id: str, data: dict[str, Any]) -> bool:
    """
    Upsert one `contacts` row, keyed by GHL contact ID. Set(merge=True) so
    repeated syncs layer cleanly without erasing fields that the current
    write didn't touch.

    Returns True if the doc was created, False if updated.
    """
    if not contact_id:
        raise ValueError("upsert_contact requires a non-empty contact_id")
    client = _get_client()
    settings = get_settings()
    doc_ref = (
        client.collection(settings.firestore_contacts_collection)
        .document(contact_id)
    )
    created = not doc_ref.get().exists
    doc_ref.set(data, merge=True)
    return created


def iter_contacts() -> Iterable[dict[str, Any]]:
    """
    Yield raw contact dicts from the `contacts` collection.

    Each dict has `ghl_contact_id` populated from the Firestore doc ID even
    when the field isn't on the stored record. No ordering guarantee.
    Used by sync rollups (contact_count refresh) and maintenance scripts.
    """
    client = _get_client()
    settings = get_settings()
    coll = client.collection(settings.firestore_contacts_collection)
    for snap in coll.stream():
        data = snap.to_dict() or {}
        data.setdefault("ghl_contact_id", snap.id)
        yield data


def search_contacts(
    *,
    first_name: str | None = None,
    last_name: str | None = None,
    municipality_slug: str | None = None,
    query: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """
    Search `contacts` by name + optional municipality.

    V1 strategy: Firestore equality filters. Small dataset, exact match is fine.
    `query` is a free-text fallback that treats the value as a first_name match
    (used when the workbook can't cleanly split the user's phrasing).

    Case handling: GHL stores names mixed-case and the V1 backfill writes them
    as-is. The workbook lowercases inputs before calling; if that proves
    brittle, add a `first_name_lower` denorm column on the contact doc and
    filter against that instead.

    Returns:
        List of raw contact dicts, with `ghl_contact_id` populated from the
        doc ID. Empty list when no match.
    """
    client = _get_client()
    settings = get_settings()
    col = client.collection(settings.firestore_contacts_collection)

    q = col
    if first_name:
        q = q.where(filter=FieldFilter("first_name", "==", first_name))
    elif query:
        q = q.where(filter=FieldFilter("first_name", "==", query))
    if last_name:
        q = q.where(filter=FieldFilter("last_name", "==", last_name))
    if municipality_slug:
        q = q.where(
            filter=FieldFilter("municipality_slug", "==", municipality_slug)
        )

    results: list[dict[str, Any]] = []
    for snap in q.limit(limit).stream():
        data = snap.to_dict() or {}
        data.setdefault("ghl_contact_id", snap.id)
        results.append(data)
    return results


# --- Drive watch state -----------------------------------------------------
# The /webhooks/drive handler reads + advances a pageToken stored at
# system/drive_watch_state. drive_watch.py persists the initial token after
# creating the channel; the webhook advances it after each changes.list call.

_DRIVE_WATCH_STATE_DOC = "drive_watch_state"


def get_drive_watch_state() -> dict[str, Any] | None:
    """Read the Drive watch state doc, or None if not yet initialized."""
    client = _get_client()
    settings = get_settings()
    snap = (
        client.collection(settings.firestore_system_collection)
        .document(_DRIVE_WATCH_STATE_DOC)
        .get()
    )
    if not snap.exists:
        return None
    return snap.to_dict()


def set_drive_watch_state(page_token: str, **extra) -> None:
    """
    Upsert the Drive watch state. `page_token` is what Drive returned on the
    last changes.list call; everything else (channel_id, resource_id, etc.)
    can be passed as keyword args and is merged in.
    """
    client = _get_client()
    settings = get_settings()
    payload = {"page_token": page_token, **extra}
    (
        client.collection(settings.firestore_system_collection)
        .document(_DRIVE_WATCH_STATE_DOC)
        .set(payload, merge=True)
    )


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
