"""Browse the Firestore `contacts` collection.

Use this to find a real contact id you can hand to the agent CLIs:

    cd backend
    python scripts/list_contacts.py
    python scripts/list_contacts.py --limit 50
    python scripts/list_contacts.py --show 0I21saCPXJVEbdncGXEW

Then run an agent against that contact:

    python scripts/run_presentation_agent.py PA-STEP4 \
        --contact-id 0I21saCPXJVEbdncGXEW \
        --override meeting_date='June 12, 2026' \
        --override audience='City Manager, PW Director' \
        --override problem_area_focus='Hogtown Creek stormwater retrofit' \
        --no-web-search --no-drive

    python scripts/run_research_agent.py PW-3 \
        --contact-id 0I21saCPXJVEbdncGXEW \
        --no-web-search --no-drive
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_BACKEND = _HERE.parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from dotenv import load_dotenv
load_dotenv(_BACKEND.parent / ".env")

from services.firestore.client import get_contact, list_contacts
from services.firestore.contact_context import build_context_from_contact


def main() -> None:
    ap = argparse.ArgumentParser(description="Browse the Firestore contacts collection.")
    ap.add_argument("--limit", type=int, default=25, help="Max contacts to list (default 25)")
    ap.add_argument(
        "--show", default=None,
        help="Print the full document AND the flattened agent context for one contact id",
    )
    args = ap.parse_args()

    if args.show:
        raw = get_contact(args.show)
        if raw is None:
            print(f"ERROR: contact {args.show} not found.")
            sys.exit(2)
        print("=" * 70)
        print(f"RAW DOCUMENT — {args.show}")
        print("=" * 70)
        print(json.dumps(raw, indent=2, default=str))

        print("\n" + "=" * 70)
        print("FLATTENED AGENT CONTEXT")
        print("=" * 70)
        ctx = build_context_from_contact(raw)
        print(json.dumps(ctx, indent=2, default=str))
        return

    contacts = list_contacts(limit=args.limit)
    print(f"Showing {len(contacts)} contacts (limit={args.limit}):\n")
    fmt = "  {idx:>3}. {id:24}  {name:30}  {org:30}  {tags}"
    print(fmt.format(idx="#", id="id", name="contactName", org="company / city", tags="tags"))
    print(fmt.format(idx="---", id="-" * 24, name="-" * 30, org="-" * 30, tags="-" * 10))
    for i, c in enumerate(contacts, 1):
        name = c.get("contactName") or c.get("email") or "(no name)"
        org = c.get("companyName") or c.get("city") or "(none)"
        tags = ", ".join(c.get("tags") or []) or "—"
        print(fmt.format(
            idx=i,
            id=c.get("id", "")[:24],
            name=str(name)[:30],
            org=str(org)[:30],
            tags=tags[:30],
        ))


if __name__ == "__main__":
    main()
