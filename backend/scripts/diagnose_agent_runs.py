"""Diagnose why the scoring agent sees zero agent_runs for a contact
that other queries report as having history.

Runs three probes against the `agent_runs` collection for one contact_id:

  1. The SAME query _fetch_agent_runs() uses (where + order_by) — but with
     the exception unmasked so an index miss or schema error surfaces.
  2. A plain `where("contact_id", "==", ...)` with no order_by — confirms
     whether the rows exist at all.
  3. A collection-wide scan grouped by contact_id (matches the ranker) —
     confirms which contact_ids the rows are actually keyed under.

Usage from backend/:
    python scripts/diagnose_agent_runs.py 9Ch8L4v30pjA6BbUyQks
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve()
_BACKEND = _HERE.parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from dotenv import load_dotenv
load_dotenv(_BACKEND.parent / ".env")

from core.settings import get_settings
from services.firestore.client import _get_client


def probe_ordered_query(contact_id: str, limit: int = 50) -> None:
    """Probe 1: the exact query _fetch_agent_runs uses, no exception swallowed."""
    print("=" * 70)
    print("PROBE 1: where(contact_id) + order_by(finished_at DESC) — same as scorer")
    print("=" * 70)
    client = _get_client()
    settings = get_settings()
    try:
        query = (
            client.collection(settings.firestore_agent_runs_collection)
            .where("contact_id", "==", contact_id)
            .order_by("finished_at", direction="DESCENDING")
            .limit(limit)
        )
        rows = [snap.to_dict() for snap in query.stream()]
        print(f"  returned {len(rows)} rows")
        for r in rows:
            print(f"    - run_id={r.get('run_id')}  "
                  f"agent={r.get('agent') or r.get('research_type_id') or r.get('score_type_id')}  "
                  f"finished_at={r.get('finished_at')!r}")
    except Exception:
        print("  RAISED:")
        traceback.print_exc()
    print()


def probe_plain_where(contact_id: str, limit: int = 50) -> None:
    """Probe 2: same filter, no order_by — does the data even exist?"""
    print("=" * 70)
    print("PROBE 2: where(contact_id) only — no order_by")
    print("=" * 70)
    client = _get_client()
    settings = get_settings()
    try:
        query = (
            client.collection(settings.firestore_agent_runs_collection)
            .where("contact_id", "==", contact_id)
            .limit(limit)
        )
        rows = [snap.to_dict() for snap in query.stream()]
        print(f"  returned {len(rows)} rows")
        for r in rows:
            print(f"    - run_id={r.get('run_id')}")
            print(f"      contact_id={r.get('contact_id')!r}")
            print(f"      agent={r.get('agent') or r.get('research_type_id') or r.get('score_type_id')}")
            print(f"      finished_at={r.get('finished_at')!r}  ({type(r.get('finished_at')).__name__})")
            print(f"      generated_at={r.get('generated_at')!r}")
            print(f"      status={r.get('status')!r}")
            print(f"      keys={sorted(r.keys())}")
    except Exception:
        print("  RAISED:")
        traceback.print_exc()
    print()


def probe_collection_scan(contact_id: str, limit: int = 2000) -> None:
    """Probe 3: scan the whole collection — find rows by string match on contact_id."""
    print("=" * 70)
    print(f"PROBE 3: collection scan (up to {limit}) — match contact_id in-Python")
    print("=" * 70)
    client = _get_client()
    settings = get_settings()
    try:
        query = client.collection(settings.firestore_agent_runs_collection).limit(limit)
        matches = []
        total = 0
        for snap in query.stream():
            total += 1
            doc = snap.to_dict() or {}
            if doc.get("contact_id") == contact_id:
                matches.append(doc)
        print(f"  scanned {total} rows, {len(matches)} match contact_id == {contact_id!r}")
        for r in matches:
            print(f"    - run_id={r.get('run_id')}  "
                  f"finished_at={r.get('finished_at')!r}  "
                  f"agent={r.get('agent') or r.get('research_type_id') or r.get('score_type_id')}")
    except Exception:
        print("  RAISED:")
        traceback.print_exc()
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("contact_id")
    args = ap.parse_args()

    print(f"\nDiagnosing agent_runs for contact_id = {args.contact_id}\n")
    probe_ordered_query(args.contact_id)
    probe_plain_where(args.contact_id)
    probe_collection_scan(args.contact_id)


if __name__ == "__main__":
    main()
