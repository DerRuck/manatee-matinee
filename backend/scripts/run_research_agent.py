"""
CLI for the Research Agent.

Run from the repo root (not backend/):

    # PW-3 municipality background (default contact: stuart_pw3)
    python -m backend.scripts.run_research_agent PW-3

    # S6-1 grant research for a seagrass project
    python -m backend.scripts.run_research_agent S6-1 --contact sample_seagrass

    # Save the brief as JSON
    python -m backend.scripts.run_research_agent PW-3 --save brief.json

    # Override model
    python -m backend.scripts.run_research_agent S6-1 --model claude-haiku-4-5-20251001

    # Skip web search (faster, uses training data only)
    python -m backend.scripts.run_research_agent S3-PREP --contact sample_intake --no-web-search

    # Skip Drive upload
    python -m backend.scripts.run_research_agent LOBBY-1 --contact sample_lobby --no-drive

Run from backend/ with pytest-style path instead:
    cd backend && python scripts/run_research_agent.py PW-3
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Allow running from repo root as `python -m backend.scripts.run_research_agent`
# or from backend/ as `python scripts/run_research_agent.py`
_HERE = Path(__file__).resolve()
_BACKEND = _HERE.parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from dotenv import load_dotenv
load_dotenv(_BACKEND.parent / ".env")

from agents.research_agent import ResearchAgent
from services.research_agent.schema import ResearchBrief


# ---------------------------------------------------------------------------
# Sample contacts for local testing
# In production these come from GHL contact records.
# ---------------------------------------------------------------------------

SAMPLE_CONTACTS: dict[str, dict] = {
    # ---- S6-1 — Grant Opportunity Research ----
    "sample_seagrass": {
        "contact_id": "ghl_test_001",
        "municipality_name": "Sarasota",
        "county": "Sarasota",
        "project_type": "seagrass_restoration",
        "estimated_cost_usd": 4_400_000,
        "project_overview": (
            "Restore approximately 18 acres of historical seagrass habitat in "
            "Sarasota Bay using nature-based shoreline stabilization and water "
            "quality improvements at three identified problem zones."
        ),
        "p3_intent": "yes",
        "timeline": "Q1 2027",
    },
    "sample_stormwater": {
        "contact_id": "ghl_test_002",
        "municipality_name": "Bradenton",
        "county": "Manatee",
        "project_type": "stormwater_retrofit",
        "estimated_cost_usd": 12_500_000,
        "project_overview": (
            "Convert three sub-basins from grey to green stormwater "
            "infrastructure to reduce nitrogen loading into Tampa Bay "
            "and address recurring flood complaints."
        ),
        "p3_intent": "exploring",
    },

    # ---- PW-3 — Municipality Background Research ----
    "sample_pw3": {
        "contact_id": "ghl_test_003",
        "municipality_name": "Gainesville",
        "county": "Alachua",
        "state": "FL",
        "contact_name": "Alex Rivera",
        "contact_title": "Public Works Director",
    },
    "sample_pw3_b": {
        "contact_id": "ghl_test_004",
        "municipality_name": "Lakeland",
        "county": "Polk",
        "state": "FL",
    },

    # ---- LOBBY-1 — Lobbyist Registration ----
    "sample_lobby": {
        "contact_id": "ghl_test_005",
        "municipality_name": "Alachua County",
        "jurisdiction_name": "Alachua County",
        "jurisdiction_type": "county",
    },
    "sample_lobby_city": {
        "contact_id": "ghl_test_006",
        "municipality_name": "Gainesville",
        "jurisdiction_name": "City of Gainesville",
        "jurisdiction_type": "city",
    },

    # ---- PW-1 — Conference Attendee Research ----
    "fsbpa_2026": {
        "contact_id": "ghl_test_conference_fsbpa_2026",
        "municipality_name": None,
        "conference_name": "FSBPA 2026 Annual Conference",
        "conference_date": "2026-09-15",
        "location": "Florida",
    },

    # ---- S1-4 — Full Internet Research on a Contact ----
    "sample_s14": {
        "contact_id": "ghl_test_003",
        "municipality_name": "Gainesville",
        "contact_name": "Alex Rivera",
        "contact_title": "Public Works Director",
        "organization": "City of Gainesville",
    },

    # ---- S1-2 — LinkedIn Research ----
    "sample_linkedin": {
        "contact_id": "ghl_test_003",
        "municipality_name": "Gainesville",
        "contact_name": "Alex Rivera",
        "contact_title": "Public Works Director",
        "organization": "City of Gainesville",
    },

    # ---- S3-PREP — Pre-Meeting Research Package ----
    "sample_intake": {
        "contact_id": "ghl_test_003",
        "municipality_name": "Gainesville",
        "county": "Alachua",
        "contact_name": "Alex Rivera",
        "contact_title": "Public Works Director",
        "meeting_date": "2026-05-26",
        "project_hint": (
            "Stormwater retrofit along Hogtown Creek; contact flagged in the "
            "booth conversation that the project has been stalled in FDEP "
            "discussions for two years."
        ),
    },

    # ---- S3-3 — Commission Meeting Preparation ----
    "sample_commission": {
        "contact_id": "ghl_test_003",
        "municipality_name": "Gainesville",
        "meeting_date": "2026-06-09",
        "meeting_goal": "observe",
        "project_status": (
            "Pre-Step-3 — contact has agreed to an intake meeting but no formal "
            "C-HAWQ proposal in front of the commission yet."
        ),
    },

    # ---- S4-DECK — Custom Deck Research Brief ----
    "sample_seagrass_deck": {
        "contact_id": "ghl_test_001",
        "municipality_name": "Sarasota",
        "project_focus": "Sarasota Bay seagrass restoration",
        "problem_areas": (
            "Sarasota Bay — three identified problem zones along the shoreline "
            "with documented seagrass loss since 2018."
        ),
        "champion_priorities": (
            "Visible early wins, P3 funding structure, demonstrating to "
            "commission that nature-based approach is cheaper than seawalls."
        ),
    },
}


# ---------------------------------------------------------------------------
# Pretty-print helper
# ---------------------------------------------------------------------------

def print_brief_summary(brief: ResearchBrief) -> None:
    print("\n" + "=" * 70)
    print(f"VALIDATED BRIEF — {brief.research_type_id} v{brief.prompt_version}")
    print("=" * 70)
    print(f"Run id:               {brief.run_id}")
    print(f"Municipality:         {brief.municipality_name}")
    print(f"Overall confidence:   {brief.overall_confidence}")
    print(f"Sources consulted:    {len(brief.sources_consulted)}")
    print(f"Cross-cutting claims: {len(brief.claims)}")

    f = brief.findings
    print(f"\nFindings ({f.research_type}):")

    if f.research_type == "S6-1":
        print(f"  Grants surfaced: {len(f.grants)}")
        for g in f.grants:
            rng = ""
            if g.typical_award_usd_min and g.typical_award_usd_max:
                rng = f"${g.typical_award_usd_min:,}–${g.typical_award_usd_max:,}"
            print(f"    - [{g.confidence:.2f}] {g.name}  ({g.administering_agency})")
            print(f"        {rng}  P3:{g.p3_compatible}  precedents:{len(g.florida_precedents)}")
        print(f"  Risks: {len(f.risks_and_disqualifiers)}")
        print(f"  P3 contractors: {len(f.p3_contractors)}")
    elif f.research_type == "PW-3":
        print(f"  Leadership: {len(f.leadership)}")
        print(f"  Active/stalled projects: {len(f.active_or_stalled_projects)}")
        print(f"  Environmental issues: {len(f.environmental_issues)}")
        print(f"  Commission members: {len(f.commission_makeup)}")
    elif f.research_type == "S3-PREP":
        print(f"  Champion: {f.champion_profile.name} ({f.champion_profile.role})")
        print(f"  Discovery questions: {len(f.tailored_discovery_questions)}")
    elif f.research_type == "S8-1":
        print(f"  Commissioners profiled: {len(f.commissioner_profiles)}")
        print(f"  Action items: {len(f.three_week_action_plan)}")
        print(f"  Top derailers: {len(f.top_derailers_and_mitigations)}")
    elif f.research_type == "LOBBY-1":
        print(f"  Jurisdiction: {f.jurisdiction_name} ({f.jurisdiction_type})")
        print(f"  Registration required: {f.registration_required}")
        print(f"  Timing: {f.timing_requirement}")
    elif f.research_type == "PW-1":
        print(f"  Priority contacts: {len(f.top_priority_contacts)}")
    elif f.research_type == "S1-2":
        print(f"  Contact: {f.contact_name}")
        print(f"  Common ground hooks: {len(f.common_ground_hooks)}")
    elif f.research_type == "S1-4":
        print(f"  Contact: {f.contact_name}")
        print(f"  Public statements: {len(f.public_statements_on_environment)}")
    elif f.research_type == "S3-3":
        print(f"  Meeting: {f.municipality_name} on {f.meeting_date}")
        print(f"  Agenda items affecting C-HAWQ: {len(f.agenda_items_affecting_chawq)}")
    elif f.research_type == "S4-DECK":
        print(f"  Comparable projects: {len(f.comparable_project_examples)}")
        print(f"  Visual suggestions: {len(f.visual_storytelling_suggestions)}")

    if brief.notes:
        print(f"\nAgent notes: {brief.notes}")
    print("=" * 70)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Run the C-HAWQ Research Agent against a sample contact."
    )
    ap.add_argument("research_type", choices=[
        "S6-1", "PW-3", "PW-1", "LOBBY-1", "S1-2", "S1-4",
        "S3-3", "S3-PREP", "S4-DECK",
    ])
    ap.add_argument(
        "--contact", default=None,
        choices=list(SAMPLE_CONTACTS.keys()),
        help="Sample contact key (defaults to the first matching contact for the type)",
    )
    ap.add_argument("--version", type=int, default=1)
    ap.add_argument("--save", help="Save validated brief JSON to this path")
    ap.add_argument("--model", help="Override the model in the YAML")
    ap.add_argument("--no-web-search", action="store_true",
                    help="Disable web_search/web_fetch (uses training data only)")
    ap.add_argument("--drive-folder", help="Drive folder ID for upload")
    ap.add_argument("--no-drive", action="store_true", help="Skip Drive upload")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if "ANTHROPIC_API_KEY" not in os.environ:
        print("ERROR: ANTHROPIC_API_KEY not set. export ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)

    # Pick a sensible default contact for the type if none specified
    type_defaults = {
        "S6-1": "sample_seagrass",
        "PW-3": "sample_pw3",
        "PW-1": "fsbpa_2026",
        "LOBBY-1": "sample_lobby",
        "S1-2": "sample_linkedin",
        "S1-4": "sample_s14",
        "S3-3": "sample_commission",
        "S3-PREP": "sample_intake",
        "S4-DECK": "sample_seagrass_deck",
    }
    contact_key = args.contact or type_defaults.get(args.research_type, "stuart_pw3")
    contact = SAMPLE_CONTACTS[contact_key]

    agent = ResearchAgent(args.research_type, version=args.version)

    if not args.quiet:
        print(f"Research type: {args.research_type}  contact: {contact_key}")

    brief, meta = agent.run(
        contact,
        model=args.model,
        verbose=not args.quiet,
        no_web_search=args.no_web_search,
    )

    print_brief_summary(brief)

    if args.save:
        Path(args.save).write_text(brief.model_dump_json(indent=2), encoding="utf-8")
        print(f"\nSaved brief: {args.save}")

    if not args.no_drive:
        try:
            from services.research_agent.drive_sync import upload_brief, DEFAULT_FOLDER_ID
            folder_id = args.drive_folder or DEFAULT_FOLDER_ID
            print(f"\nUploading to Drive folder {folder_id}...")
            results = upload_brief(brief, folder_id=folder_id)
            for kind, file_meta in results.items():
                print(f"  {kind:9s} {file_meta['name']}")
                print(f"            {file_meta['webViewLink']}")
        except Exception as exc:
            print(f"\nWARN: Drive upload failed: {exc}")
            if args.save:
                print(f"  Brief saved locally at: {args.save}")


if __name__ == "__main__":
    main()
