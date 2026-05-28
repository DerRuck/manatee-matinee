"""
sync_ghl_contacts.py — backfill GHL contacts + municipalities into Firestore.

Thin CLI wrapper around services.ghl.sync.run_sync. Same logic the
POST /sync/ghl/contacts endpoint runs.

Run from backend/ dir:
    python -m scripts.sync_ghl_contacts                    # full backfill
    python -m scripts.sync_ghl_contacts --contact-id <id>  # single contact
    python -m scripts.sync_ghl_contacts --since 2026-05-01 # incremental
    python -m scripts.sync_ghl_contacts --dry-run          # no writes
    python -m scripts.sync_ghl_contacts --verbose

Requires .env with GHL_PIT, GHL_LOCATION_ID, GCP_PROJECT_ID set.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Load .env from the repo root BEFORE importing settings — pydantic-settings
# resolves env_file=".env" relative to cwd, which misses the repo-root .env
# when this script is launched from backend/. Mirrors the load_dotenv call
# in app/main.py.
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from core.logging import configure_logging  # noqa: E402
from core.settings import get_settings  # noqa: E402
from services.ghl.sync import run_sync  # noqa: E402


logger = logging.getLogger("sync_ghl_contacts")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--contact-id",
        help="Sync a single contact by GHL ID. Skips municipality count refresh.",
    )
    parser.add_argument(
        "--since",
        help="Only sync contacts updated after this ISO timestamp (e.g. 2026-05-01).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log intended writes without performing them.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    configure_logging()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    settings = get_settings()
    missing = [
        name
        for name, val in {
            "GHL_PIT": settings.ghl_pit,
            "GHL_LOCATION_ID": settings.ghl_location_id,
            "GCP_PROJECT_ID": settings.gcp_project_id,
        }.items()
        if not val
    ]
    if missing:
        logger.error("missing required env vars: %s", ", ".join(missing))
        return 1

    sync_source = "manual" if args.contact_id else "backfill"
    result = run_sync(
        contact_id=args.contact_id,
        since=args.since,
        dry_run=args.dry_run,
        sync_source=sync_source,
    )

    logger.info(
        "sync done. created=%d updated=%d no_municipality=%d errors=%d",
        result["created"],
        result["updated"],
        result["no_municipality"],
        result["errors"],
    )
    return 0 if result["errors"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
