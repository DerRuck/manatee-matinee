"""
Bulk ingest a Drive folder into Firestore.

Replaces the older scripts/ingest_demo_corpus.py. The per-file ingest
logic now lives in backend/ingestion/orchestrator.py as
ingest_one_drive_file(); this script just walks a folder and calls it
on every leaf.

Run from backend/:
    python -m scripts.bulk_ingest --folder-id <ROOT> --source <source_key>

`--source` values:
    plaud | leads | industry_context | email_inbox | iflytek | all

When `all` is selected, the root is expected to contain each source's
canonical subfolder name (matching SourceConfig.folder_name) and the
walker dispatches per subfolder.

For a single source, the walker passes `--source` as the source_hint so
the orchestrator can skip parent-chain probing on each file.
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

from ingestion import ingest_one_drive_file, IngestStats, SOURCE_CONFIGS  # noqa: E402
from services.drive.client import FOLDER_MIME, list_folder_files, walk_folder  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("bulk_ingest")


def _ingest_source_folder(
    folder_id: str, source_key: str, stats: IngestStats
) -> None:
    """Walk folder_id and ingest every leaf with source_hint=source_key."""
    logger.info("walking source=%s folder_id=%s", source_key, folder_id)
    for file_meta, _sub_segments in walk_folder(folder_id):
        ingest_one_drive_file(
            file_meta["id"],
            source_hint=source_key,
            stats=stats,
        )


def _find_subfolder(parent_id: str, name: str) -> str | None:
    """Find a direct child folder of parent_id whose name matches. Returns ID or None."""
    for child in list_folder_files(parent_id):
        if child.get("mimeType") == FOLDER_MIME and child.get("name") == name:
            return child["id"]
    return None


def _ingest_all_sources(root_folder_id: str, stats: IngestStats) -> None:
    """For --source all: find each registered source's subfolder under root and ingest."""
    for source_key, cfg in SOURCE_CONFIGS.items():
        sub_id = _find_subfolder(root_folder_id, cfg.folder_name)
        if sub_id is None:
            logger.warning(
                "skipping source=%s: no subfolder named '%s' under root",
                source_key,
                cfg.folder_name,
            )
            continue
        _ingest_source_folder(sub_id, source_key, stats)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--folder-id",
        required=True,
        help="Drive folder ID to walk. For --source all, this should be the "
             "parent containing each registered source's subfolder.",
    )
    parser.add_argument(
        "--source",
        required=True,
        choices=list(SOURCE_CONFIGS.keys()) + ["all"],
        help="Source key. 'all' walks every registered source's subfolder under root.",
    )
    parser.add_argument(
        "--folder-is-root",
        action="store_true",
        help="Treat --folder-id as the PARENT root and locate this source's "
             "named subfolder (SourceConfig.folder_name) under it, instead of "
             "walking --folder-id directly. Use this so a stable root ID keeps "
             "working even if the source subfolder is deleted + recreated with "
             "a new ID. Ignored for --source all (which always treats folder-id "
             "as the root).",
    )
    args = parser.parse_args()

    stats = IngestStats()
    started = time.monotonic()

    if args.source == "all":
        _ingest_all_sources(args.folder_id, stats)
    else:
        target_folder_id = args.folder_id
        if args.folder_is_root:
            cfg = SOURCE_CONFIGS[args.source]
            sub_id = _find_subfolder(args.folder_id, cfg.folder_name)
            if sub_id is None:
                logger.error(
                    "no subfolder named '%s' under root %s -- nothing to ingest",
                    cfg.folder_name,
                    args.folder_id,
                )
                sys.exit(1)
            logger.info(
                "resolved source=%s subfolder '%s' -> %s (under root %s)",
                args.source,
                cfg.folder_name,
                sub_id,
                args.folder_id,
            )
            target_folder_id = sub_id
        _ingest_source_folder(target_folder_id, args.source, stats)

    elapsed = time.monotonic() - started
    logger.info(
        "ingest summary - attempted=%d ingested=%d skipped=%d failed=%d chunks=%d elapsed=%.1fs",
        stats.documents_attempted,
        stats.documents_ingested,
        stats.documents_skipped,
        stats.documents_failed,
        stats.chunks_written,
        elapsed,
    )
    if stats.skip_reasons:
        logger.info("skip reasons:")
        for reason, count in sorted(stats.skip_reasons.items(), key=lambda kv: -kv[1]):
            logger.info("  %4d  %s", count, reason)


if __name__ == "__main__":
    main()
