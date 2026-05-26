"""
CLI for the daily scoring sweep.

The scoring sweep iterates every eligible contact in Firestore and runs
the Scoring Agent against each, so the workbook UI starts the morning
with fresh `contact_scores` rows. The same function (run_daily_sweep)
runs from POST /jobs/scoring/daily in production — this CLI exists so
the team can trigger a sweep ad-hoc or test the eligibility filter
without Cloud Scheduler.

Run from backend/:

    # Standard sweep — score up to 100 eligible contacts
    python scripts/run_daily_scoring.py

    # Dry run — print what would be scored, hit no LLM API
    python scripts/run_daily_scoring.py --dry-run

    # Score everyone, even contacts scored within the last 18h
    python scripts/run_daily_scoring.py --min-age-hours 0

    # Limit the sweep to N contacts (useful for debugging)
    python scripts/run_daily_scoring.py --max-contacts 5

Eligibility logic lives in services/scoring_agent/sweep.py — see the
docstring there for the exact filter rules.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_BACKEND = _HERE.parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from dotenv import load_dotenv
load_dotenv(_BACKEND.parent / ".env")


def _print_report(report, verbose: bool) -> None:
    elapsed = (
        (report.finished_at - report.started_at).total_seconds()
        if report.finished_at else 0
    )
    print()
    print("=" * 70)
    print(f"SCORING SWEEP — {report.sweep_id}")
    print("=" * 70)
    print(f"Triggered by:   {report.triggered_by}")
    print(f"Elapsed:        {elapsed:.1f}s")
    print(f"Eligible:       {report.total_eligible}")
    print(f"Scored:         {report.total_scored}")
    print(f"Skipped:        {report.total_skipped}")
    print(f"Failed:         {report.total_failed}")
    print("=" * 70)

    if verbose:
        for o in report.outcomes:
            if o.status == "scored":
                print(
                    f"  [scored ]  {o.contact_id:24}  "
                    f"{o.lead_heat or '-':6}  {o.lead_heat_score or 0:>3}/100  "
                    f"in={o.input_tokens or 0:>5}  out={o.output_tokens or 0:>5}  "
                    f"{o.elapsed_sec or 0:.1f}s"
                )
            elif o.status == "skipped":
                print(f"  [skipped]  {o.contact_id:24}  reason={o.skipped_reason}")
            else:
                print(f"  [FAILED ]  {o.contact_id:24}  error={(o.error or '')[:80]}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Run the daily scoring sweep against every eligible contact."
    )
    ap.add_argument("--max-contacts", type=int, default=100,
                    help="Hard cap on contacts scanned (default 100)")
    ap.add_argument("--triggered-by", default="manual",
                    choices=["daily", "manual", "webhook", "new_data"])
    ap.add_argument("--min-age-hours", type=int, default=18,
                    help="Skip contacts scored within this window (default 18)")
    ap.add_argument("--no-skip-lost", action="store_true",
                    help="Score contacts whose latest heat is 'lost' (default: skip)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Build eligibility but skip the LLM + persist steps")
    ap.add_argument("--no-persist", action="store_true",
                    help="Skip writing the sweep audit doc to Firestore")
    ap.add_argument("--quiet", action="store_true",
                    help="Suppress per-contact output (just print summary)")
    args = ap.parse_args()

    if not args.dry_run and "ANTHROPIC_API_KEY" not in os.environ:
        print("ERROR: ANTHROPIC_API_KEY not set (or pass --dry-run).")
        sys.exit(1)

    from services.scoring_agent.sweep import run_daily_sweep

    if not args.quiet:
        print(
            f"Sweep starting: max_contacts={args.max_contacts}, "
            f"min_age_hours={args.min_age_hours}, dry_run={args.dry_run}"
        )

    report = run_daily_sweep(
        max_contacts=args.max_contacts,
        triggered_by=args.triggered_by,
        min_age_hours=args.min_age_hours,
        skip_lost=not args.no_skip_lost,
        dry_run=args.dry_run,
        persist_report=not args.no_persist,
    )

    _print_report(report, verbose=not args.quiet)

    # Non-zero exit when every contact failed — useful for the CLI exit
    # status when run from a cron wrapper (Cloud Scheduler ignores this).
    if report.total_eligible > 0 and report.total_scored == 0 and report.total_failed > 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
