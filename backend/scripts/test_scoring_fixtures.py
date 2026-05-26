"""
Fixture-driven verification for the Scoring Agent.

Runs the scoring agent against hand-crafted synthetic contexts that
represent known Proven Process states (Step 1 cold, Step 4 ready, Step
7 mobilized, stalled lead, etc.) and checks the result against expected
ranges. Catches prompt + schema regressions in a handful of LLM calls
without needing Firestore, Drive, or real contact data.

Two kinds of assertions per fixture:

  - Structural (always required) — the result validates against
    ScoringResult, contains at least one signal, summary_one_line is
    non-empty, etc.
  - Banding (per fixture) — current_step within an expected range,
    lead_heat in an allowed set, lead_heat_score within bounds,
    ready_to_advance matches the scenario.

Banding ranges are intentionally loose: the goal is to catch obvious
regressions (Step 4 ready-to-advance contact suddenly scoring at Step 1
cold), not to lock the model into exact numbers. Tighten the ranges if
you want stricter golden tests; loosen them when the model legitimately
re-classifies an edge case.

Run from backend/:

    # All fixtures
    python scripts/test_scoring_fixtures.py

    # Just one
    python scripts/test_scoring_fixtures.py --fixture step4_ready

    # List available fixtures
    python scripts/test_scoring_fixtures.py --list

    # Save validated outputs for diff vs. a future run
    python scripts/test_scoring_fixtures.py --save-dir /tmp/scoring_baseline

    # Render docx locally so you can eyeball the formatted output
    python scripts/test_scoring_fixtures.py --save-docx-dir /tmp/scoring_docx

    # Upload each fixture's docx to Drive — use a sandbox folder so the
    # real contact tree doesn't get fixture results mixed in
    python scripts/test_scoring_fixtures.py --drive --drive-folder 1abc...

    # Try a different model
    python scripts/test_scoring_fixtures.py --model claude-opus-4-7

Exit code 0 only when every selected fixture passes its banding asserts.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve()
_BACKEND = _HERE.parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from dotenv import load_dotenv
load_dotenv(_BACKEND.parent / ".env")


# =============================================================================
# Fixtures
# =============================================================================

@dataclass
class Fixture:
    name:         str
    description:  str
    context:      dict[str, Any]
    expected:     dict[str, Any] = field(default_factory=dict)


def _contact_record(**overrides) -> dict[str, Any]:
    """A reasonable default flattened contact_record. Mirrors what
    build_context_from_contact emits so the agent sees a familiar shape.
    """
    base = {
        "contact_id":         "fixture-contact",
        "contact_name":       "Sample Contact",
        "first_name":         "Sample",
        "last_name":          "Contact",
        "email":              "contact@example.org",
        "company_name":       None,
        "municipality_name":  "Sample City",
        "city":               "Sample City",
        "state":              "FL",
        "tags":               [],
        "type":               "lead",
        "custom_fields":      {},
    }
    base.update(overrides)
    return base


def _run_summary(agent_type: str, days_ago: int, key_finding: str | None = None,
                 step: int | None = None) -> dict[str, Any]:
    """One row in agent_runs_summary, as context_builder._summarize_agent_runs emits."""
    from datetime import datetime, timezone, timedelta
    ts = datetime.now(tz=timezone.utc) - timedelta(days=days_ago)
    return {
        "run_id":               f"run_{agent_type}_{days_ago}d",
        "agent_type":           agent_type,
        "proven_process_step":  step,
        "finished_at":          ts.isoformat(),
        "key_finding":          key_finding,
        "model":                "claude-sonnet-4-6",
        "status":               "succeeded",
    }


FIXTURES: list[Fixture] = [

    # -----------------------------------------------------------------
    # Step 1 cold — bare stub, no prior work, no signal
    # -----------------------------------------------------------------
    Fixture(
        name="step1_cold",
        description=(
            "Brand-new contact, no tags, no agent_runs, no comms. The "
            "agent should place this at Step 1 with cold heat and low score."
        ),
        context={
            "contact_id":              "fixture-step1-cold",
            "municipality_name":       "Sample City",
            "contact_record":          _contact_record(
                contact_id="fixture-step1-cold",
                tags=[],
            ),
            "agent_runs_summary":      [],
            "recent_communications":   None,
            "ghl_pipeline_stage":      None,
            "triggered_by":            "manual",
            "days_since_last_signal":  None,
        },
        expected={
            "current_step_in":     [1, 2],
            "lead_heat_in":        ["cold", "simmer"],
            "lead_heat_score_max": 40,
            "ready_to_advance":    False,
        },
    ),

    # -----------------------------------------------------------------
    # Step 2 simmer — initial outreach research done, contact responded
    # -----------------------------------------------------------------
    Fixture(
        name="step2_simmer",
        description=(
            "Initial research run (S1-4) done; contact has replied to "
            "outreach. Should be Step 2-3, simmering, modest score."
        ),
        context={
            "contact_id":              "fixture-step2-simmer",
            "municipality_name":       "Cedar Key",
            "contact_record":          _contact_record(
                contact_id="fixture-step2-simmer",
                contact_name="Marcia Holloway",
                first_name="Marcia",
                last_name="Holloway",
                email="mholloway@cedarkey.fl.gov",
                municipality_name="Cedar Key",
                city="Cedar Key",
                tags=["intake-pending", "simmer"],
                custom_fields={"contact_notes": "Asked good questions about seagrass restoration."},
                contact_notes="Asked good questions about seagrass restoration.",
            ),
            "agent_runs_summary": [
                _run_summary("S1-4", days_ago=14, step=1,
                             key_finding="20-year city manager; led last canal project."),
                _run_summary("S1-2", days_ago=12, step=1),
            ],
            "recent_communications": [
                {
                    "channel": "email", "direction": "inbound",
                    "timestamp": "2026-05-20T16:00:00Z",
                    "subject": "Re: C-HAWQ intro",
                    "body": "Thanks for reaching out — would love to learn more about your work on seagrass.",
                }
            ],
            "ghl_pipeline_stage":      None,
            "triggered_by":            "manual",
            "days_since_last_signal":  6,
        },
        expected={
            "current_step_in":     [2, 3],
            "lead_heat_in":        ["simmer", "boil"],
            "lead_heat_score_min": 30,
            "lead_heat_score_max": 75,
        },
    ),

    # -----------------------------------------------------------------
    # Step 4 ready to advance — Champion + commissioners confirmed
    # -----------------------------------------------------------------
    Fixture(
        name="step4_ready",
        description=(
            "S3-PREP, S4-LETTER, and PA-STEP4 all done. Champion has "
            "confirmed site walk with two commissioners. Should be boil, "
            "high score, ready_to_advance=True."
        ),
        context={
            "contact_id":              "fixture-step4-ready",
            "municipality_name":       "Naples",
            "contact_record":          _contact_record(
                contact_id="fixture-step4-ready",
                contact_name="Jared Reynolds",
                first_name="Jared",
                last_name="Reynolds",
                email="jreynolds@rookerybay.org",
                company_name="Rookery Bay NERR",
                municipality_name="Naples",
                tags=["boil", "champion-confirmed", "step4-prep-done"],
                custom_fields={
                    "job_title": "Director, Rookery Bay NERR",
                    "contact_notes": "Champion confirmed June 12 site walk; bringing two commissioners.",
                },
                job_title="Director, Rookery Bay NERR",
                contact_notes="Champion confirmed June 12 site walk; bringing two commissioners.",
            ),
            "agent_runs_summary": [
                _run_summary("S3-PREP",  days_ago=30, step=3,
                             key_finding="Strong intake; Boil scorecard 17/21."),
                _run_summary("S4-LETTER", days_ago=20, step=4,
                             key_finding="Personalized letter sent; thank-you reply received."),
                _run_summary("PA-STEP4", days_ago=4, step=4,
                             key_finding="Site walk presentation ready for June 12."),
            ],
            "recent_communications": [
                {
                    "channel": "email", "direction": "inbound",
                    "timestamp": "2026-05-22T14:00:00Z",
                    "subject": "Re: site walk",
                    "body": "Confirmed June 12, 10am. Bringing Commissioner Diaz and Commissioner Wells.",
                },
            ],
            "ghl_pipeline_stage":      "Project Pipeline / Step 4 — Schedule",
            "triggered_by":            "manual",
            "days_since_last_signal":  4,
        },
        expected={
            "current_step_in":     [4, 5],
            "lead_heat_in":        ["boil"],
            "lead_heat_score_min": 70,
            "ready_to_advance":    True,
        },
    ),

    # -----------------------------------------------------------------
    # Step 7 mobilized — project placed, funding underway
    # -----------------------------------------------------------------
    Fixture(
        name="step7_mobilized",
        description=(
            "Project placed (S7-PLAN), commission resolution in flight "
            "(S8-x). Should land at Step 7-8, boil, very high score."
        ),
        context={
            "contact_id":              "fixture-step7",
            "municipality_name":       "Tallahassee",
            "contact_record":          _contact_record(
                contact_id="fixture-step7",
                contact_name="Dr. Lena Park",
                first_name="Lena",
                last_name="Park",
                email="lpark@talgov.com",
                municipality_name="Tallahassee",
                tags=["boil", "p3-active", "commission-prep"],
                custom_fields={"job_title": "Director of Sustainability"},
                job_title="Director of Sustainability",
            ),
            "agent_runs_summary": [
                _run_summary("S3-PREP",  days_ago=90, step=3),
                _run_summary("S4-LETTER", days_ago=80, step=4),
                _run_summary("S5-1",     days_ago=70, step=5,
                             key_finding="Internal coalition built; staff signed off."),
                _run_summary("S6-1",     days_ago=55, step=6,
                             key_finding="Project package finalized."),
                _run_summary("S7-PLAN",  days_ago=30, step=7,
                             key_finding="Project placed with city; awaiting commission vote."),
            ],
            "recent_communications": [
                {
                    "channel": "email", "direction": "outbound",
                    "timestamp": "2026-05-23T19:00:00Z",
                    "subject": "Commission packet sent",
                    "body": "Sent the commission packet for the 6/4 agenda. Champion confirmed sponsorship.",
                },
            ],
            "ghl_pipeline_stage":      "Project Pipeline / Step 7 — Placed",
            "triggered_by":            "manual",
            "days_since_last_signal":  3,
        },
        expected={
            "current_step_in":     [6, 7, 8],
            "lead_heat_in":        ["boil"],
            "lead_heat_score_min": 75,
        },
    ),

    # -----------------------------------------------------------------
    # Stalled — was warm, gone dark for 90+ days
    # -----------------------------------------------------------------
    Fixture(
        name="stalled_lead",
        description=(
            "S3-PREP done, S4 attempted, no signal in 95 days. Should "
            "be tagged stall (or cold), low confidence on advancement, "
            "ready_to_advance=False."
        ),
        context={
            "contact_id":              "fixture-stalled",
            "municipality_name":       "Sarasota",
            "contact_record":          _contact_record(
                contact_id="fixture-stalled",
                contact_name="Tom Rivers",
                first_name="Tom",
                last_name="Rivers",
                email="trivers@sarasota.gov",
                municipality_name="Sarasota",
                tags=["stall", "follow-up-needed"],
                custom_fields={"contact_notes": "Went quiet after intake; rumored to be reorging."},
                contact_notes="Went quiet after intake; rumored to be reorging.",
            ),
            "agent_runs_summary": [
                _run_summary("S3-PREP",  days_ago=105, step=3),
                _run_summary("S4-LETTER", days_ago=95, step=4,
                             key_finding="Letter sent; no response."),
            ],
            "recent_communications":   None,
            "ghl_pipeline_stage":      "Project Pipeline / Step 4 — Schedule",
            "triggered_by":            "manual",
            "days_since_last_signal":  95,
        },
        expected={
            "current_step_in":     [3, 4],
            "lead_heat_in":        ["stall", "cold"],
            "lead_heat_score_max": 50,
            "ready_to_advance":    False,
        },
    ),
]


# =============================================================================
# Assertions
# =============================================================================

@dataclass
class CheckResult:
    fixture:        str
    passed:         bool
    elapsed_sec:    float
    input_tokens:   int
    output_tokens: int
    actual:         dict[str, Any]
    failures:       list[str]
    error:          str | None = None
    # Carried so the CLI can upload to Drive without re-running the agent.
    result:         Any = None
    drive_link:     str | None = None


def _check_expectations(actual: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    """Return a list of human-readable failure messages — empty when all pass."""
    failures: list[str] = []

    if "current_step_in" in expected:
        allowed = expected["current_step_in"]
        if actual["current_step"] not in allowed:
            failures.append(
                f"current_step={actual['current_step']} not in {allowed}"
            )

    if "lead_heat_in" in expected:
        allowed = expected["lead_heat_in"]
        if actual["lead_heat"] not in allowed:
            failures.append(
                f"lead_heat={actual['lead_heat']!r} not in {allowed}"
            )

    if "lead_heat_score_min" in expected:
        floor = expected["lead_heat_score_min"]
        if actual["lead_heat_score"] < floor:
            failures.append(
                f"lead_heat_score={actual['lead_heat_score']} below floor {floor}"
            )

    if "lead_heat_score_max" in expected:
        ceiling = expected["lead_heat_score_max"]
        if actual["lead_heat_score"] > ceiling:
            failures.append(
                f"lead_heat_score={actual['lead_heat_score']} above ceiling {ceiling}"
            )

    if "ready_to_advance" in expected:
        want = expected["ready_to_advance"]
        if actual["ready_to_advance"] != want:
            failures.append(
                f"ready_to_advance={actual['ready_to_advance']} (expected {want})"
            )

    # Structural — always-on checks that the result is usable
    if not actual.get("summary_one_line"):
        failures.append("summary_one_line is empty")
    if actual.get("signals_count", 0) < 1:
        failures.append("no signals returned")

    return failures


# =============================================================================
# Runner
# =============================================================================

def run_one(fixture: Fixture, model: str | None, verbose: bool) -> CheckResult:
    from agents.scoring_agent import ScoringAgent

    t0 = time.time()
    try:
        agent = ScoringAgent("PIPELINE-SCORE")
        result, meta = agent.run(fixture.context, model=model, verbose=False)
    except Exception as exc:
        return CheckResult(
            fixture=fixture.name,
            passed=False,
            elapsed_sec=round(time.time() - t0, 1),
            input_tokens=0,
            output_tokens=0,
            actual={},
            failures=[],
            error=str(exc),
        )

    f = result.findings
    actual = {
        "current_step":      f.current_step,
        "current_step_name": f.current_step_name,
        "lead_heat":         f.lead_heat,
        "lead_heat_score":   f.lead_heat_score,
        "step_confidence":   f.step_confidence,
        "ready_to_advance":  f.ready_to_advance,
        "signals_count":     len(f.signals),
        "actions_count":     len(f.recommended_actions),
        "summary_one_line":  f.summary_one_line,
    }
    failures = _check_expectations(actual, fixture.expected)

    if verbose:
        print(f"\n--- {fixture.name} full result ---")
        print(json.dumps(actual, indent=2))

    return CheckResult(
        fixture=fixture.name,
        passed=not failures,
        elapsed_sec=meta["elapsed_sec"],
        input_tokens=meta["input_tokens"],
        output_tokens=meta["output_tokens"],
        actual=actual,
        failures=failures,
        result=result,
    )


# =============================================================================
# CLI
# =============================================================================

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Verify the scoring agent against synthetic golden fixtures.",
    )
    ap.add_argument("--fixture", action="append", metavar="NAME",
                    help="Run only this fixture (pass multiple times for several).")
    ap.add_argument("--list", action="store_true",
                    help="List available fixtures and exit.")
    ap.add_argument("--model",
                    help="Override the model in the YAML (e.g. claude-opus-4-7).")
    ap.add_argument("--save-dir", metavar="DIR",
                    help="Save each fixture's actual values as JSON for diffing.")
    ap.add_argument("--save-docx-dir", metavar="DIR",
                    help="Render each fixture's docx locally and write to this directory.")
    ap.add_argument("--drive", action="store_true",
                    help="Upload each rendered docx + JSON to Drive (fixture contacts only).")
    ap.add_argument("--drive-folder", metavar="FOLDER_ID",
                    help="Override the Drive root folder. Useful for sandboxing fixture runs.")
    ap.add_argument("--stop-on-fail", action="store_true",
                    help="Halt on first failure.")
    ap.add_argument("--verbose", action="store_true",
                    help="Print the full actual values per fixture.")
    args = ap.parse_args()

    if args.list:
        for f in FIXTURES:
            print(f"  {f.name:<18}  {f.description}")
        return

    if "ANTHROPIC_API_KEY" not in os.environ:
        print("ERROR: ANTHROPIC_API_KEY not set.")
        sys.exit(1)

    save_dir = Path(args.save_dir) if args.save_dir else None
    if save_dir:
        save_dir.mkdir(parents=True, exist_ok=True)

    save_docx_dir = Path(args.save_docx_dir) if args.save_docx_dir else None
    if save_docx_dir:
        save_docx_dir.mkdir(parents=True, exist_ok=True)

    selected = FIXTURES
    if args.fixture:
        wanted = set(args.fixture)
        selected = [f for f in FIXTURES if f.name in wanted]
        unknown = wanted - {f.name for f in FIXTURES}
        if unknown:
            print(f"ERROR: unknown fixture(s): {sorted(unknown)}")
            sys.exit(2)

    print(f"\nRunning {len(selected)} fixture(s) against the scoring agent.\n")
    header = (
        f"{'FIXTURE':<18} {'RESULT':<6} {'STEP':>4}  {'HEAT':>6}  "
        f"{'SCORE':>5}  {'READY':>5}  {'TIME':>5}  {'IN':>6}  {'OUT':>5}"
    )
    print(header)
    print("─" * len(header))

    results: list[CheckResult] = []
    for fixture in selected:
        print(f"  {fixture.name:<16}  running…", end="", flush=True)
        r = run_one(fixture, model=args.model, verbose=args.verbose)
        results.append(r)

        if save_dir:
            (save_dir / f"{fixture.name}.json").write_text(
                json.dumps({
                    "fixture":  fixture.name,
                    "expected": fixture.expected,
                    "actual":   r.actual,
                    "failures": r.failures,
                    "passed":   r.passed,
                    "tokens":   {"in": r.input_tokens, "out": r.output_tokens},
                    "elapsed":  r.elapsed_sec,
                }, indent=2),
                encoding="utf-8",
            )

        # Optional artifact outputs — only when the agent actually returned a
        # validated result. Failures (parse / validation errors) skip these.
        if r.result is not None and save_docx_dir:
            try:
                from services.scoring_agent.drive_sync import filename_for, render_docx
                docx_bytes = render_docx(r.result)
                (save_docx_dir / filename_for(r.result, "docx")).write_bytes(docx_bytes)
            except Exception as exc:
                print(f"\n  WARN ({fixture.name}): local docx render failed: {exc}")

        if r.result is not None and args.drive:
            try:
                from services.scoring_agent.drive_sync import (
                    DEFAULT_FOLDER_ID, upload_score,
                )
                target = args.drive_folder or DEFAULT_FOLDER_ID
                files = upload_score(r.result, folder_id=target)
                r.drive_link = files.get("docx", {}).get("webViewLink")
            except Exception as exc:
                print(f"\n  WARN ({fixture.name}): Drive upload failed: {exc}")

        status = "PASS" if r.passed else "FAIL"
        if r.error:
            print(
                f"\r  {fixture.name:<18} {status:<6}  ERROR: {r.error[:60]}"
            )
            if args.stop_on_fail:
                break
            continue

        step = r.actual["current_step"]
        heat = r.actual["lead_heat"].upper()[:5]
        score = r.actual["lead_heat_score"]
        ready = "yes" if r.actual["ready_to_advance"] else "no"
        print(
            f"\r  {fixture.name:<18} {status:<6} {step:>4}  {heat:>6}  "
            f"{score:>5}  {ready:>5}  "
            f"{r.elapsed_sec:>4.1f}s  {r.input_tokens:>6,}  {r.output_tokens:>5,}"
        )
        for fail in r.failures:
            print(f"      ✗ {fail}")
        if r.drive_link:
            print(f"      drive: {r.drive_link}")
        if not r.passed and args.stop_on_fail:
            break

    print("─" * len(header))
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    in_tot = sum(r.input_tokens for r in results)
    out_tot = sum(r.output_tokens for r in results)
    print(
        f"\n{passed}/{total} fixtures passed.   "
        f"tokens in={in_tot:,}  out={out_tot:,}  "
        f"elapsed={sum(r.elapsed_sec for r in results):.1f}s"
    )

    if passed != total:
        print("\nFailing fixtures:")
        for r in results:
            if not r.passed:
                print(f"  {r.fixture}:")
                if r.error:
                    print(f"    error: {r.error}")
                for fail in r.failures:
                    print(f"    ✗ {fail}")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
