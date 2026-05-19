"""
CLI for the Presentation Agent.

Run from the repo root:

    # PA-CURIOSITY deck for the default sample context
    python -m backend.scripts.run_presentation_agent PA-CURIOSITY

    # Override the context with a specific sample
    python -m backend.scripts.run_presentation_agent PA-CURIOSITY --context rookery_bay

    # Save the outline as JSON
    python -m backend.scripts.run_presentation_agent PA-CURIOSITY --save outline.json

    # Skip web search (faster, training-data only — useful for smoke tests)
    python -m backend.scripts.run_presentation_agent PA-CURIOSITY --no-web-search

Run from backend/ as a plain script:
    cd backend && python scripts/run_presentation_agent.py PA-CURIOSITY
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

from agents.presentation_agent import PresentationAgent
from services.presentation_agent.schema import PresentationOutline


# ---------------------------------------------------------------------------
# Sample contexts for local testing
# In production these come from GHL workflow payloads + upstream brief lookups.
# ---------------------------------------------------------------------------

SAMPLE_CONTEXTS: dict[str, dict] = {
    "rookery_bay": {
        "contact_id": "ghl_test_rookery_001",
        "municipality_name": "Rookery Bay National Estuarine Research Reserve",
        "audience": "Reserve Manager, field research staff, and education team",
        "champion_name": "Dr. Sarah Chen",
        "champion_role": "Reserve Manager",
        "project_focus": "Mapping and restoring tidal creek connectivity in Rookery Bay",
        "meeting_date": "May 26, 2026",
        "problem_areas": (
            "Henderson Creek tidal flow restriction near the US-41 culverts; "
            "documented mangrove dieback in three sub-basins since 2019."
        ),
        "champion_priorities": (
            "Demonstrate to staff that C-HAWQ brings funding and academic "
            "partnerships without taking control of the reserve's research agenda."
        ),
    },
    "sarasota_seagrass": {
        "contact_id": "ghl_test_001",
        "municipality_name": "City of Sarasota",
        "audience": "City Manager, Public Works Director, Sustainability Coordinator",
        "champion_name": "Alex Rivera",
        "champion_role": "Sustainability Coordinator",
        "project_focus": "Sarasota Bay seagrass restoration with nature-based shoreline stabilization",
        "meeting_date": "June 12, 2026",
        "problem_areas": (
            "Three shoreline zones in Sarasota Bay with documented seagrass "
            "loss since 2018; commission has dismissed prior seawall-based proposals."
        ),
        "champion_priorities": (
            "Show that nature-based approach is cheaper than seawalls and that "
            "P3 funding removes the upfront-cost objection."
        ),
    },
}


# ---------------------------------------------------------------------------
# Pretty-print helper
# ---------------------------------------------------------------------------

def print_outline_summary(outline: PresentationOutline) -> None:
    print("\n" + "=" * 70)
    print(f"VALIDATED OUTLINE — {outline.outline_type_id} v{outline.prompt_version}")
    print("=" * 70)
    print(f"Run id:               {outline.run_id}")
    print(f"Municipality / org:   {outline.municipality_name}")
    print(f"Overall confidence:   {outline.overall_confidence}")
    print(f"Upstream briefs:      {len(outline.upstream_briefs)}")

    f = outline.findings
    print(f"\nFindings ({f.outline_type}):")
    print(f"  Audience:           {f.audience}")
    print(f"  Objective:          {f.meeting_objective}")
    print(f"  Deck title:         {f.deck_title}")
    print(f"  Slide count:        {len(f.slides)}")
    print(f"  Next step ask:      {f.suggested_next_step}")

    print(f"\nSlides:")
    for s in f.slides:
        label = getattr(s, "title", None) or getattr(s, "section_title", None) \
            or getattr(s, "headline", None) or getattr(s, "deck_title", None) \
            or s.layout
        print(f"  {s.slide_number}. [{s.layout}] {label}")

    if outline.notes:
        print(f"\nAgent notes: {outline.notes}")
    print("=" * 70)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Run the C-HAWQ Presentation Agent against a sample context."
    )
    ap.add_argument("outline_type", choices=["PA-CURIOSITY"])
    ap.add_argument(
        "--context", default=None,
        choices=list(SAMPLE_CONTEXTS.keys()),
        help="Sample context key (defaults to rookery_bay)",
    )
    ap.add_argument("--version", type=int, default=1)
    ap.add_argument("--save", help="Save validated outline JSON to this path")
    ap.add_argument("--model", help="Override the model in the YAML")
    ap.add_argument("--no-web-search", action="store_true",
                    help="Disable web_search/web_fetch (uses training data only)")
    ap.add_argument("--drive-folder", help="Drive root folder ID for upload")
    ap.add_argument("--no-drive", action="store_true", help="Skip Drive upload")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if "ANTHROPIC_API_KEY" not in os.environ:
        print("ERROR: ANTHROPIC_API_KEY not set. export ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)

    context_key = args.context or "rookery_bay"
    context = SAMPLE_CONTEXTS[context_key]

    agent = PresentationAgent(args.outline_type, version=args.version)

    if not args.quiet:
        print(f"Outline type: {args.outline_type}  context: {context_key}")

    outline, meta = agent.run(
        context,
        model=args.model,
        verbose=not args.quiet,
        no_web_search=args.no_web_search,
    )

    print_outline_summary(outline)

    if args.save:
        Path(args.save).write_text(outline.model_dump_json(indent=2), encoding="utf-8")
        print(f"\nSaved outline: {args.save}")

    if not args.no_drive:
        try:
            from services.presentation_agent.drive_sync import upload_outline, DEFAULT_FOLDER_ID
            folder_id = args.drive_folder or DEFAULT_FOLDER_ID
            print(f"\nUploading to Drive folder {folder_id}...")
            results = upload_outline(outline, folder_id=folder_id)
            for kind, file_meta in results.items():
                print(f"  {kind:9s} {file_meta['name']}")
                print(f"            {file_meta['webViewLink']}")
        except Exception as exc:
            print(f"\nWARN: Drive upload failed: {exc}")
            if args.save:
                print(f"  Outline saved locally at: {args.save}")


if __name__ == "__main__":
    main()
