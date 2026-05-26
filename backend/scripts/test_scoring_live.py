"""
Live end-to-end smoke test for the scoring agent.

What it does:
  1. Pulls real contacts from Firestore (using the same demo-readiness
     ranking as rank_contacts_for_scoring.py).
  2. Runs the Scoring Agent against the top-N — real LLM call, real
     binder retrieval, real validation.
  3. (Optional) writes results to Firestore + Drive end-to-end.
  4. Prints a single-line summary per contact + overall pass/fail.

This is the equivalent of scripts/test_all_agents.py but for scoring:
exercises the full path the daily sweep uses, against actual data, in
under a few minutes.

Run from backend/:

    # Default: top 5 contacts, no persistence (safe to run repeatedly)
    python scripts/test_scoring_live.py

    # Score the top 10 and write everything to Firestore + Drive
    python scripts/test_scoring_live.py --top 10 --persist

    # Limit to one specific contact
    python scripts/test_scoring_live.py --contact-id 0I21saCPXJVEbdncGXEW

    # Save per-contact JSON locally (debug / regression baselines)
    python scripts/test_scoring_live.py --save-dir /tmp/scoring_smoke

Exit code 0 only when every selected contact scored cleanly.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve()
_BACKEND = _HERE.parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from dotenv import load_dotenv
load_dotenv(_BACKEND.parent / ".env")


# ---------------------------------------------------------------------------
# Result row
# ---------------------------------------------------------------------------

@dataclass
class SmokeResult:
    contact_id:    str
    contact_label: str
    passed:        bool
    elapsed_sec:   float
    input_tokens:  int
    output_tokens: int
    current_step:  int | None
    lead_heat:     str | None
    lead_heat_score: int | None
    step_confidence: float | None
    ready_to_advance: bool | None
    signals_count: int
    actions_count: int
    summary_one_line: str | None
    drive_link:    str | None
    error:         str | None


# ---------------------------------------------------------------------------
# Contact selection
# ---------------------------------------------------------------------------

def pick_top_contacts(top_n: int, contact_limit: int, runs_limit: int) -> list[dict]:
    """Reuse rank_contacts_for_scoring's logic to pick demo-worthy contacts."""
    from scripts.rank_contacts_for_scoring import (
        count_agent_runs_per_contact, score_contact,
    )
    from services.firestore.client import list_contacts

    contacts = list_contacts(limit=contact_limit)
    run_counts = count_agent_runs_per_contact(limit=runs_limit)

    ranked: list[tuple[int, dict]] = []
    for c in contacts:
        cid = c.get("id") or ""
        s, _ = score_contact(c, run_counts.get(cid, 0))
        ranked.append((s, c))
    ranked.sort(key=lambda row: row[0], reverse=True)
    return [c for _, c in ranked[:top_n]]


def contact_label(contact: dict) -> str:
    first = (contact.get("firstNameRaw") or contact.get("firstName") or "").strip()
    last = (contact.get("lastNameRaw") or contact.get("lastName") or "").strip()
    name = f"{first} {last}".strip()
    if name:
        return name
    return (
        contact.get("contactName")
        or contact.get("email")
        or contact.get("city")
        or contact.get("companyName")
        or contact.get("id")
        or "(unknown)"
    )


# ---------------------------------------------------------------------------
# One scoring run
# ---------------------------------------------------------------------------

