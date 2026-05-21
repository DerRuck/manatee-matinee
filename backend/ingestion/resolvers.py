"""
Per-source ingest resolvers.

Each resolver inspects a Drive file's path + metadata and produces an
IngestDecision describing whether/how to ingest it. The orchestrator
calls the right resolver based on `--source` (or, for webhook-driven
ingestion, the file's top-level folder name).

Resolvers stay metadata-only. If a source needs the file's text content
to set identity fields (counterparty -> contact_id, etc.), it provides
an `enrich_from_text` callback on its SourceConfig. The orchestrator
downloads the text once, then calls the callback to enrich the decision.

Migrated from scripts/ingest_demo_corpus.py on 2026-05-20 as part of the
move to a library-shaped ingestion module.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Literal, Optional

from services.drive.client import (
    GOOGLE_DOC_MIME,
    PDF_MIME,
    DOCX_MIME,
    is_text_extractable_mime,
)
from services.firestore.client import get_contact_by_email
from services.firestore.schema import DocumentType

from ingestion.email_header import (
    parse_email_date_to_datetime,
    parse_email_summary_header,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Decision shape
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
    # Email-specific. Populated by enrich_email_from_text after download.
    email_message_id: Optional[str] = None
    email_thread_id: Optional[str] = None
    email_direction: Optional[Literal["inbound", "outbound", "internal"]] = None
    # When the underlying event happened. Email resolvers set this from
    # the RFC 2822 Date header; non-email sources leave it None and the
    # orchestrator falls back to drive_modified_time.
    event_time: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Source configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceConfig:
    """
    Folder name -> resolver mapping plus optional text-enrichment hook.

    `folder_name` matches the top-level subfolder under the ingestion root.
    `data_source` is the slug stored on every document and chunk from this
    source. `resolver` runs on file metadata; `enrich_from_text` (if set)
    runs after the file body is downloaded to layer in fields that require
    parsing content.
    """

    folder_name: str
    data_source: str
    resolver: Callable[[list[str], dict], IngestDecision]
    enrich_from_text: Optional[Callable[[IngestDecision, str], None]] = None


# Populated below the resolver definitions so we can reference them.
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
    s = name.strip().lower()
    s = re.sub(r"[\s\-]+", "_", s)
    s = re.sub(r"[^a-z0-9_]", "", s)
    return s.strip("_")


def _slug_for_municipality(name: str) -> str:
    norm = name.strip().lower()
    if norm in KNOWN_MUNICIPALITY_SLUGS:
        return KNOWN_MUNICIPALITY_SLUGS[norm]
    return f"{_slugify(name)}_fl"


def _scan_known_municipalities(tokens: list[str]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    haystack = " ".join(tokens).lower()
    for keyword, slug in KNOWN_MUNICIPALITY_SLUGS.items():
        if keyword in haystack and slug not in seen:
            found.append(slug)
            seen.add(slug)
    return found


# ---------------------------------------------------------------------------
# Resolvers — migrated unchanged from the demo-corpus script
# ---------------------------------------------------------------------------

def resolve_plaud(path_segments: list[str], file_meta: dict) -> IngestDecision:
    """Plaud session folders: ingest only the Google Doc transcript."""
    name = file_meta.get("name", "")
    mime = file_meta.get("mimeType", "")
    if name.startswith("."):
        return IngestDecision(skip=True, reason="plaud sentinel file")
    if mime != GOOGLE_DOC_MIME:
        return IngestDecision(skip=True, reason=f"plaud non-canonical mime {mime}")
    municipalities = _scan_known_municipalities(path_segments + [name])
    return IngestDecision(document_type="meeting_notes", municipality=municipalities)


def resolve_leads(path_segments: list[str], file_meta: dict) -> IngestDecision:
    """Leads/<Municipality>/... — type inferred from filename keywords."""
    name = file_meta.get("name", "")
    mime = file_meta.get("mimeType", "")
    if not is_text_extractable_mime(mime):
        return IngestDecision(skip=True, reason=f"non-text mime {mime}")

    municipalities: list[str] = []
    if len(path_segments) >= 2:
        municipalities = [_slug_for_municipality(path_segments[1])]

    lower = name.lower()
    if "mail" in lower:
        doc_type: DocumentType = "email"
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


def resolve_industry_context(path_segments: list[str], file_meta: dict) -> IngestDecision:
    """External Context -> research_report; What is C-HAWQ -> internal_policy."""
    mime = file_meta.get("mimeType", "")
    if not is_text_extractable_mime(mime):
        return IngestDecision(skip=True, reason=f"non-text mime {mime}")
    subcategory = path_segments[1].lower() if len(path_segments) >= 2 else ""
    if "what is c-hawq" in subcategory or "what is chawq" in subcategory:
        return IngestDecision(document_type="internal_policy")
    if "external context" in subcategory:
        return IngestDecision(document_type="research_report")
    return IngestDecision(document_type="research_report")


def resolve_iflytek(path_segments: list[str], file_meta: dict) -> IngestDecision:
    """Iflytek folder: only PDFs (audio + empty txt sidecars get skipped)."""
    name = file_meta.get("name", "")
    mime = file_meta.get("mimeType", "")
    size = int(file_meta.get("size", 0) or 0)
    if size == 0:
        return IngestDecision(skip=True, reason="empty file")
    if mime != PDF_MIME and not name.lower().endswith(".pdf"):
        return IngestDecision(skip=True, reason=f"iflytek non-pdf mime {mime}")
    municipalities = _scan_known_municipalities(path_segments + [name])
    return IngestDecision(document_type="meeting_notes", municipality=municipalities)


# ---------------------------------------------------------------------------
# Email-inbox resolver (new in 2026-05-20 refactor)
# ---------------------------------------------------------------------------

def resolve_email_inbox(path_segments: list[str], file_meta: dict) -> IngestDecision:
    """
    Email-inbox folder layout (jobs/email_scraper/main.py writes here):

        <root>/email-inbox/<YYYY-MM>/
            <counterparty>_<slug>_<subject>_summary.txt   <- the email body
            <counterparty>_<slug>_<subject>_<attachment>.pdf
            <counterparty>_<slug>_<subject>_<attachment>.docx

    The summary .txt carries a structured header block that
    enrich_email_from_text parses for identity (contact_id, municipality)
    and email metadata (message_id, thread_id, direction).

    Attachments get document_type="other" with empty identity for V1 --
    retrieval can find them by Drive proximity to the summary. Enriching
    attachments with their summary's metadata is a V2 nice-to-have.
    """
    name = file_meta.get("name", "")
    mime = file_meta.get("mimeType", "")
    lower = name.lower()

    # Summary files: full email handling, including post-download enrichment.
    if lower.endswith("_summary.txt") or mime in {"text/plain", "text/markdown"}:
        return IngestDecision(document_type="email")

    # Attachments: keep as "other" with no identity tagging in V1.
    if mime == PDF_MIME or lower.endswith(".pdf"):
        return IngestDecision(document_type="other")
    if mime == DOCX_MIME or lower.endswith(".docx"):
        return IngestDecision(document_type="other")

    # Spreadsheets and other supported attachment types fall through as
    # "other"; anything else gets dropped.
    if is_text_extractable_mime(mime):
        return IngestDecision(document_type="other")

    return IngestDecision(skip=True, reason=f"email attachment mime {mime}")


def enrich_email_from_text(decision: IngestDecision, text: str) -> None:
    """
    Mutate a "email"-typed decision in place with fields parsed from the
    summary header. Looks up the counterparty in Firestore `contacts` to
    derive contact_id + municipality.

    Called by the orchestrator AFTER the file body is downloaded. No-op for
    decisions whose document_type isn't "email" (so attachments under the
    same folder are unaffected).
    """
    if decision.document_type != "email":
        return

    header = parse_email_summary_header(text)

    decision.email_message_id = header.get("message_id") or None
    decision.email_thread_id = header.get("thread_id") or None
    direction = header.get("direction") or ""
    if direction in {"inbound", "outbound", "internal"}:
        decision.email_direction = direction  # type: ignore[assignment]

    # event_time: when the email was actually sent/received. Critical
    # because every email summary file's drive_modified_time is the
    # scrape time (today), not the message time — backfilled corpora
    # would all collapse to one date without this.
    decision.event_time = parse_email_date_to_datetime(header.get("date", ""))

    counterparty = (header.get("counterparty") or "").strip()
    if not counterparty:
        # Internal-direction messages have no external counterparty by
        # definition; nothing to look up. Leave contact_id/municipality empty.
        return

    contact = get_contact_by_email(counterparty)
    if contact is None:
        logger.info(
            "email_inbox: no contact match for %s (msg_id=%s)",
            counterparty,
            decision.email_message_id,
        )
        return

    contact_id = contact.get("ghl_contact_id") or contact.get("id")
    if contact_id:
        decision.contact_id = [contact_id]

    municipality_slug = contact.get("municipality_slug")
    if municipality_slug:
        decision.municipality = [municipality_slug]


# ---------------------------------------------------------------------------
# Source registry
# ---------------------------------------------------------------------------

SOURCE_CONFIGS.update(
    plaud=SourceConfig("Plaud Files", "plaud", resolve_plaud),
    leads=SourceConfig("Leads", "leads", resolve_leads),
    industry_context=SourceConfig(
        "Industry Context", "industry_context", resolve_industry_context
    ),
    email_inbox=SourceConfig(
        "email-inbox",
        "email_inbox",
        resolve_email_inbox,
        enrich_from_text=enrich_email_from_text,
    ),
    iflytek=SourceConfig("Iflytek Files", "iflytek", resolve_iflytek),
)
