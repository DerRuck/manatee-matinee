"""
Ingest the AI Infrastructure Drive corpus into Firestore.

Run from backend/:
    python -m scripts.ingest_demo_corpus --folder-id <ROOT> --source <name>

`--source` selects which top-level subfolder under the root to ingest:
    plaud | leads | industry_context | email | iflytek | all

The script knows how each source folder is shaped and dispatches to a
per-source resolver. Resolvers decide:
  - Skip this file? (and why)
  - If not, what document_type / municipality / contact_id / project_name?

Re-ingest is idempotent and dedupe-aware. Document IDs equal Drive file
IDs, so a re-run won't create duplicate Firestore rows. Files whose
documents-collection row is already `completed` at the current Drive
`modifiedTime` skip the download + embed entirely (no Vertex spend).
Files that have actually changed get re-chunked and re-embedded; chunks
for the document are deleted and rewritten in one cycle.

Per-source resolvers:
    Plaud Files       — only Google Doc transcripts; skip raw JSON, audio,
                        sentinels. document_type=meeting_notes; municipality
                        from path/filename keyword scan.
    Leads             — keyed by <Municipality> subfolder. document_type
                        from filename ("Mail" -> email, "Letter" -> letter,
                        "Notes"/"Prep" -> meeting_notes, else -> other).
    Industry Context  — External Context -> research_report,
                        What is C-HAWQ -> internal_policy. No municipality.
    Email data        — <account>/<YYYY-MM>/files. .txt -> email; PDF
                        attachments -> other; other types skipped.
    Iflytek Files     — flat folder, file triplets per recording. PDFs
                        only (the .txt sidecars are 0 bytes); audio +
                        empty txt skipped. document_type=meeting_notes.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# Allow running as a script from inside backend/ without -m gymnastics.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

# Load .env from repo root before anything reads settings/credentials.
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from ingestion.chunker import chunk_text  # noqa: E402
from services.drive.client import (  # noqa: E402
    FOLDER_MIME,
    GOOGLE_DOC_MIME,
    PDF_MIME,
    DOCX_MIME,
    download_file_as_text,
    get_file_metadata,
    is_text_extractable_mime,
    list_folder_files,
    walk_folder,
)
from services.embeddings.vertex import embed_texts  # noqa: E402
from services.firestore.client import (  # noqa: E402
    delete_chunks_for_document,
    get_document_state,
    put_chunks_bulk,
    put_document,
)
from services.firestore.schema import Chunk, Document, DocumentType  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("ingest")


# ---------------------------------------------------------------------------
# Source folder configuration
# ---------------------------------------------------------------------------

# Maps the --source CLI value to the folder name expected directly under the
# AI Infrastructure root, plus the data_source slug stored on every document
# and chunk produced from that source.
@dataclass(frozen=True)
class SourceConfig:
    folder_name: str
    data_source: str
    resolver: Callable[[list[str], dict], "IngestDecision"]


# Filled in below the resolver definitions so we can reference them.
SOURCE_CONFIGS: dict[str, SourceConfig] = {}


# ---------------------------------------------------------------------------
# Municipality slug map (extend as new pilots come online)
# ---------------------------------------------------------------------------

KNOWN_MUNICIPALITY_SLUGS: dict[str, str] = {
    "rookery bay": "rookery_bay_fl",
    "sfwmd": "sfwmd_fl",
    "boynton beach": "boynton_beach_fl",
    "st.petersburg": "st_petersburg_fl",
    "st petersburg": "st_petersburg_fl",
    "naples": "naples_fl",
    "marco island": "marco_island_fl",
}


def _slugify(name: str) -> str:
    """Lowercase, spaces -> underscores, drop non-[a-z0-9_]."""
    s = name.strip().lower()
    s = re.sub(r"[\s\-]+", "_", s)
    s = re.sub(r"[^a-z0-9_]", "", s)
    return s.strip("_")


def _slug_for_municipality(name: str) -> str:
    """Best-effort canonical slug for a municipality token."""
    norm = name.strip().lower()
    if norm in KNOWN_MUNICIPALITY_SLUGS:
        return KNOWN_MUNICIPALITY_SLUGS[norm]
    return f"{_slugify(name)}_fl"


def _scan_known_municipalities(tokens: list[str]) -> list[str]:
    """
    Scan a list of strings (path segments + filename) for known municipality
    keywords and return the canonical slugs in deterministic order.

    Only matches on the KNOWN map — no slugify fallback here, because we
    don't want a stray word in a filename to invent a new municipality slug.
    """
    found: list[str] = []
    seen: set[str] = set()
    haystack = " ".join(tokens).lower()
    for keyword, slug in KNOWN_MUNICIPALITY_SLUGS.items():
        if keyword in haystack and slug not in seen:
            found.append(slug)
            seen.add(slug)
    return found


# ---------------------------------------------------------------------------
# Per-source resolver result
# ---------------------------------------------------------------------------

@dataclass
class IngestDecision:
    """Per-file ingest plan: skip with reason, or proceed with metadata."""

    skip: bool = False
    reason: Optional[str] = None
    document_type: Optional[DocumentType] = None
    municipality: list[str] = field(default_factory=list)
    project_name: list[str] = field(default_factory=list)
    contact_id: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------

def resolve_plaud(path_segments: list[str], file_meta: dict) -> IngestDecision:
    """
    Plaud session folders contain one Google Doc (the canonical transcript +
    AI summary) plus a pile of raw JSON, audio, and Plaud sentinel files.
    Only the Google Doc gets ingested.
    """
    name = file_meta.get("name", "")
    mime = file_meta.get("mimeType", "")

    if name.startswith("."):
        return IngestDecision(skip=True, reason="plaud sentinel file")
    if mime != GOOGLE_DOC_MIME:
        return IngestDecision(skip=True, reason=f"plaud non-canonical mime {mime}")

    municipalities = _scan_known_municipalities(path_segments + [name])
    return IngestDecision(
        document_type="meeting_notes",
        municipality=municipalities,
    )


def resolve_leads(path_segments: list[str], file_meta: dict) -> IngestDecision:
    """
    Leads folder is keyed by municipality. Document type is inferred from
    filename keywords; unrecognized files become "other".

    Files directly under Leads/ (with no municipality subfolder) are
    treated as general lead-qualification material — no municipality.
    """
    name = file_meta.get("name", "")
    mime = file_meta.get("mimeType", "")

    if not is_text_extractable_mime(mime):
        return IngestDecision(skip=True, reason=f"non-text mime {mime}")

    # path_segments[0] == "Leads" by construction. The municipality, if
    # present, is path_segments[1].
    municipalities: list[str] = []
    if len(path_segments) >= 2:
        municipalities = [_slug_for_municipality(path_segments[1])]

    lower = name.lower()
    doc_type: DocumentType
    if "mail" in lower:
        doc_type = "email"
    elif "letter" in lower:
        doc_type = "letter"
    elif "notes" in lower or "note" in lower:
        doc_type = "meeting_notes"
    elif "prep" in lower:
        doc_type = "meeting_notes"
    elif "presentation" in lower or "deck" in lower or "slides" in lower:
        doc_type = "presentation"
    else:
        doc_type = "other"

    return IngestDecision(document_type=doc_type, municipality=municipalities)


def resolve_industry_context(
    path_segments: list[str], file_meta: dict
) -> IngestDecision:
    """
    Industry Context has two subfolders: External Context (research material
    about partners, agencies, watershed conditions) and What is C-HAWQ
    (internal positioning, mission docs).
    """
    mime = file_meta.get("mimeType", "")
    if not is_text_extractable_mime(mime):
        return IngestDecision(skip=True, reason=f"non-text mime {mime}")

    subcategory = path_segments[1].lower() if len(path_segments) >= 2 else ""
    if "what is c-hawq" in subcategory or "what is chawq" in subcategory:
        return IngestDecision(document_type="internal_policy")
    if "external context" in subcategory:
        return IngestDecision(document_type="research_report")

    # Files at the root of Industry Context default to research_report
    # (the more conservative of the two).
    return IngestDecision(document_type="research_report")


def resolve_email(path_segments: list[str], file_meta: dict) -> IngestDecision:
    """
    Email data: <account>/<YYYY-MM>/files. The .txt files are the email
    bodies (with a small summary at the top); PDFs are attachments. Other
    attachment types get skipped — V1 doesn't try to parse them.
    """
    name = file_meta.get("name", "")
    mime = file_meta.get("mimeType", "")
    lower = name.lower()

    if mime == GOOGLE_DOC_MIME or lower.endswith(".txt") or mime in {
        "text/plain",
        "text/markdown",
    }:
        return IngestDecision(document_type="email")

    if mime == PDF_MIME or lower.endswith(".pdf"):
        return IngestDecision(document_type="other")

    if mime == DOCX_MIME or lower.endswith(".docx"):
        return IngestDecision(document_type="other")

    return IngestDecision(skip=True, reason=f"email attachment mime {mime}")


def resolve_iflytek(path_segments: list[str], file_meta: dict) -> IngestDecision:
    """
    Iflytek folder is a flat dump of triplets per recording: .opus audio,
    a 0-byte .txt sidecar, and a .pdf with the actual transcript. Only
    PDFs get ingested. Municipality comes from the filename.
    """
    name = file_meta.get("name", "")
    mime = file_meta.get("mimeType", "")
    size = int(file_meta.get("size", 0) or 0)

    if size == 0:
        return IngestDecision(skip=True, reason="empty file")

    if mime != PDF_MIME and not name.lower().endswith(".pdf"):
        return IngestDecision(skip=True, reason=f"iflytek non-pdf mime {mime}")

    municipalities = _scan_known_municipalities(path_segments + [name])
    return IngestDecision(
        document_type="meeting_notes",
        municipality=municipalities,
    )


# Wire resolvers into the source configuration. Folder names match the
# actual Drive folder titles and must stay in sync with the user's layout.
SOURCE_CONFIGS.update(
    plaud=SourceConfig("Plaud Files", "plaud", resolve_plaud),
    leads=SourceConfig("Leads", "leads", resolve_leads),
    industry_context=SourceConfig(
        "Industry Context", "industry_context", resolve_industry_context
    ),
    email=SourceConfig("Email data", "email_data", resolve_email),
    iflytek=SourceConfig("Iflytek Files", "iflytek", resolve_iflytek),
)


# ---------------------------------------------------------------------------
# Orchestration
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


def _resolve_source_root(
    ai_infra_folder_id: str, source_key: str
) -> tuple[str, SourceConfig]:
    """Find the source-folder ID under the AI Infrastructure root."""
    cfg = SOURCE_CONFIGS[source_key]
    for child in list_folder_files(ai_infra_folder_id):
        if (
            child.get("mimeType") == FOLDER_MIME
            and child.get("name") == cfg.folder_name
        ):
            return child["id"], cfg
    raise ValueError(
        f"AI Infrastructure folder has no subfolder named '{cfg.folder_name}' "
        f"(needed for --source {source_key})"
    )


def _is_already_ingested(file_meta: dict) -> bool:
    """
    Has this Drive file already been fully ingested at its current
    drive_modified_time? If so, the resolver, download, chunker, and
    embedder can all be skipped — the chunks already in Firestore are
    still authoritative.

    Force a re-ingest by either editing the Drive file (modifiedTime
    changes) or deleting the documents row in Firestore.
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
    # Both ends store tz-aware UTC datetimes. Exact equality is fine —
    # Drive returns ms precision and Firestore preserves the value as
    # written, so the round-trip is deterministic.
    return stored_mtime == fresh_mtime