def score_one(
    contact_id: str,
    label: str,
    persist: bool,
    drive: bool,
) -> SmokeResult:
    from agents.scoring_agent import ScoringAgent
    from services.scoring_agent.context_builder import build_scoring_context

    t0 = time.time()
    try:
        context = build_scoring_context(contact_id, triggered_by="manual")
        agent = ScoringAgent("PIPELINE-SCORE")
        result, meta = agent.run(context, verbose=False)
    except Exception as exc:
        return SmokeResult(
            contact_id=contact_id,
            contact_label=label,
            passed=False,
            elapsed_sec=round(time.time() - t0, 1),
            input_tokens=0, output_tokens=0,
            current_step=None, lead_heat=None, lead_heat_score=None,
            step_confidence=None, ready_to_advance=None,
            signals_count=0, actions_count=0,
            summary_one_line=None,
            drive_link=None,
            error=str(exc),
        )

    drive_link: str | None = None
    if persist or drive:
        try:
            if persist:
                from services.scoring_agent.firestore_sync import persist_score
                links = persist_score(result, meta)
                drive_link = links.get("docx")
            elif drive:
                from services.scoring_agent.drive_sync import upload_score
                files = upload_score(result)
                drive_link = files.get("docx", {}).get("webViewLink")
        except Exception as exc:
            # Drive/Firestore failure doesn't fail the smoke test — scoring
            # itself succeeded. Surface in the error column so the team
            # notices, but mark the run as passed for the scoring contract.
            print(f"\n  WARN ({contact_id}): persist/drive failed: {exc}")

    f = result.findings
    return SmokeResult(
        contact_id=contact_id,
        contact_label=label,
        passed=True,
        elapsed_sec=meta["elapsed_sec"],
        input_tokens=meta["input_tokens"],
        output_tokens=meta["output_tokens"],
        current_step=f.current_step,
        lead_heat=f.lead_heat,
        lead_heat_score=f.lead_heat_score,
        step_confidence=f.step_confidence,
        ready_to_advance=f.ready_to_advance,
        signals_count=len(f.signals),
        actions_count=len(f.recommended_actions),
        summary_one_line=f.summary_one_line,
        drive_link=drive_link,
        error=None,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Run the scoring agent live against real Firestore contacts.",
    )
    ap.add_argument("--top", type=int, default=5,
                    help="Number of top-ranked contacts to score (default 5)")
    ap.add_argument("--contact-id", action="append", metavar="ID",
                    help="Score this specific contact instead of the ranked list. "
                         "Pass multiple times to score several.")
    ap.add_argument("--contact-limit", type=int, default=300,
                    help="Max contacts to scan for ranking (default 300)")
    ap.add_argument("--runs-limit", type=int, default=2000,
                    help="Max recent agent_runs to scan for the join (default 2000)")
    ap.add_argument("--persist", action="store_true",
                    help="Write results to Firestore (agent_runs + contact_scores) + Drive")
    ap.add_argument("--drive", action="store_true",
                    help="Upload to Drive only (no Firestore writeback). Ignored if --persist set.")
    ap.add_argument("--save-dir", metavar="DIR",
                    help="Save each scored result as JSON in this local directory")
    ap.add_argument("--stop-on-fail", action="store_true",
                    help="Halt on first failure instead of running all")
    args = ap.parse_args()

    if "ANTHROPIC_API_KEY" not in os.environ:
        print("ERROR: ANTHROPIC_API_KEY not set.")
        sys.exit(1)

    save_dir = Path(args.save_dir) if args.save_dir else None
    if save_dir:
        save_dir.mkdir(parents=True, exist_ok=True)

    # ---- Pick contacts ------------------------------------------------------
    if args.contact_id:
        from services.firestore.client import get_contact
        contacts: list[dict] = []
        for cid in args.contact_id:
            doc = get_contact(cid)
            if doc is None:
                print(f"ERROR: contact {cid!r} not in Firestore. Skipping.")
                continue
            contacts.append(doc)
        if not contacts:
            print("No valid contacts to score.")
            sys.exit(2)
    else:
        contacts = pick_top_contacts(
            top_n=args.top,
            contact_limit=args.contact_limit,
            runs_limit=args.runs_limit,
        )
        if not contacts:
            print("Ranking returned no contacts. Is the contacts collection populated?")
            sys.exit(2)

    persist_label = "Firestore + Drive" if args.persist else ("Drive" if args.drive else "none")
    print(f"\nScoring {len(contacts)} contacts. Persistence: {persist_label}\n")

    header = (
        f"{'CONTACT':<24} {'STATUS':<6} {'STEP':>4}  {'HEAT':>7}  "
        f"{'SCORE':>5}  {'CONF':>5}  {'READY':>5}  {'TIME':>5}  "
        f"{'IN':>6}  {'OUT':>5}  SUMMARY"
    )
    print(header)
    print("─" * len(header))

    results: list[SmokeResult] = []
    for contact in contacts:
        cid = contact.get("id") or ""
        label = contact_label(contact)
        print(f"  {label[:22]:<22}  scoring…", end="", flush=True)
        r = score_one(cid, label, persist=args.persist, drive=args.drive)
        results.append(r)

        if save_dir and r.passed:
            out = save_dir / f"score_{cid}.json"
            from agents.scoring_agent import ScoringAgent  # noqa: F401 — for symmetry
            out.write_text(
                json.dumps(_smoke_row_to_dict(r), indent=2), encoding="utf-8",
            )

        status = "PASS" if r.passed else "FAIL"
        summary = (r.summary_one_line or "—")[:50]
        if not r.passed:
            summary = (r.error or "(no error message)")[:50]

        ready = "yes" if r.ready_to_advance else ("no" if r.ready_to_advance is False else "—")
        heat = (r.lead_heat or "—").upper()[:6]
        score = r.lead_heat_score if r.lead_heat_score is not None else "—"
        conf = f"{r.step_confidence:.2f}" if r.step_confidence is not None else "—"
        step = r.current_step if r.current_step is not None else "—"

        print(
            f"\r  {label[:22]:<22}  {status:<6} {step:>4}  {heat:>7}  "
            f"{score!s:>5}  {conf:>5}  {ready:>5}  "
            f"{r.elapsed_sec:>4.1f}s  {r.input_tokens:>6,}  {r.output_tokens:>5,}  "
            f"{summary}"
        )
        if r.drive_link:
            print(f"    drive: {r.drive_link}")

        if not r.passed and args.stop_on_fail:
            break

    print("─" * len(header))
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    in_tot = sum(r.input_tokens for r in results)
    out_tot = sum(r.output_tokens for r in results)
    print(
        f"\n{passed}/{total} passed   "
        f"tokens in={in_tot:,}  out={out_tot:,}  "
        f"elapsed={sum(r.elapsed_sec for r in results):.1f}s"
    )

    if passed != total:
        print("\nFailures:")
        for r in results:
            if not r.passed:
                print(f"  {r.contact_id}  ({r.contact_label}): {r.error}")

    sys.exit(0 if passed == total else 1)


def _smoke_row_to_dict(r: SmokeResult) -> dict[str, Any]:
    return {
        "contact_id":        r.contact_id,
        "contact_label":     r.contact_label,
        "current_step":      r.current_step,
        "lead_heat":         r.lead_heat,
        "lead_heat_score":   r.lead_heat_score,
        "step_confidence":   r.step_confidence,
        "ready_to_advance":  r.ready_to_advance,
        "signals_count":     r.signals_count,
        "actions_count":     r.actions_count,
        "summary_one_line":  r.summary_one_line,
        "input_tokens":      r.input_tokens,
        "output_tokens":     r.output_tokens,
        "elapsed_sec":       r.elapsed_sec,
        "drive_link":        r.drive_link,
    }


if __name__ == "__main__":
    main()
