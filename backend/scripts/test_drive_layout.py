"""
Live end-to-end smoke test for the new Drive folder layout.

Runs both agents against Claude with web_search + web_fetch enabled,
uploads results via the new folder-aware code paths, walks the Drive
tree to confirm the layout, and optionally cleans up.

Expect ~1-3 minutes per agent and a few cents in tokens — this is the
realistic end-to-end path that production webhook runs take.

What it exercises:
  1. Research agent  (LOBBY-1)         -> Drive Layout Smoke Test City/Research Briefs/
  2. Presentation agent (PA-CURIOSITY) -> Drive Layout Smoke Test City/Presentation Outlines/
  3. resolve_contact_folder_name fallback chain
  4. ensure_subfolder idempotency  (re-running creates no duplicate folders)

Usage (from backend/):
    python scripts/test_drive_layout.py
    python scripts/test_drive_layout.py --cleanup     # trash the test folder after
    python scripts/test_drive_layout.py --drive-folder <root_id>

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
TEST_CONTACT_NAME = "Cedar Key, FL"

LOBBY_CONTACT = {
    "contact_id": "drive_layout_smoke",
    "municipality_name": TEST_CONTACT_NAME,
    "jurisdiction_name": "City of Cedar Key",
    "jurisdiction_type": "city",
}

PA_CONTEXT = {
    "contact_id": "drive_layout_smoke",
    "municipality_name": TEST_CONTACT_NAME,
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
        help="Skip the LOBBY-1 research run (e.g. if it's already uploaded).",
    )
    ap.add_argument(
        "--skip-presentation", action="store_true",
        help="Skip the PA-CURIOSITY presentation run.",
    )
    args = ap.parse_args()

    if "ANTHROPIC_API_KEY" not in os.environ:
        print("ERROR: ANTHROPIC_API_KEY not set.")
        return 1

    root_id = args.drive_folder
    print(f"Root Drive folder: {root_id}")
    print(f"Test contact:      {TEST_CONTACT_NAME}")
    print("-" * 70)

    # -----------------------------------------------------------------------
    # 0. Preflight: verify Drive auth BEFORE spending Claude tokens.
    # -----------------------------------------------------------------------
    print("\n[0/3] Preflight: checking Drive credentials...")
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
    # 1. Research agent: LOBBY-1 with web_search + web_fetch enabled
    # -----------------------------------------------------------------------
    if not args.skip_research:
        print("\n[1/3] Running ResearchAgent(LOBBY-1) with web search enabled...")
        brief, meta = ResearchAgent("LOBBY-1").run(
            LOBBY_CONTACT, verbose=True, no_web_search=False,
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
    else:
        print("\n[1/3] SKIPPED research run")

    # -----------------------------------------------------------------------
    # 2. Presentation agent: PA-CURIOSITY with web_search + web_fetch enabled
    # -----------------------------------------------------------------------
    if not args.skip_presentation:
        print("\n[2/3] Running PresentationAgent(PA-CURIOSITY) with web search enabled...")
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
        print("\n[2/3] SKIPPED presentation run")

    # -----------------------------------------------------------------------
    # 3. Walk the Drive tree to verify the layout
    # -----------------------------------------------------------------------
    print("\n[3/3] Verifying Drive layout under the test contact folder...")
    service = _get_drive_service()
    contact_folder = _find_subfolder(service, root_id, TEST_CONTACT_NAME)
    if not contact_folder:
        print(f"      FAIL: No contact folder named '{TEST_CONTACT_NAME}' found under root.")
        return 2

    print(f"      Found contact folder: {contact_folder['name']}  id={contact_folder['id']}")
    print()
    _print_tree(service, contact_folder["id"], contact_folder["name"])

    # Sanity checks: both expected subfolders exist
    expected_subs = []
    if not args.skip_research:
        expected_subs.append("Research Briefs")
    if not args.skip_presentation:
        expected_subs.append("Presentation Outlines")

    missing: list[str] = []
    for sub in expected_subs:
        if _find_subfolder(service, contact_folder["id"], sub) is None:
            missing.append(sub)

    if missing:
        print(f"\n      FAIL: missing subfolders: {missing}")
        return 3

    # Idempotency check: ensure_subfolder called a second time returns the
    # same id and creates nothing new. Drop the in-process cache so the
    # second call has to hit Drive.
    print("\n      Idempotency check (re-running ensure_subfolder for the same contact)...")
    clear_folder_cache()
    again_id = ensure_subfolder(service, root_id, TEST_CONTACT_NAME)
    if again_id != contact_folder["id"]:
        print(
            f"      FAIL: second ensure_subfolder returned a different id "
            f"({again_id} vs {contact_folder['id']}) — a duplicate folder was created."
        )
        return 4
    print(f"      OK — same folder id returned: {again_id}")

    print("\n      PASS: layout matches expectations.")

    # -----------------------------------------------------------------------
    # 4. Optional cleanup
    # -----------------------------------------------------------------------
    if args.cleanup:
        print(
            f"\n--cleanup: about to move '{contact_folder['name']}' "
            f"(id={contact_folder['id']}) to Drive trash."
        )
        print(
            "  This folder uses a real Florida town name. If C-HAWQ has a "
            "production contact for this place, this would trash real work."
        )
        reply = input("  Type the contact name to confirm trash: ").strip()
        if reply != contact_folder["name"]:
            print(f"  Did not match '{contact_folder['name']}'. Skipping cleanup.")
        else:
            _trash_folder(service, contact_folder["id"])
            print(f"  trashed folder id={contact_folder['id']}")
            print("  (recoverable from Drive trash for ~30 days)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
