"""
Purge the email corpus from Firestore (documents + chunks).

Scoped, NOT a collection wipe. Deletes only rows whose `data_source`
marks them as email (default "email_inbox"), leaving plaud / leads /
iflytek / research-brief rows -- and prod data -- untouched. The
`documents` and `chunks` collections are shared across every source AND
across dev/prod (same project, un-prefixed collection names), so a
blanket collection delete is never safe.

Both `documents` and `chunks` carry `data_source`, so each collection is
purged with one direct filtered query -- no per-document subqueries.

Use this to reset the email corpus after the 5/29 duplicate-folder
scrape. Pair it with deleting the Drive `email-inbox` subtree, then
re-scrape with the fixed scraper and re-ingest.

DRY-RUN BY DEFAULT. Nothing is deleted unless you pass --apply.

Run from backend/:
    python -m scripts.purge_email_corpus              # dry-run, report only
    python -m scripts.purge_email_corpus --apply      # actually delete
    python -m scripts.purge_email_corpus --data-source email_inbox --apply
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# Allow running as a script from inside backend/ without -m gymnastics.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from google.api_core.retry import Retry  # noqa: E402
from google.cloud.firestore_v1.base_query import FieldFilter  # noqa: E402

from core.settings import get_settings  # noqa: E402
from services.firestore.client import _get_client  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("purge_email_corpus")

# Generous timeout + explicit retry so a cold/transient DEADLINE_EXCEEDED
# on the first stream doesn't hit the firestore client's buggy retry path
# (_UnaryStreamMultiCallable has no _retry on some versions).
STREAM_TIMEOUT = 600.0
STREAM_RETRY = Retry(deadline=STREAM_TIMEOUT)
DELETE_BATCH = 400  # Firestore batch hard limit is 500.


def _purge_collection(client, collection: str, value: str, apply: bool, want_sample: bool):
    """Stream `collection` where data_source == value. Count (dry-run) or
    batch-delete (apply). .select() keeps the payload tiny so chunk
    embeddings are never downloaded just to count or delete.
    """
    fields = ["data_source", "document_type", "drive_file_name"] if want_sample else ["data_source"]
    q = (
        client.collection(collection)
        .where(filter=FieldFilter("data_source", "==", value))
        .select(fields)
    )

    seen = 0
    sample: list[tuple[str, str, str]] = []
    batch = client.batch()
    pending = 0

    for snap in q.stream(timeout=STREAM_TIMEOUT, retry=STREAM_RETRY):
        seen += 1
        if want_sample and len(sample) < 10:
            d = snap.to_dict() or {}
            sample.append((snap.id, str(d.get("document_type", "?")), str(d.get("drive_file_name", "?"))))
        if apply:
            batch.delete(snap.reference)
            pending += 1
            if pending >= DELETE_BATCH:
                batch.commit()
                batch = client.batch()
                pending = 0
        if seen % 500 == 0:
            logger.info("  %s: %d rows so far...", collection, seen)

    if apply and pending:
        batch.commit()

    return seen, sample


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--data-source",
        default="email_inbox",
        help="data_source value to purge (default: email_inbox).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete. Without this flag the script only reports (dry-run).",
    )
    args = parser.parse_args()

    settings = get_settings()
    client = _get_client()
    docs_col = settings.firestore_documents_collection
    chunks_col = settings.firestore_chunks_collection

    mode = "APPLY (deleting)" if args.apply else "DRY-RUN (no deletes)"
    logger.info(
        "purge email corpus | mode=%s | project=%s | docs=%s chunks=%s | data_source=%r",
        mode, settings.gcp_project_id, docs_col, chunks_col, args.data_source,
    )

    started = time.monotonic()

    docs_seen, sample = _purge_collection(client, docs_col, args.data_source, args.apply, want_sample=True)
    if sample:
        logger.info("sample of matched documents (up to 10):")
        for doc_id, dtype, name in sample:
            logger.info("  %s  type=%s  %s", doc_id, dtype, name)

    chunks_seen, _ = _purge_collection(client, chunks_col, args.data_source, args.apply, want_sample=False)

    elapsed = time.monotonic() - started
    verb = "deleted" if args.apply else "would delete"
    logger.info(
        "summary | documents %s=%d | chunks %s=%d | elapsed=%.1fs",
        verb, docs_seen, verb, chunks_seen, elapsed,
    )
    if not args.apply and (docs_seen or chunks_seen):
        logger.info("DRY-RUN only. Re-run with --apply to delete the rows above.")
    if not docs_seen and not chunks_seen:
        logger.info("nothing matched -- corpus already clean for this data_source.")


if __name__ == "__main__":
    main()
