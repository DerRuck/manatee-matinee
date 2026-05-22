"""Rank Firestore contacts by how much signal the scoring agent will see.

The scoring agent (`run_scoring_agent.py`) only produces a rich, demo-worthy
result when the underlying contact has real data attached — tags, custom
fields, a municipality, and prior `agent_runs` history. A bare-stub contact
(only an email) returns a thin 5/100 cold score, which is fine for the daily
sweep but useless as a showcase example.

This script scans the `contacts` collection plus the `agent_runs` collection,
joins them in memory, scores every contact for "demo-readiness", and prints
the top N — so you can pick the one that will exercise the scoring prompt
end-to-end.

Run from backend/:

    python scripts/rank_contacts_for_scoring.py
    python scripts/rank_contacts_for_scoring.py --top 25 --contact-limit 500
    python scripts/rank_contacts_for_scoring.py --top 1 --run    # score the winner

Scoring weights match what build_scoring_context() actually passes to the
prompt — see services/scoring_agent/context_builder.py.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve()
_BACKEND = _HERE.parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from dotenv import load_dotenv
load_dotenv(_BACKEND.parent / ".env")

from core.settings import get_settings
from services.firestore.client import _get_client, list_contacts


HOT_TAGS = {"boil", "simmer"}


def count_agent_runs_per_contact(limit: int = 2000) -> Counter:
    """Stream recent agent_runs and tally how many exist per contact_id.

    One scan, grouped in memory — far cheaper than N per-contact queries.
    """
    client = _get_client()
    settings = get_settings()
    counts: Counter = Counter()
    query = (
        client.collection(settings.firestore_agent_runs_collection)
        .order_by("finished_at", direction="DESCENDING")
        .limit(limit)
    )
    for snap in query.stream():
        doc = snap.to_dict() or {}
        cid = doc.get("contact_id")
        if cid:
            counts[cid] += 1
    return counts


def score_contact(contact: dict[str, Any], run_count: int) -> tuple[int, list[str]]:
    """Return (richness_score, reasons_list) for one contact.

    Higher = better demo example. Each reason explains a points contribution
    so the printed ranking is self-documenting.
    """
    score = 0
    reasons: list[str] = []

    first = (contact.get("firstNameRaw") or contact.get("firstName") or "").strip()
    last = (contact.get("lastNameRaw") or contact.get("lastName") or "").strip()
    if first or last:
        score += 5
        reasons.append("+5 real name")

    municipality = contact.get("city") or contact.get("companyName")
    if municipality:
        score += 5
        reasons.append(f"+5 municipality ({municipality})")

    tags = [t for t in (contact.get("tags") or []) if t]
    if tags:
        score += min(len(tags), 4) * 3
        hot = [t for t in tags if t.lower() in HOT_TAGS]
        if hot:
            score += 5 * len(hot)
            reasons.append(f"+{3 * min(len(tags), 4) + 5 * len(hot)} tags ({', '.join(tags)})")
        else:
            reasons.append(f"+{3 * min(len(tags), 4)} tags ({', '.join(tags)})")

    custom_fields = contact.get("customFields") or []
    populated = [
        c for c in custom_fields
        if isinstance(c, dict) and c.get("value") not in (None, "", [], {})
    ]
    if populated:
        bump = min(len(populated), 6) * 2
        score += bump
        reasons.append(f"+{bump} custom fields ({len(populated)} populated)")

    if contact.get("ghl_pipeline_stage"):
        score += 3
        reasons.append("+3 pipeline stage set")

    if run_count > 0:
        bump = min(run_count, 6) * 4
        score += bump
        reasons.append(f"+{bump} prior agent_runs ({run_count})")

    if not reasons:
        reasons.append("bare stub — would produce a thin cold score")

    return score, reasons


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Rank contacts by how demo-worthy their pipeline score will be."
    )
    ap.add_argument("--contact-limit", type=int, default=500,
                    help="Max contacts to scan (default 500)")
    ap.add_argument("--runs-limit", type=int, default=2000,
                    help="Max recent agent_runs to scan for the join (default 2000)")
    ap.add_argument("--top", type=int, default=10,
                    help="How many of the top contacts to print (default 10)")
    ap.add_argument("--run", action="store_true",
                    help="Pipe the #1 contact straight into run_scoring_agent.py")
    args = ap.parse_args()

    print(f"Scanning up to {args.contact_limit} contacts and {args.runs_limit} agent_runs...")
    contacts = list_contacts(limit=args.contact_limit)
    run_counts = count_agent_runs_per_contact(limit=args.runs_limit)

    ranked: list[tuple[int, dict[str, Any], list[str]]] = []
    for c in contacts:
        cid = c.get("id") or ""
        s, reasons = score_contact(c, run_counts.get(cid, 0))
        ranked.append((s, c, reasons))
    ranked.sort(key=lambda row: row[0], reverse=True)

    print(f"\nTop {min(args.top, len(ranked))} of {len(ranked)} contacts:\n")
    fmt = "  {rank:>3}  {score:>5}  {id:24}  {name:30}  {muni:25}  {tags}"
    print(fmt.format(rank="#", score="score", id="contact_id",
                     name="name", muni="city / company", tags="tags"))
    print(fmt.format(rank="---", score="-----", id="-" * 24,
                     name="-" * 30, muni="-" * 25, tags="-" * 20))
    for rank, (score, c, reasons) in enumerate(ranked[: args.top], 1):
        first = (c.get("firstNameRaw") or c.get("firstName") or "").strip()
        last = (c.get("lastNameRaw") or c.get("lastName") or "").strip()
        name = (f"{first} {last}".strip()
                or c.get("contactName")
                or c.get("email")
                or "(no name)")
        muni = c.get("city") or c.get("companyName") or "—"
        tags = ", ".join(c.get("tags") or []) or "—"
        print(fmt.format(
            rank=rank,
            score=score,
            id=(c.get("id") or "")[:24],
            name=str(name)[:30],
            muni=str(muni)[:25],
            tags=tags[:30],
        ))
        for reason in reasons:
            print(f"          {reason}")
        print()

    if not ranked:
        print("No contacts found.")
        return

    winner = ranked[0][1]
    winner_id = winner.get("id")
    print("To score the top contact:")
    print(f"    python scripts/run_scoring_agent.py {winner_id}\n")

    if args.run and winner_id:
        cmd = [sys.executable, str(_BACKEND / "scripts" / "run_scoring_agent.py"), winner_id]
        print(f"Running: {' '.join(cmd)}\n")
        subprocess.run(cmd, check=False)


if __name__ == "__main__":
    main()