def _ingest_one_file(
    file_meta: dict,
    path_segments: list[str],
    source_cfg: SourceConfig,
    stats: IngestStats,
) -> None:
    """Fully ingest one file: resolve, download, chunk, embed, write."""
    file_id = file_meta["id"]
    name = file_meta.get("name", "<no-name>")
    mime = file_meta.get("mimeType", "")
    log_extra = {"file_id": file_id, "file_name": name, "path": "/".join(path_segments)}

    # Cheap dedupe gate: if Firestore already has a completed row at this
    # drive_modified_time, the existing chunks are still correct and
    # downloading + re-embedding would just waste Vertex spend.
    if _is_already_ingested(file_meta):
        logger.info("skip — already ingested (unchanged)", extra=log_extra)
        stats.record_skip("already ingested (unchanged)")
        return

    decision = source_cfg.resolver(path_segments, file_meta)
    if decision.skip:
        reason = decision.reason or "unspecified"
        logger.info("skip — %s", reason, extra=log_extra)
        stats.record_skip(reason)
        return

    text = download_file_as_text(file_id, mime, file_name=name)
    if not text:
        logger.info("skip — no extractable text", extra=log_extra)
        stats.record_skip("no extractable text")
        return

    chunks = chunk_text(text)
    if not chunks:
        logger.info("skip — empty after chunking", extra=log_extra)
        stats.record_skip("empty after chunking")
        return

    # Build & write the Document row first (status=processing) so failures
    # mid-embed leave a discoverable trail rather than a vanished record.
    doc = Document(
        document_id=file_id,
        drive_file_id=file_id,
        drive_file_name=name,
        drive_mime_type=mime,
        drive_modified_time=_parse_drive_time(file_meta.get("modifiedTime")),
        drive_web_view_link=file_meta.get("webViewLink"),
        drive_parent_folder_id=(file_meta.get("parents") or [None])[0],
        ingestion_status="processing",
        chunk_count=len(chunks),
        document_type=decision.document_type or "other",
        contact_id=decision.contact_id,
        municipality=decision.municipality,
        project_name=decision.project_name,
        data_source=source_cfg.data_source,
    )
    put_document(doc)

    # Embed + write chunks. Re-ingest? Drop the old chunks first.
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
        )
        for c, emb in zip(chunks, embeddings)
    ]
    written = put_chunks_bulk(chunk_models)
    stats.chunks_written += written

    # Flip the document row to completed.
    doc.ingestion_status = "completed"
    doc.ingested_at = datetime.now(timezone.utc)
    put_document(doc)
    stats.documents_ingested += 1
    logger.info(
        "ingested — %d chunks (%s, mu=%s)",
        written,
        decision.document_type,
        decision.municipality or "-",
        extra=log_extra,
    )


