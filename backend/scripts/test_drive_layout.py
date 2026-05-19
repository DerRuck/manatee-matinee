"""
Live end-to-end smoke test for the new Drive folder layout.

Runs three agents against Claude with web_search + web_fetch enabled,
uploads results via the new folder-aware code paths, walks the Drive
tree to confirm the layout, and optionally cleans up.

Expect ~1-3 minutes per agent and a few cents in tokens — this is the
realistic end-to-end path that production webhook runs take.

What it exercises:
  1. Research agent  (LOBBY-1)         -> Cedar Key, FL/Research Briefs/
  2. Presentation agent (PA-CURIOSITY) -> Cedar Key, FL/Presentation Outlines/
  3. Research agent  (PW-1)            -> FSBPA 2026 Annual Conference/Research Briefs/
  4. resolve_contact_folder_name fallback chain — both municipality_name
     (Cedar Key) and conference_name (FSBPA) cases land in the right folder
  5. ensure_subfolder idempotency  (re-running creates no duplicate folders)

PW-1 is C-HAWQ's highest-leverage lead-gen path — FSBPA pre-selects
coastal/water decision-makers, so every priority contact surfaced is a
Municipal Champion candidate. The PW-1 prompt itself uses FSBPA as its
canonical example.

Usage (from backend/):
    python scripts/test_drive_layout.py
    python scripts/test_drive_layout.py --cleanup     # trash test folders after
    python scripts/test_drive_layout.py --drive-folder <root_id>
    python scripts/test_drive_layout.py --skip-pw1    # save a Claude call

Requirements:
    - ANTHROPIC_API_KEY  set
    - Google ADC (gcloud auth application-default login) OR
      DRIVE_SA_EMAIL / DRIVE_SA_KEY env vars
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
from agents.research_agent import ResearchAgent
from services.drive.folders import (
    FOLDER_MIME,
    clear_folder_cache,
    ensure_subfolder,
    normalize_folder_name,
)
from services.presentation_agent.drive_sync import upload_outline
from services.research_agent.drive_sync import (
    DEFAULT_FOLDER_ID,
    _get_drive_service,
    upload_brief,
)

# Real Florida coastal town with documented water-quality concerns —
# web search will find genuine FDEP, Levy County, and ecology records,
# so the smoke test exercises the FULL agent path (not a hallucination
# path). Collision risk with a real C-HAWQ contact is near-zero, but
# --cleanup adds a confirmation prompt as a safety net.
TEST_CITY = "Cedar Key, FL"

# Real annual conference — FSBPA (Florida Shore & Beach Preservation
# Association) is the highest-yield lead-gen event for C-HAWQ. Every
# attendee is self-selected for coastal habitat or water quality. The
# PW-1 prompt itself uses FSBPA as its canonical example.
TEST_CONFERENCE = "FSBPA 2026 Annual Conference"

LOBBY_CONTACT = {
    "contact_id": "drive_layout_smoke_lobby",
    "municipality_name": TEST_CITY,
    "jurisdiction_name": "City of Cedar Key",
    "jurisdiction_type": "city",
}

PA_CONTEXT = {
    "contact_id": "drive_layout_smoke_pa",
    "municipality_name": TEST_CITY,
    "audience": "City Commission and Public Works staff",
    "champion_name": "Test Champion",
    "champion_role": "City Manager",
    "project_focus": "Living shoreline + water quality monitoring around Cedar Key",
    "meeting_date": "May 19, 2026",
    "problem_areas": (
        "Recurring red tide impacts and shoreline erosion along the Gulf-facing "
        "neighborhoods; historic clamming industry pressure on water quality."
    ),
}

# PW-1 deliberately has no municipality_name — the brief routes to the
# conference folder via the resolve_contact_folder_name fallback chain.
PW1_CONTACT = {
    "contact_id": "drive_layout_smoke_pw1",
    "municipality_name": None,
    "conference_name": TEST_CONFERENCE,
    "conference_date": "2026-09-15",
    "location": "Florida",
}


# ---------------------------------------------------------------------------
# Drive walking helpers
# ---------------------------------------------------------------------------

def _list_children(service, parent_id: str) -> list[dict]:
    return service.files().list(
        q=f"'{parent_id}' in parents and trashed = false",
        fields="files(id, name, mimeType, webViewLink)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        pageSize=100,
    ).execute().get("files", [])


def _find_subfolder(service, parent_id: str, name: str) -> dict | None:
    target = normalize_folder_name(name)
    escaped = target.replace("\\", "\\\\").replace("'", "\\'")
    res = service.files().list(
        q=(
            f"name = '{escaped}' and '{parent_id}' in parents "
            f"and mimeType = '{FOLDER_MIME}' and trashed = false"
        ),
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        pageSize=1,
    ).execute().get("files", [])
    return res[0] if res else None


def _print_tree(service, root_id: str, root_label: str, depth: int = 0) -> None:
    indent = "  " * depth
    print(f"{indent}{root_label}/")
    for child in _list_children(service, root_id):
        if child["mimeType"] == FOLDER_MIME:
            _print_tree(service, child["id"], child["name"], depth + 1)
        else:
            mark = "📄"
            print(f"{indent}  {mark} {child['name']}")
            print(f"{indent}     {child.get('webViewLink','(no link)')}")


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def _preflight_drive_auth(root_id: str) -> tuple[bool, str]:
    """Verify Drive credentials work BEFORE running expensive Claude calls.

    Hits a cheap metadata read on the root folder. Returns (ok, message).
    A 401 here usually means the local ADC token has expired — the fix
    is `gcloud auth application-default login`.
    """
    try:
        service = _get_drive_service()
        meta = service.files().get(
            fileId=root_id,
            fields="id, name",
            supportsAllDrives=True,
        ).execute()
        return True, f"OK — root folder is '{meta.get('name', root_id)}'"
    except Exception as exc:
        return False, str(exc)


def _trash_folder(service, folder_id: str) -> None:
    """Move a folder (and everything inside it) to Drive's trash.

    Using trash vs. permanent delete so a mistake is recoverable for ~30
    days. Drive cascades trashed=true to descendants implicitly when the
    parent is trashed.
    """
    service.files().update(
        fileId=folder_id,
        body={"trashed": True},
        supportsAllDrives=True,
    ).execute()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--drive-folder", default=DEFAULT_FOLDER_ID,
        help="Drive root folder ID to test inside (defaults to DEFAULT_FOLDER_ID)",
    )
    ap.add_argument(
        "--cleanup", action="store_true",
        help="After printing the layout, move the test contact folder to trash.",
    )
    ap.add_argument(
        "--skip-research", action="store_true",
        help="Skip the LOBBY-1 research run.",
    )
    ap.add_argument(
        "--skip-presentation", action="store_true",
        help="Skip the PA-CURIOSITY presentation run.",
    )
    ap.add_argument(
        "--skip-pw1", action="store_true",
        help="Skip the PW-1 conference attendee research run.",
    )
    args = ap.parse_args()

    if "ANTHROPIC_API_KEY" not in os.environ:
        print("ERROR: ANTHROPIC_API_KEY not set.")
        return 1

    root_id = args.drive_folder
    print(f"Root Drive folder: {root_id}")
    print(f"Test city:         {TEST_CITY}")
    print(f"Test conference:   {TEST_CONFERENCE}")
    print("-" * 70)

    # -----------------------------------------------------------------------
    # 0. Preflight: verify Drive auth BEFORE spending Claude tokens.
    # -----------------------------------------------------------------------
    print("\n[0/4] Preflight: checking Drive credentials...")
    ok, msg = _preflight_drive_auth(root_id)
    if not ok:
        print(f"      FAIL: Drive auth check failed.\n        {msg}")
        print(
            "\n      Likely fix:  gcloud auth application-default login"
            "\n      Or set DRIVE_SA_EMAIL / DRIVE_SA_KEY in .env."
            "\n      Aborting before spending Claude tokens."
        )
        return 5
    print(f"      {msg}")

    # -----------------------------------------------------------------------
    # 1. Research agent: LOBBY-1 (Cedar Key lobbyist registration)
    # -----------------------------------------------------------------------
    if not args.skip_research:
        _run_research_step("[1/4]", "LOBBY-1", LOBBY_CONTACT, root_id)
    else:
        print("\n[1/4] SKIPPED LOBBY-1 run")

    # -----------------------------------------------------------------------
    # 2. Presentation agent: PA-CURIOSITY (Cedar Key curiosity deck)
    # -----------------------------------------------------------------------
    if not args.skip_presentation:
        print("\n[2/4] Running PresentationAgent(PA-CURIOSITY) with web search enabled...")
        outline, meta = PresentationAgent("PA-CURIOSITY").run(
            PA_CONTEXT, verbose=True, no_web_search=False,
        )
        print(
            f"      outline.run_id={outline.run_id[:8]}  slides={len(outline.findings.slides)}  "
            f"in/out tokens={meta['input_tokens']}/{meta['output_tokens']}  "
            f"web_search={meta.get('web_searches', 0)}  web_fetch={meta.get('web_fetches', 0)}"
        )

        print("      Uploading via upload_outline()...")
        results = upload_outline(outline, folder_id=root_id)
        for kind, f in results.items():
            print(f"        {kind:5s}  {f['name']}")
            print(f"               {f.get('webViewLink','(no link)')}")
    else:
        print("\n[2/4] SKIPPED PA-CURIOSITY run")

    # -----------------------------------------------------------------------
    # 3. Research agent: PW-1 (FSBPA conference attendee research)
    #    Routes to the conference folder via the fallback chain — exercises
    #    the cross-folder behavior the Cedar Key steps don't touch.
    # -----------------------------------------------------------------------
    if not args.skip_pw1:
        _run_research_step("[3/4]", "PW-1", PW1_CONTACT, root_id)
    else:
        print("\n[3/4] SKIPPED PW-1 run")

    # -----------------------------------------------------------------------
    # 4. Walk the Drive tree to verify the layout
    # -----------------------------------------------------------------------
    print("\n[4/4] Verifying Drive layout under the root folder...")
    service = _get_drive_service()

    expected_folders: list[tuple[str, list[str]]] = []
    if not args.skip_research:
        expected_folders.append((TEST_CITY, ["Research Briefs"]))
    if not args.skip_presentation:
        # Both Cedar Key subfolders are checked together — append "Presentation
        # Outlines" to the existing Cedar Key entry if it's there, otherwise
        # create a new one.
        for i, (name, subs) in enumerate(expected_folders):
            if name == TEST_CITY:
                expected_folders[i] = (name, subs + ["Presentation Outlines"])
                break
        else:
            expected_folders.append((TEST_CITY, ["Presentation Outlines"]))
    if not args.skip_pw1:
        expected_folders.append((TEST_CONFERENCE, ["Research Briefs"]))

    found_folders: list[dict] = []
    failures: list[str] = []

    for folder_name, expected_subs in expected_folders:
        folder = _find_subfolder(service, root_id, folder_name)
        if not folder:
            failures.append(f"contact folder '{folder_name}' missing")
            continue
        print(f"\n      Found: {folder['name']}  id={folder['id']}")
        _print_tree(service, folder["id"], folder["name"])
        found_folders.append(folder)

        for sub in expected_subs:
            if _find_subfolder(service, folder["id"], sub) is None:
                failures.append(f"'{folder_name}/{sub}' missing")

    if failures:
        print(f"\n      FAIL: {failures}")
        return 3

    # Idempotency check: ensure_subfolder called twice for each contact
    # returns the same id. Drop the in-process cache so the second call
    # has to hit Drive.
    print("\n      Idempotency check (clearing cache, re-resolving each folder)...")
    clear_folder_cache()
    for folder in found_folders:
        again_id = ensure_subfolder(service, root_id, folder["name"])
        if again_id != folder["id"]:
            print(
                f"      FAIL: second ensure_subfolder for '{folder['name']}' "
                f"returned a different id ({again_id} vs {folder['id']}) — "
                f"duplicate folder created."
            )
            return 4
        print(f"      OK — '{folder['name']}' resolved to same id: {again_id}")

    print("\n      PASS: layout matches expectations.")

    # -----------------------------------------------------------------------
    # 5. Optional cleanup — confirm each folder individually before trashing.
    # -----------------------------------------------------------------------
    if args.cleanup:
        for folder in found_folders:
            print(
                f"\n--cleanup: about to move '{folder['name']}' "
                f"(id={folder['id']}) to Drive trash."
            )
            print(
                "  If C-HAWQ has a production contact for this place, this "
                "would trash real work."
            )
            reply = input(f"  Type '{folder['name']}' to confirm: ").strip()
            if reply != folder["name"]:
                print(f"  Did not match. Skipping '{folder['name']}'.")
            else:
                _trash_folder(service, folder["id"])
                print(f"  Trashed.  (recoverable from Drive trash for ~30 days)")

    return 0


def _run_research_step(label: str, research_type: str, contact: dict, root_id: str) -> None:
    """Run + upload a research agent, with consistent logging across LOBBY-1 and PW-1."""
    print(f"\n{label} Running ResearchAgent({research_type}) with web search enabled...")
    brief, meta = ResearchAgent(research_type).run(
        contact, verbose=True, no_web_search=False,
    )
    print(
        f"      brief.run_id={brief.run_id[:8]}  "
        f"in/out tokens={meta['input_tokens']}/{meta['output_tokens']}  "
        f"web_search={meta.get('web_searches', 0)}  web_fetch={meta.get('web_fetches', 0)}"
    )
    print("      Uploading via upload_brief()...")
    results = upload_brief(brief, folder_id=root_id)
    for kind, f in results.items():
        print(f"        {kind:5s}  {f['name']}")
        print(f"               {f.get('webViewLink','(no link)')}")


if __name__ == "__main__":
    sys.exit(main())
