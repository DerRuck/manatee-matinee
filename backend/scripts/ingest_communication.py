"""
Ingest one communication record so the scoring agent can see it.

Two main use cases:

  1. Plaud voice transcripts. The Plaud Pin captures meetings and events;
     the binder protocol says exports land in Drive within 24 hours. This
     CLI takes one transcript file and writes it to the communications
     collection tied to a specific contact.

  2. Manual notes. When the team has a phone-call recap or an off-channel
     email that didn't route through GHL, drop it in via --body so the
     scoring agent picks up the signal.

Run from backend/:

    # Plaud transcript from a local file
    python scripts/ingest_communication.py \\
        --contact-id 0I21saCPXJVEbdncGXEW \\
        --channel voice_transcript \\
        --direction inbound \\
        --file /path/to/2026_05_14_FSBPA_Day1_Emily.txt \\
        --timestamp 2026-05-14T15:30:00Z \\
        --subject "FSBPA Day 1 booth conversations" \\
        --author Emily

    # Quick manual note from the command line
    python scripts/ingest_communication.py \\
        --contact-id 0I21saCPXJVEbdncGXEW \\
        --channel note \\
        --direction internal \\
        --body "Champion confirmed June 12 site walk; bringing 2 commissioners." \\
        --author Logan

    # Dry run — see what would be written, hit no Firestore
    python scripts/ingest_communication.py --contact-id X --body "test" --dry-run

Idempotency: the comm_id is derived from --source-ref when given, else from
a hash of the body. Re-running with the same inputs overwrites the same row.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve()
_BACKEND = _HERE.parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from dotenv import load_dotenv
load_dotenv(_BACKEND.parent / ".env")


def _parse_ts(value: str | None) -> datetime:
    if not value:
        return datetime.now(tz=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemExit(f"--timestamp {value!r} is not ISO 8601: {exc}") from exc


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Write one communication into Firestore (email, transcript, note).",
    )
    ap.add_argument("--contact-id", required=True,
                    help="GHL contact id this communication belongs to")
    ap.add_argument(
        "--channel", required=True,
        choices=["email", "sms", "voice_transcript", "note", "call"],
    )
    ap.add_argument(
        "--direction", default="inbound",
        choices=["inbound", "outbound", "internal"],
        help="inbound = from contact, outbound = from us, internal = team note (default inbound)",
    )
    ap.add_argument("--body", help="Inline body text. Mutually exclusive with --file.")
    ap.add_argument("--file", help="Read body text from this file (Plaud transcript, etc.).")
    ap.add_argument("--subject", help="Optional subject line.")
    ap.add_argument("--author", help="Sender — email address for outbound, name for transcripts.")
    ap.add_argument(
        "--timestamp",
        help="When the comm happened (ISO 8601). Defaults to now.",
    )
    ap.add_argument(
        "--source",
        choices=["ghl", "drive", "manual"],
        default="manual",
        help="Where the comm came from (default: manual).",
    )
    ap.add_argument(
        "--source-ref",
        help="External id — GHL messageId, Drive fileId, etc. Drives idempotent re-ingestion.",
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the resolved Communication record but don't write.")
    args = ap.parse_args()

    if args.body and args.file:
        raise SystemExit("Pass --body OR --file, not both.")
    if not args.body and not args.file and not args.subject:
        raise SystemExit("Must provide --body, --file, or at least --subject.")

    body = ""
    if args.file:
        path = Path(args.file)
        if not path.exists():
            raise SystemExit(f"--file {path} does not exist")
        body = path.read_text(encoding="utf-8", errors="replace")
    elif args.body:
        body = args.body

    from services.firestore.communications import (
        Communication, make_comm_id, put_communication,
    )

    comm_id = make_comm_id(args.source, args.source_ref, body)
    comm = Communication(
        comm_id=comm_id,
        contact_id=args.contact_id,
        channel=args.channel,
        direction=args.direction,
        timestamp=_parse_ts(args.timestamp),
        subject=args.subject,
        body=body,
        source=args.source,
        source_ref=args.source_ref,
        author=args.author,
    )

    print(f"\nResolved communication:")
    print(f"  comm_id:    {comm.comm_id}")
    print(f"  contact:    {comm.contact_id}")
    print(f"  channel:    {comm.channel}  ({comm.direction})")
    print(f"  timestamp:  {comm.timestamp.isoformat()}")
    print(f"  subject:    {comm.subject or '(none)'}")
    print(f"  author:     {comm.author or '(none)'}")
    print(f"  body chars: {len(comm.body):,}")

    if args.dry_run:
        print("\n--dry-run set; nothing written.")
        return

    put_communication(comm)
    print(f"\nWrote communications/{comm.comm_id}")


if __name__ == "__main__":
    main()
