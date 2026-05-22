"""
CLI for the Scoring Agent.

The scoring agent reads every signal we have about a contact (Firestore
contact record + agent_runs history) and emits a structured assessment
of where they are in the Proven Process and how hot the lead is.

Run from backend/:

    # Score a real Firestore contact end-to-end
    python scripts/run_scoring_agent.py 0I21saCPXJVEbdncGXEW

    # Save the result locally, skip the Firestore writeback
    python scripts/run_scoring_agent.py 0I21saCPXJVEbdncGXEW \\
        --save /tmp/score.json --no-persist

    # Use a custom prompt version
    python scripts/run_scoring_agent.py 0I21saCPXJVEbdncGXEW --version 2

    # Tag the run with a non-manual trigger (for telemetry)
    python scripts/run_scoring_agent.py 0I21saCPXJVEbdncGXEW --triggered-by daily

What "testable" means here:
  The CLI hits the same code path as the webhook dispatcher and the
  daily-cron sweep. If --contact-id resolves a real contact in
  Firestore, the score is real. If you want to test the prompt against
  a synthetic context, use the unit tests instead.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_BACKEND = _HERE.parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from dotenv import load_dotenv
load_dotenv(_BACKEND.parent / ".env")

from agents.scoring_agent import ScoringAgent
from services.scoring_agent.schema import ScoringResult


def print_score_summary(result: ScoringResult) -> None:
    f = result.findings
    print("\n" + "=" * 70)
    print(f"SCORING RESULT — {result.score_type_id} v{result.prompt_version}")
    print("=" * 70)
    print(f"Run id:               {result.run_id}")
    print(f"Contact:              {result.contact_id}")
    print(f"Municipality:         {result.municipality_name}")
    print(f"Triggered by:         {result.triggered_by}")
    print()
    print(f"Step placement:       {f.current_step_name}  (phase {f.current_phase})")
    print(f"Step confidence:      {f.step_confidence:.2f}")
    print(f"Ready to advance:     {f.ready_to_advance}")
    if not f.ready_to_advance:
        for b in f.next_step_blockers:
            owner = f" ({b.owner})" if b.owner else ""
            print(f"  blocker [{b.severity:6}]: {b.description}{owner}")
    print()
    print(f"Lead heat:            {f.lead_heat.upper()}  ({f.lead_heat_score}/100)")
    print(f"Days since signal:    {f.days_since_last_signal}")
    print()
    print(f"  >>> {f.summary_one_line}")
    print()
    print(f"Signals ({len(f.signals)}):")
    for s in f.signals[:5]:
        print(f"  [{s.impact:8}] (w={s.weight:.2f}) {s.description}")
        print(f"             {s.evidence_source}")
    print()
    print(f"Recommended actions ({len(f.recommended_actions)}):")
    for a in f.recommended_actions:
        step_tag = f" [Step {a.proven_process_step}]" if a.proven_process_step else ""
        print(f"  ({a.owner}, ≤{a.due_within_days}d){step_tag} {a.action}")

    if f.go_no_go:
        gng = f.go_no_go
        print()
        print(f"Go/No-Go scorecard:   total {gng.total}/21  →  {gng.decision}")
        print(f"  {gng.rationale}")

    if result.notes:
        print()
        print(f"Agent notes:          {result.notes}")
    print("=" * 70)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Score one C-HAWQ contact against the Proven Process."
    )
    ap.add_argument("contact_id", help="GHL contact id (Firestore doc id)")
    ap.add_argument(
        "--score-type", default="PIPELINE-SCORE",
        help="Score type id (default PIPELINE-SCORE)",
    )
    ap.add_argument("--version", type=int, default=1)
    ap.add_argument(
        "--triggered-by", default="manual",
        choices=["daily", "new_data", "manual", "webhook"],
        help="Telemetry tag for what kicked this run off",
    )
    ap.add_argument("--model", help="Override the model in the YAML")
    ap.add_argument("--save", help="Save the validated ScoringResult JSON to this path")
    ap.add_argument(
        "--no-persist", action="store_true",
        help="Skip the Firestore writeback (agent_runs + contact_scores)",
    )
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if "ANTHROPIC_API_KEY" not in os.environ:
        print("ERROR: ANTHROPIC_API_KEY not set. export ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)

    from services.scoring_agent.context_builder import build_scoring_context

    try:
        context = build_scoring_context(args.contact_id, triggered_by=args.triggered_by)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        sys.exit(2)

    if not args.quiet:
        print(
            f"Scoring {args.contact_id} ({context.get('municipality_name') or 'no municipality'}) "
            f"— {args.score_type} v{args.version}, trigger={args.triggered_by}"
        )
        agent_runs = context.get("agent_runs_summary") or []
        print(f"  agent runs in history: {len(agent_runs)}")

    agent = ScoringAgent(args.score_type, version=args.version)
    result, meta = agent.run(context, model=args.model, verbose=not args.quiet)
    print_score_summary(result)

    if args.save:
        Path(args.save).write_text(result.model_dump_json(indent=2), encoding="utf-8")
        print(f"\nSaved result: {args.save}")

    if not args.no_persist:
        try:
            from services.scoring_agent.firestore_sync import persist_score
            drive_links = persist_score(result, meta)
            persist_line = (
                f"\nPersisted: agent_runs/{result.run_id}  +  "
                f"contact_scores/{result.contact_id}"
            )
            if drive_links.get("docx"):
                persist_line += f"\nDrive:     {drive_links['docx']}"
            print(persist_line)
        except Exception as exc:
            print(f"\nWARN: Firestore persist failed: {exc}")


if __name__ == "__main__":
    main()
