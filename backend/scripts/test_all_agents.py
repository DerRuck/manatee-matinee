"""
Smoke-test all research agent types in order of functional importance.

Each test uses --no-web-search (training-data only) to run fast and free.
A pass means: inputs resolved, binder retrieved, Claude responded, schema
validated. Web search correctness is tested separately in golden evals.

Run from backend/:
    python scripts/test_all_agents.py
    python scripts/test_all_agents.py --stop-on-fail
    python scripts/test_all_agents.py --types LOBBY-1 S1-4
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve()
_BACKEND = _HERE.parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from dotenv import load_dotenv
load_dotenv(_BACKEND.parent / ".env")

from agents.research_agent import ResearchAgent

# ---------------------------------------------------------------------------
# Test order — functional pipeline sequence
# ---------------------------------------------------------------------------

TESTS: list[tuple[str, dict]] = [
    # 1. Compliance gate — must clear before any formal outreach
    ("LOBBY-1", {
        "contact_id":        "ghl_test_005",
        "municipality_name": "Alachua County",
        "jurisdiction_name": "Alachua County",
        "jurisdiction_type": "county",
    }),

    # 2. Municipality background — research the target before Step 1
    ("PW-3", {
        "contact_id":      "ghl_test_003",
        "municipality_name": "Gainesville",
        "county":          "Alachua",
        "state":           "FL",
        "contact_name":    "Alex Rivera",
        "contact_title":   "Public Works Director",
    }),

    # 3. Contact internet research — full profile before first touch
    ("S1-4", {
        "contact_id":      "ghl_test_003",
        "municipality_name": "Gainesville",
        "contact_name":    "Alex Rivera",
        "contact_title":   "Public Works Director",
        "organization":    "City of Gainesville",
    }),

    # 4. LinkedIn connection prep — personalized opener before connecting
    ("S1-2", {
        "contact_id":      "ghl_test_003",
        "municipality_name": "Gainesville",
        "contact_name":    "Alex Rivera",
        "contact_title":   "Public Works Director",
        "organization":    "City of Gainesville",
    }),

    # 5. Conference attendee research — pre-event lead mapping
    ("PW-1", {
        "contact_id":      "ghl_test_conference_001",
        "municipality_name": None,
        "conference_name": "FSBPA 2026 Annual Conference",
        "conference_date": "2026-09-15",
        "location":        "Florida",
    }),

    # 6. Pre-meeting research package — intake meeting prep (Step 3)
    ("S3-PREP", {
        "contact_id":      "ghl_test_003",
        "municipality_name": "Gainesville",
        "county":          "Alachua",
        "contact_name":    "Alex Rivera",
        "contact_title":   "Public Works Director",
        "meeting_date":    "2026-06-10",
        "project_hint":    "Stormwater retrofit along Hogtown Creek; stalled in FDEP for two years.",
    }),

    # 7. Commission meeting prep — political landscape before attending
    ("S3-3", {
        "contact_id":      "ghl_test_003",
        "municipality_name": "Gainesville",
        "meeting_date":    "2026-06-09",
        "meeting_goal":    "observe",
        "project_status":  "Pre-Step-3 — contact agreed to intake meeting, no formal proposal yet.",
    }),

    # 8. Custom deck research — presentation prep (Step 4)
    ("S4-DECK", {
        "contact_id":      "ghl_test_001",
        "municipality_name": "Sarasota",
        "project_focus":   "Sarasota Bay seagrass restoration",
        "problem_areas":   "Sarasota Bay — three identified problem zones along the shoreline.",
        "champion_priorities": (
            "Visible early wins, P3 funding structure, nature-based approach "
            "cheaper than seawalls."
        ),
    }),

    # 9. Grant opportunity research — funding identification (Step 6)
    ("S6-1", {
        "contact_id":        "ghl_test_001",
        "municipality_name": "Sarasota",
        "county":            "Sarasota",
        "project_type":      "seagrass_restoration",
        "estimated_cost_usd": 4_400_000,
        "project_overview":  (
            "Restore 18 acres of historical seagrass habitat in Sarasota Bay "
            "using nature-based shoreline stabilization and water quality "
            "improvements at three identified problem zones."
        ),
        "p3_intent": "yes",
    }),
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

@dataclass
class Result:
    research_type: str
    passed: bool
    elapsed: float
    input_tokens: int
    output_tokens: int
    binder_chars: int
    brief: object | None
    error: str | None


def run_one(research_type: str, contact: dict, web_search: bool = False) -> Result:
    from services.research_agent.runner import retrieve_binder_context, load_prompt

    yaml_path = _BACKEND / "prompts" / "research_agent" / research_type / "v1.yaml"
    cfg = load_prompt(yaml_path)
    binder_text = retrieve_binder_context(cfg)

    t0 = time.time()
    try:
        agent = ResearchAgent(research_type)
        brief, meta = agent.run(contact, no_web_search=not web_search, verbose=False)
        return Result(
            research_type=research_type,
            passed=True,
            elapsed=meta["elapsed_sec"],
            input_tokens=meta["input_tokens"],
            output_tokens=meta["output_tokens"],
            binder_chars=len(binder_text),
            brief=brief,
            error=None,
        )
    except Exception as exc:
        return Result(
            research_type=research_type,
            passed=False,
            elapsed=round(time.time() - t0, 1),
            input_tokens=0,
            output_tokens=0,
            binder_chars=len(binder_text),
            brief=None,
            error=str(exc),
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stop-on-fail", action="store_true",
                    help="Halt on first failure instead of running all tests")
    ap.add_argument("--types", nargs="+", metavar="TYPE",
                    help="Run only these research types (e.g. LOBBY-1 S1-4)")
    ap.add_argument("--drive", action="store_true",
                    help="Upload each passing brief to Google Drive")
    ap.add_argument("--drive-folder",
                    help="Drive folder ID (defaults to drive_sync.DEFAULT_FOLDER_ID)")
    ap.add_argument("--save-dir", metavar="DIR",
                    help="Save each passing brief as JSON in this local directory")
    ap.add_argument("--web-search", action="store_true",
                    help="Enable web_search and web_fetch tools (slower, billed)")
    args = ap.parse_args()

    if "ANTHROPIC_API_KEY" not in os.environ:
        print("ERROR: ANTHROPIC_API_KEY not set.")
        sys.exit(1)

    save_dir = Path(args.save_dir) if args.save_dir else None
    if save_dir:
        save_dir.mkdir(parents=True, exist_ok=True)

    tests = [(rt, c) for rt, c in TESTS if not args.types or rt in args.types]

    print(f"\n{'TYPE':<12} {'RESULT':<8} {'TIME':>6}  {'IN':>6}  {'OUT':>5}  {'BINDER':>7}  OUTPUT")
    print("─" * 80)

    results: list[Result] = []
    for research_type, contact in tests:
        print(f"{research_type:<12} {'running':<8}", end="", flush=True)
        r = run_one(research_type, contact, web_search=args.web_search)
        results.append(r)

        status = "PASS" if r.passed else "FAIL"
        output_note = ""

        if r.passed and r.brief:
            if save_dir:
                from services.research_agent.drive_sync import filename_for
                out_path = save_dir / filename_for(r.brief, "json")
                out_path.write_text(r.brief.model_dump_json(indent=2), encoding="utf-8")
                output_note = f"  saved → {out_path.name}"

            if args.drive:
                try:
                    from services.research_agent.drive_sync import upload_brief, DEFAULT_FOLDER_ID
                    folder = args.drive_folder or DEFAULT_FOLDER_ID
                    files = upload_brief(r.brief, folder_id=folder)
                    links = "  ".join(f["webViewLink"] for f in files.values())
                    output_note = f"  drive → {links}"
                except Exception as exc:
                    output_note = f"  drive FAILED: {str(exc)[:40]}"

        err_snippet = f"  {r.error[:50]}..." if r.error else output_note
        print(
            f"\r{r.research_type:<12} {status:<8} "
            f"{r.elapsed:>5.1f}s  {r.input_tokens:>6,}  {r.output_tokens:>5,}  "
            f"{r.binder_chars:>6,}c{err_snippet}"
        )
        if not r.passed and args.stop_on_fail:
            break

    passed = sum(1 for r in results if r.passed)
    total = len(results)
    print("─" * 80)
    print(f"\n{passed}/{total} passed", end="")
    if passed == total:
        print("  ✓ all green")
    else:
        print()
        for r in results:
            if not r.passed:
                print(f"\n  {r.research_type} error:\n  {r.error}")
    print()

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
