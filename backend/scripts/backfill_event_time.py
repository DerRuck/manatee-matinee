"""
Backfill the `event_time` field onto existing documents + chunks.

Why this exists:
    `event_time` represents when the underlying event actually happened
    (email Date, meeting date, etc.) — distinct from `drive_modified_time`
    (file mtime; used for the idempotency check) and `ingested_at`
    (pipeline metadata). Rows ingested before the field existed have no
    event_time, and for emails the drive_modified_time is the scrape
    time (today), not the message time. This script reads each document,
    resolves event_time, and writes it to the documents row + all its chunks.

Resolution rules:
    - Email rows (data_source == "email_inbox", document_type == "email"):
      download the summary .txt from Drive, parse the RFC 2822 Date
      header, use that.
    - All other rows: fall back to `drive_modified_time`, which is
      accurate for sources without a wrapper layer (plaud, leads,
      industry_context, iflytek).
    - If the email header can't be parsed AND drive_modified_time is
      missing: log + skip.

Idempotent: rows that already have event_time are skipped unless --force
is passed. Safe to re-run.

Run from backend/:
    python -m scripts.backfill_event_time              # do the work
    python -m scripts.backfill_event_time --dry-run    # report only
    python -m scripts.backfill_event_time --force      # overwrite existing
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# Allow running as a script from inside backend/ without -m gymnastics.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from ingestion.email_header import (  # noqa: E402
    parse_email_date_to_datetime,
    parse_email_summary_header,
)
from services.drive.client import download_file_as_text  # noqa: E402
from services.firestore.client import (  # noqa: E402
    iter_documents,
    update_chunks_for_document,
    update_document_fields,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("backfill_event_time")


def _resolve_event_time(doc: dict) -> tuple[Optional[datetime], str]:
    """
    Decide what event_time to write for one document row.

    Returns (event_time, source) where `source` is one of:
        "email_header"        — parsed from the summary .txt Date: line
        "drive_modified_time" — fell back to the Drive file mtime
        "missing"             — nothing usable (will be skipped)
    """
    is_email = (
        doc.get("data_source") == "email_inbox"
        and doc.get("document_type") == "email"
    )
    if is_email:
        file_id = doc.get("drive_file_id") or doc.get("document_id")
        mime = doc.get("drive_mime_type") or "text/plain"
        name = doc.get("drive_file_name") or ""
        try:
            text = download_file_as_text(file_id, mime, file_name=name)
        except Exception:
            logger.exception(
                "download failed for email row id=%s name=%s", file_id, name
            )
            text = ""
        if text:
            header = parse_email_summary_header(text)
            parsed = parse_email_date_to_datetime(header.get("date", ""))
            if parsed is not None:
                return parsed, "email_header"
        # Email row but header missing/unparseable — fall through to
        # drive_modified_time so we still get *something* recency-aware.
        logger.info(
            "email row missing parseable Date header, falling back to "
            "drive_modified_time: id=%s name=%s",
            doc.get("document_id"),
            name,
        )

    fallback = doc.get("drive_modified_time")
    if isinstance(fallback, datetime):
        return fallback, "drive_modified_time"
    return None, "missing"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill event_time on documents + chunks."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Walk + report only; perform no writes.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite event_time even on rows that already have one.",
    )
    args = parser.parse_args()

    started = time.monotonic()
    seen = 0
    updated_docs = 0
    updated_chunks = 0
    skipped_already = 0
    skipped_missing = 0
    by_source: dict[str, int] = {}

    for doc in iter_documents():
        seen += 1
        document_id = doc.get("document_id")
        if not document_id:
            continue

        if doc.get("event_time") and not args.force:
            skipped_already += 1
            continue

        event_time, source = _resolve_event_time(doc)
        if event_time is None:
            skipped_missing += 1
            logger.warning(
                "no event_time resolvable id=%s name=%s data_source=%s",
                document_id,
                doc.get("drive_file_name"),
                doc.get("data_source"),
            )
            continue

        by_source[source] = by_source.get(source, 0) + 1

        if args.dry_run:
            logger.info(
                "dry-run: %s event_time=%s source=%s",
                document_id,
                event_time.isoformat(),
                source,
            )
            continue

        update_document_fields(document_id, {"event_time": event_time})
        n = update_chunks_for_document(document_id, {"event_time": event_time})
        updated_docs += 1
        updated_chunks += n

    elapsed = time.monotonic() - started
    logger.info(
        "backfill summary - seen=%d updated_docs=%d updated_chunks=%d "
        "skipped_already_set=%d skipped_missing=%d elapsed=%.1fs",
        seen,
        updated_docs,
        updated_chunks,
        skipped_already,
        skipped_missing,
        elapsed,
    )
    if by_source:
        logger.info("by source:")
        for src, count in sorted(by_source.items(), key=lambda kv: -kv[1]):
            logger.info("  %4d  %s", count, src)


if __name__ == "__main__":
    main()
