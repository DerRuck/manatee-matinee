"""
Ingest orchestrator — the single-file entry point for Drive -> Firestore.

`ingest_one_drive_file(file_id, source_hint=None)` is what the Drive
webhook handler, the research-agent end-of-run hook, and the bulk_ingest
CLI all call. It handles:

  - Source resolution (which SourceConfig applies to this file)
  - Idempotency (skip files unchanged since last successful ingest)
  - Resolver dispatch
  - Body download + optional content-based enrichment
  - Chunking + embedding
  - Firestore documents + chunks write
  - Status transitions on the documents row

Migrated from scripts/ingest_demo_corpus.py on 2026-05-20. The CLI walker
that used to live in that script now lives in scripts/bulk_ingest.py;
this module owns the per-file work.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ingestion.chunker import chunk_text
from ingestion.resolvers import IngestDecision, SourceConfig, SOURCE_CONFIGS
from services.drive.client import (
    FOLDER_MIME,
    download_file_as_text,
    get_file_metadata,
)
from services.embeddings.vertex import embed_texts
from services.firestore.client import (
    delete_chunks_for_document,
    get_document_state,
    put_chunks_bulk,
    put_document,
)
from services.firestore.schema import Chunk, Document

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-run statistics
# ---------------------------------------------------------------------------

@dataclass
class IngestStats:
    documents_attempted: int = 0
    documents_ingested: int = 0
    documents_skipped: int = 0
    documents_failed: int = 0
    chunks_written: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)

    def record_skip(self, reason: str) -> None:
        self.documents_skipped += 1
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_drive_time(value: Optional[str]) -> datetime:
    """RFC 3339 Zulu -> tz-aware datetime. Falls back to now() on miss."""
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _is_already_ingested(file_meta: dict) -> bool:
    """
    Skip download + chunk + embed if the documents row is `completed` and
    drive_modified_time matches what's already stored.
    """
    file_id = file_meta["id"]
    existing = get_document_state(file_id)
    if not existing:
        return False
    if existing.get("ingestion_status") != "completed":
        return False
    fresh_mtime = _parse_drive_time(file_meta.get("modifiedTime"))
    stored_mtime = existing.get("drive_modified_time")
    if not isinstance(stored_mtime, datetime):
        return False
    return stored_mtime == fresh_mtime


def _walk_up_to_source(file_meta: dict) -> tuple[Optional[SourceConfig], list[str]]:
    """
    Walk the parent chain of a Drive file to find the SourceConfig whose
    folder_name appears anywhere in the lineage. Returns (cfg, path_segments)
    where path_segments lists folder names from the source folder DOWN to
    (but not including) the file itself.

    Used when ingest_one_drive_file is invoked without an explicit source_hint
    (e.g., from the Drive webhook). Returns (None, []) if no source matches.
    """
    folder_to_cfg = {cfg.folder_name: cfg for cfg in SOURCE_CONFIGS.values()}

    # Walk up via parents, collecting folder names in order from file -> root.
    # Stop once we hit a folder name we recognize.
    chain: list[str] = []
    parents = file_meta.get("parents") or []
    while parents:
        parent_id = parents[0]
        parent_meta = get_file_metadata(parent_id)
        if parent_meta is None:
            break
        name = parent_meta.get("name", "")
        chain.append(name)
        if name in folder_to_cfg:
            # path_segments goes source-down: [source_folder, ..., parent_of_file].
            return folder_to_cfg[name], list(reversed(chain))
        parents = parent_meta.get("parents") or []

    return None, []


# ---------------------------------------------------------------------------
# Single-file ingest entry point
# ---------------------------------------------------------------------------

def ingest_one_drive_file(
    file_id: str,
    source_hint: Optional[str] = None,
    stats: Optional[IngestStats] = None,
) -> None:
    """
    Ingest one Drive file into Firestore.

    Args:
        file_id: Drive file ID.
        source_hint: Optional source key (e.g. "email_inbox"). If omitted,
            the orchestrator walks the file's parent chain to find a
            matching SourceConfig. Passing the hint when known is cheaper
            (skips parent walks).
        stats: Optional IngestStats to accumulate into. Useful for the CLI
            walker; webhook callers can omit.

    No return value -- raises only on programmer errors. File-level
    failures (Drive 404, embed failure, etc.) are caught and logged with
    a documents row left in `error` status for visibility.
    """
    if stats is None:
        stats = IngestStats()

    file_meta = get_file_metadata(file_id)
    if file_meta is None:
        logger.warning("ingest: Drive file not found id=%s", file_id)
        stats.record_skip("drive file not found")
        return

    # Don't try to ingest folders themselves.
    if file_meta.get("mimeType") == FOLDER_MIME:
        stats.record_skip("is folder")
        return

    # Cheap dedupe gate before any resolver / download work.
    if _is_already_ingested(file_meta):
        logger.info(
            "ingest: skip (already ingested unchanged) id=%s name=%s",
            file_id,
            file_meta.get("name"),
        )
        stats.record_skip("already ingested (unchanged)")
        return

    # Resolve source.
    if source_hint:
        cfg = SOURCE_CONFIGS.get(source_hint)
        if cfg is None:
            logger.error("ingest: unknown source_hint=%s id=%s", source_hint, file_id)
            stats.record_skip(f"unknown source_hint {source_hint}")
            return
        # When the hint is provided, we still need path_segments. Walk the
        # chain anyway -- it's only used by some resolvers (Leads,
        # industry_context, iflytek path-matching municipality scan).
        _, path_segments = _walk_up_to_source(file_meta)
        if not path_segments:
            path_segments = [cfg.folder_name]
    else:
        cfg, path_segments = _walk_up_to_source(file_meta)
        if cfg is None:
            logger.info(
                "ingest: no source matches file's parent chain id=%s name=%s",
                file_id,
                file_meta.get("name"),
            )
            stats.record_skip("no source match")
            return

    stats.documents_attempted += 1

    try:
        _ingest_one_file(file_meta, path_segments, cfg, stats)
    except Exception:
        stats.documents_failed += 1
        logger.exception(
            "ingest: error id=%s name=%s",
            file_id,
            file_meta.get("name"),
        )


def _ingest_one_file(
    file_meta: dict,
    path_segments: list[str],
    source_cfg: SourceConfig,
    stats: IngestStats,
) -> None:
    """Resolve, download, optionally enrich, chunk, embed, write."""
    file_id = file_meta["id"]
    name = file_meta.get("name", "<no-name>")
    mime = file_meta.get("mimeType", "")
    log_extra = {
        "file_id": file_id,
        "file_name": name,
        "path": "/".join(path_segments),
    }

    decision = source_cfg.resolver(path_segments, file_meta)
    if decision.skip:
        reason = decision.reason or "unspecified"
        logger.info("skip - %s", reason, extra=log_extra)
        stats.record_skip(reason)
        return

    text = download_file_as_text(file_id, mime, file_name=name)
    if not text:
        logger.info("skip - no extractable text", extra=log_extra)
        stats.record_skip("no extractable text")
        return

    # Content-based enrichment (currently used only by email_inbox to parse
    # the summary header into contact_id, municipality, message_id, etc.).
    if source_cfg.enrich_from_text is not None:
        try:
            source_cfg.enrich_from_text(decision, text)
        except Exception:
            logger.exception("enrich_from_text raised", extra=log_extra)

    chunks = chunk_text(text)
    if not chunks:
        logger.info("skip - empty after chunking", extra=log_extra)
        stats.record_skip("empty after chunking")
        return

    drive_modified = _parse_drive_time(file_meta.get("modifiedTime"))
    # event_time = when the underlying event happened. Email resolvers
    # set this from the RFC 2822 Date header (so backfilled scrapes
    # don't collapse to today). For sources without a wrapper layer
    # (Plaud, leads, industry context, iflytek), drive_modified_time
    # is the right answer — the file mtime IS the event time.
    event_time = decision.event_time or drive_modified

    doc = Document(
        document_id=file_id,
        drive_file_id=file_id,
        drive_file_name=name,
        drive_mime_type=mime,
        drive_modified_time=drive_modified,
        drive_web_view_link=file_meta.get("webViewLink"),
        drive_parent_folder_id=(file_meta.get("parents") or [None])[0],
        ingestion_status="processing",
        chunk_count=len(chunks),
        document_type=decision.document_type or "other",
        contact_id=decision.contact_id,
        municipality=decision.municipality,
        project_name=decision.project_name,
        data_source=source_cfg.data_source,
        email_message_id=decision.email_message_id,
        email_thread_id=decision.email_thread_id,
        email_direction=decision.email_direction,
        event_time=event_time,
    )
    put_document(doc)

    delete_chunks_for_document(file_id)
    embeddings = embed_texts([c.text for c in chunks], task="RETRIEVAL_DOCUMENT")
    chunk_models = [
        Chunk(
            chunk_id=f"{file_id}__{c.chunk_index:04d}",
            document_id=file_id,
            chunk_index=c.chunk_index,
            text=c.text,
            embedding=emb,
            token_count=c.token_count,
            char_count=c.char_count,
            document_type=decision.document_type or "other",
            contact_id=decision.contact_id,
            municipality=decision.municipality,
            project_name=decision.project_name,
            data_source=source_cfg.data_source,
            email_message_id=decision.email_message_id,
            email_thread_id=decision.email_thread_id,
            email_direction=decision.email_direction,
            event_time=event_time,
        )
        for c, emb in zip(chunks, embeddings)
    ]
    written = put_chunks_bulk(chunk_models)
    stats.chunks_written += written

    doc.ingestion_status = "completed"
    doc.ingested_at = datetime.now(timezone.utc)
    put_document(doc)
    stats.documents_ingested += 1
    logger.info(
        "ingested - %d chunks (%s, mu=%s)",
        written,
        decision.document_type,
        decision.municipality or "-",
        extra=log_extra,
    )