def _parse_drive_time(value: Optional[str]) -> datetime:
    """RFC 3339 -> datetime. Drive returns Zulu; fall back to now() on miss."""
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _ingest_one_source(
    ai_infra_folder_id: str, source_key: str, stats: IngestStats
) -> None:
    """Walk one source folder and ingest every leaf the resolver accepts."""
    src_folder_id, cfg = _resolve_source_root(ai_infra_folder_id, source_key)
    logger.info(
        "ingest source=%s data_source=%s root_folder=%s",
        source_key,
        cfg.data_source,
        src_folder_id,
    )
    # walk_folder yields path segments relative to the source folder (so
    # the source folder name itself is NOT included). Re-prefix it so
    # resolvers see the full lineage (source-folder-name first), which
    # makes their pattern matching simpler.
    for file_meta, sub_segments in walk_folder(src_folder_id):
        path_segments = [cfg.folder_name] + list(sub_segments)
        stats.documents_attempted += 1
        try:
            _ingest_one_file(file_meta, path_segments, cfg, stats)
        except Exception:
            stats.documents_failed += 1
            logger.exception(
                "ingest error",
                extra={
                    "file_id": file_meta.get("id"),
                    "name": file_meta.get("name"),
                    "path": "/".join(path_segments),
                },
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--folder-id",
        required=True,
        help="Drive folder ID of the AI Infrastructure root.",
    )
    parser.add_argument(
        "--source",
        required=True,
        choices=list(SOURCE_CONFIGS.keys()) + ["all"],
        help="Which source subfolder to ingest. 'all' walks every mapped one.",
    )
    args = parser.parse_args()

    stats = IngestStats()
    started = time.monotonic()
    sources_to_run = (
        list(SOURCE_CONFIGS.keys()) if args.source == "all" else [args.source]
    )

    for source_key in sources_to_run:
        try:
            _ingest_one_source(args.folder_id, source_key, stats)
        except Exception:
            logger.exception("source-level failure", extra={"source": source_key})

    elapsed = time.monotonic() - started
    logger.info(
        "ingest summary — attempted=%d ingested=%d skipped=%d failed=%d chunks=%d elapsed=%.1fs",
        stats.documents_attempted,
        stats.documents_ingested,
        stats.documents_skipped,
        stats.documents_failed,
        stats.chunks_written,
        elapsed,
    )
    if stats.skip_reasons:
        logger.info("skip reasons:")
        for reason, count in sorted(
            stats.skip_reasons.items(), key=lambda kv: -kv[1]
        ):
            logger.info("  %4d  %s", count, reason)


if __name__ == "__main__":
    main()
