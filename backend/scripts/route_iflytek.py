"""
Iflytek → Leads + Conferences sweeper.

Moves iflytek recording quartets (.opus + _Transcript.txt + .pdf +
Google Doc summary) out of the flat `Iflytek Files/` dump on Drive and
into either:

  - a lead's `Stage <N> - <suffix>` subfolder under the Leads tree, or
  - a date-prefixed conference folder under the Events & Conferences
    tree (e.g. `2026-04 FWRC`).

Lead match wins precedence — a meeting WITH a lead AT a conference
(e.g. "Meetup at UF Water Symposium with SFWMD") routes to the SFWMD
lead. Conference routing is the fallback for files that don't name a
lead but do name a conference. Anything matching neither lands in
`Iflytek Files/_needs_routing/`.

Filename-only routing — PM owns the naming convention documented in the
`iflytek export paths` Google Doc:

    YYYY-MM-DD_Client{-optionalProject}_MeetingType_{optionalTopic}

The sweeper:

  1. Builds a lead-folder index by scanning the Leads root once at
     startup (dynamic — no hardcoded slug → folder map to maintain).
  2. Groups iflytek files by basename via `utils.iflytek.group_iflytek_files`.
  3. For each group, parses the basename for date + stage number +
     client remainder, then matches the remainder against the lead
     index.
  4. Finds (or creates) `Stage <N> - <lead folder name>` inside the
     matched lead folder.
  5. Moves the whole group there via `drive.client.move_file`.
  6. Groups that don't match a lead → `Iflytek Files/_needs_routing/`
     with a one-line `triage.log` append, so PM has a single inbox to
     resolve.

Usage from backend/:

    # Dry-run — print the plan, touch nothing.
    python -m scripts.route_iflytek --dry-run

    # Backfill the existing flat dump.
    python -m scripts.route_iflytek

    # Override folder IDs (defaults pulled from settings).
    python -m scripts.route_iflytek \
        --iflytek-folder-id 16LA9eTqIL4mR-ZAKeM2y74VlyembMq6F \
        --leads-root-id 1q4vytPoZcmyJX_djAlXkWEbXxuShWelG

Prereqs:
  - .env: `DRIVE_IFLYTEK_FOLDER_ID`, `DRIVE_LEADS_ROOT_FOLDER_ID`.
  - Local IAM: `roles/iam.serviceAccountTokenCreator` on the runtime SA
    for the active user (the same DWD path the agent runners use).

Cadence:
  - V1: run on-demand for the existing backfill.
  - Future: schedule via Cloud Scheduler → Cloud Run job, or trigger
    reactively from a Drive watch on the Iflytek Files folder.

# TODO(ingest-walker): After files move to lead-folder Stage subfolders,
# `ingestion/resolvers.py::resolve_iflytek` (which only walks the flat
# Iflytek Files folder) finds nothing on subsequent runs. The leads
# resolver in the same module treats `Stage <N> - X` subfolders as
# "other" today. Follow-up: teach `resolve_leads` to recognize stage
# subfolders as document_type=meeting_notes and walk them on every
# ingest run (or add a new `resolve_lead_meetings` source pointed at
# the Leads root).
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Allow running as a script without -m gymnastics.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from core.settings import get_settings  # noqa: E402
from services.drive.client import (  # noqa: E402
    FOLDER_MIME,
    find_or_create_folder,
    list_folder_files,
    list_subfolders,
    move_file,
    upload_text_file,
)
from utils.iflytek import (  # noqa: E402
    IflytekGroup,
    group_iflytek_files,
    keyword_candidates,
)
from utils.municipality import slug_for_municipality, slugify  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("route_iflytek")


NEEDS_ROUTING_FOLDER_NAME = "_needs_routing"
TRIAGE_LOG_FILENAME = "triage.log"

# Files modified within this window are assumed to still be in flight
# from the iflytek scraper — let them settle before we move them.
SETTLE_WINDOW_SECONDS = 600  # 10 minutes


# ---------------------------------------------------------------------------
# Lead folder index
# ---------------------------------------------------------------------------

@dataclass
class LeadFolder:
    """One lead folder under the Leads root, with match-keyword aliases."""

    folder_id: str
    name: str
    keywords: set[str] = field(default_factory=set)

    def matches(self, candidates: list[str]) -> Optional[str]:
        """Return the matched keyword if any candidate hits this lead."""
        for candidate in candidates:
            if candidate in self.keywords:
                return candidate
        return None


@dataclass
class ConferenceFolder:
    """
    One date-prefixed conference folder under the Events & Conferences
    root. Same keyword-set matching shape as LeadFolder, but routing to
    a conference drops at the folder root — no stage equivalent.
    """

    folder_id: str
    name: str
    keywords: set[str] = field(default_factory=set)

    def matches(self, candidates: list[str]) -> Optional[str]:
        for candidate in candidates:
            if candidate in self.keywords:
                return candidate
        return None


# Folder names that count as routable conferences must look like
# `YYYY-MM ...`, `YYYY-MM-DD ...`, `YY-MM-DD ...`. Operational folders
# (`Abstracts & Bios`, `Conference Intelligence Bot`, etc.) don't match
# and stay out of the index.
_CONFERENCE_NAME_DATE_PREFIX_RE = re.compile(r"^\d{2,4}-\d{1,2}(-\d{1,2})?[\s_-]+")


def build_lead_index(leads_root_id: str) -> list[LeadFolder]:
    """
    Scan the Leads root once and build per-folder keyword aliases.

    Aliases per lead folder:
      - full lowercased folder name
      - lowercased name with whitespace stripped
      - each token from the folder name (split on whitespace + punctuation)
      - the slugified municipality form via `slug_for_municipality`

    Index uses substring tokens, NOT slug equality, because lead folder
    names are human-readable ("Rookery Bay | National Estuarine Research
    Reserve") while filenames carry short keywords ("RookeryBay",
    "rookery_bay"). Substring + slug fallback catches both shapes.
    """
    leads: list[LeadFolder] = []
    for child in list_subfolders(leads_root_id):
        name = child.get("name", "")
        if not name or name.startswith("_"):
            continue

        kws: set[str] = set()
        lower = name.lower().strip()
        kws.add(lower)
        kws.add(re.sub(r"[\s_]+", "", lower))

        # Individual tokens — split on whitespace + common punctuation.
        for tok in re.split(r"[\s_\-|/(),.]+", lower):
            if tok and len(tok) >= 3:  # skip stop-words like "of", "a"
                kws.add(tok)

        # Slug form — "Rookery Bay | NERR" → "rookery_bay_nerr"
        slug = slugify(name)
        if slug:
            kws.add(slug)

        # Municipality slug fallback — "Rookery Bay" → "rookery_bay_fl"
        muni_slug = slug_for_municipality(name)
        if muni_slug:
            kws.add(muni_slug)

        leads.append(LeadFolder(folder_id=child["id"], name=name, keywords=kws))

    return leads


def build_conference_index(conferences_root_id: str) -> list[ConferenceFolder]:
    """
    Scan the Events & Conferences root and index the date-prefixed
    conference folders. Operational folders (no date prefix) are
    skipped so a generic-named recording can't accidentally land in
    `Abstracts & Bios` or `Presentations`.

    Keyword aliases per conference: the name with the date prefix
    stripped (in lowercase + no-space form), individual tokens of length
    >= 3, and pair tokens — same pattern as the lead index so the
    matcher can compare the two indexes uniformly.
    """
    conferences: list[ConferenceFolder] = []
    for child in list_subfolders(conferences_root_id):
        name = child.get("name", "")
        if not name or name.startswith("_"):
            continue
        if not _CONFERENCE_NAME_DATE_PREFIX_RE.match(name):
            continue  # skip operational folders without a date prefix

        # Strip the date prefix to get the human-readable conference label.
        label = _CONFERENCE_NAME_DATE_PREFIX_RE.sub("", name, count=1)
        lower_full = name.lower().strip()
        lower_label = label.lower().strip()

        kws: set[str] = set()
        kws.add(lower_full)
        kws.add(lower_label)
        kws.add(re.sub(r"[\s_]+", "", lower_label))

        tokens = [
            tok for tok in re.split(r"[\s_\-|/(),.]+", lower_label) if tok and len(tok) >= 3
        ]
        for tok in tokens:
            kws.add(tok)
        for i in range(len(tokens) - 1):
            kws.add(f"{tokens[i]} {tokens[i + 1]}")

        conferences.append(
            ConferenceFolder(folder_id=child["id"], name=name, keywords=kws)
        )

    return conferences


def find_conference_for_group(
    group: IflytekGroup, conferences: list[ConferenceFolder]
) -> Optional[tuple[ConferenceFolder, str]]:
    """Best-effort conference match. Same shape as `find_lead_for_group`."""
    candidates = keyword_candidates(group.parsed.client_remainder)
    if not candidates:
        return None
    for conf in conferences:
        hit = conf.matches(candidates)
        if hit:
            return conf, hit
    return None


def find_lead_for_group(
    group: IflytekGroup, leads: list[LeadFolder]
) -> Optional[tuple[LeadFolder, str]]:
    """
    Best-effort match: try the parsed client remainder against every
    lead's keyword set; first hit wins. Returns (lead, matched_keyword)
    or None.

    For multi-lead overlap (rare — e.g. "SFWMD" in a filename + an SFWMD
    project subfolder inside another lead), the first lead in scan order
    wins. Use the dry-run output to spot collisions before applying.
    """
    candidates = keyword_candidates(group.parsed.client_remainder)
    if not candidates:
        return None

    for lead in leads:
        hit = lead.matches(candidates)
        if hit:
            return lead, hit
    return None


# ---------------------------------------------------------------------------
# Stage subfolder lookup
# ---------------------------------------------------------------------------

# Matches existing stage folder names regardless of suffix variation:
# "Stage 3 - Champion", "Stage 4 - SFWMD", "Stage6 - Buy In", "Stage_5",
# "STAGE  6 — Define".
_STAGE_FOLDER_RE = re.compile(r"^\s*stage[\s_-]*(\d+)\b", re.IGNORECASE)


def find_or_create_stage_folder(
    lead: LeadFolder,
    stage_number: int,
    existing_children: Optional[list[dict]] = None,
    dry_run: bool = False,
) -> Optional[str]:
    """
    Locate (or create) `Stage <N> - <lead name>` inside the lead folder.

    If a folder whose name starts with `Stage <N>` already exists (any
    suffix), use it — PM-chosen naming wins. Otherwise create one with
    the default suffix being the lead folder's own name.

    Returns the stage folder ID, or None in dry-run mode.
    """
    if existing_children is None:
        existing_children = list_subfolders(lead.folder_id)

    for child in existing_children:
        m = _STAGE_FOLDER_RE.match(child.get("name", ""))
        if m and int(m.group(1)) == stage_number:
            return child["id"]

    new_name = f"Stage {stage_number} - {lead.name}"
    if dry_run:
        logger.info(
            "would create stage folder",
            extra={"lead": lead.name, "new_folder": new_name},
        )
        return None

    return find_or_create_folder(new_name, lead.folder_id)


# ---------------------------------------------------------------------------
# Move execution
# ---------------------------------------------------------------------------

@dataclass
class SweepStats:
    groups_seen: int = 0
    groups_routed_lead: int = 0       # lead match (stage subfolder or lead root)
    groups_routed_conference: int = 0 # conference match (conference folder root)
    groups_deferred: int = 0          # too-recent (within settle window)
    groups_unmatched: int = 0
    files_moved: int = 0
    errors: int = 0

    @property
    def groups_routed(self) -> int:
        return self.groups_routed_lead + self.groups_routed_conference


def move_group_to_folder(
    group: IflytekGroup,
    dest_folder_id: str,
    iflytek_folder_id: str,
    dry_run: bool,
    stats: SweepStats,
) -> None:
    """Move every file in the group to `dest_folder_id`. Errors are logged + counted."""
    for kind, meta in group.files.items():
        file_id = meta["id"]
        if dry_run:
            logger.info(
                "would move",
                extra={
                    "kind": kind,
                    "file_id": file_id,
                    "file_name": meta.get("name", ""),
                    "dest_folder_id": dest_folder_id,
                },
            )
            stats.files_moved += 1
            continue
        try:
            move_file(
                file_id=file_id,
                new_parent_folder_id=dest_folder_id,
                old_parent_folder_id=iflytek_folder_id,
            )
            stats.files_moved += 1
        except Exception as exc:
            stats.errors += 1
            logger.exception(
                "move failed",
                extra={
                    "kind": kind,
                    "file_id": file_id,
                    "file_name": meta.get("name", ""),
                    "error": str(exc),
                },
            )


def append_triage_line(
    needs_routing_folder_id: str,
    group: IflytekGroup,
    reason: str,
    dry_run: bool,
) -> None:
    """
    Drop one row into the triage log so PM sees a flat list of files
    that need manual placement.

    Per-run write rather than append-to-existing (Drive doesn't have a
    native "append text" verb; we'd have to download + edit + upload).
    Each sweep run writes a dated triage log: `triage_<YYYY-MM-DDTHH-MM>.log`.
    """
    if dry_run:
        return
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    filename = f"triage_{timestamp}_{slugify(group.basename)[:40]}.log"
    line = (
        f"{group.basename}\n"
        f"  files: {', '.join(group.files.keys())}\n"
        f"  ids:   {', '.join(group.file_ids)}\n"
        f"  reason: {reason}\n"
        f"  parsed: date={group.parsed.date_prefix} stage={group.parsed.stage_number} "
        f"client_remainder={group.parsed.client_remainder!r}\n"
    )
    try:
        upload_text_file(
            folder_id=needs_routing_folder_id,
            filename=filename,
            content=line,
            mime_type="text/plain",
        )
    except Exception:
        logger.exception("triage log write failed", extra={"basename": group.basename})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _too_recent(meta: dict, settle_seconds: int) -> bool:
    """Is this file modified within the settle window?"""
    modified = meta.get("modifiedTime")
    if not modified:
        return False
    try:
        # Drive returns RFC 3339 / ISO-8601, e.g. "2026-05-29T19:33:36.103Z"
        mt = datetime.fromisoformat(modified.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(tz=timezone.utc) - mt).total_seconds() < settle_seconds


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--iflytek-folder-id",
        default=None,
        help="Drive folder ID of `Iflytek Files`. Defaults to settings.drive_iflytek_folder_id.",
    )
    parser.add_argument(
        "--leads-root-id",
        default=None,
        help="Drive folder ID of the Leads root. Defaults to settings.drive_leads_root_folder_id.",
    )
    parser.add_argument(
        "--conferences-root-id",
        default=None,
        help=(
            "Drive folder ID of the Events & Conferences root. Defaults to "
            "settings.drive_conferences_root_folder_id. Optional — when unset, "
            "conference routing is disabled and only lead routing runs."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan without touching Drive. No moves, no folder creation, no triage logs.",
    )
    parser.add_argument(
        "--settle-seconds",
        type=int,
        default=SETTLE_WINDOW_SECONDS,
        help=(
            "Skip files modified within this many seconds (iflytek is "
            f"still uploading the quartet). Default {SETTLE_WINDOW_SECONDS}s."
        ),
    )
    args = parser.parse_args()

    settings = get_settings()
    iflytek_id = args.iflytek_folder_id or settings.drive_iflytek_folder_id
    leads_root_id = args.leads_root_id or settings.drive_leads_root_folder_id
    conferences_root_id = (
        args.conferences_root_id or settings.drive_conferences_root_folder_id
    )

    if not iflytek_id:
        parser.error(
            "Iflytek folder ID not set — pass --iflytek-folder-id or set "
            "DRIVE_IFLYTEK_FOLDER_ID in .env."
        )
    if not leads_root_id:
        parser.error(
            "Leads root folder ID not set — pass --leads-root-id or set "
            "DRIVE_LEADS_ROOT_FOLDER_ID in .env."
        )

    logger.info(
        "sweeper starting — iflytek=%s leads=%s conferences=%s dry_run=%s",
        iflytek_id, leads_root_id, conferences_root_id or "(disabled)", args.dry_run,
    )
    started = time.monotonic()

    leads = build_lead_index(leads_root_id)
    logger.info("indexed leads: count=%d", len(leads))
    if leads:
        for lead in leads:
            sample_kws = sorted(lead.keywords)[:8]
            logger.info("  lead: %r kws=%s%s", lead.name, sample_kws,
                        " ..." if len(lead.keywords) > 8 else "")
    else:
        logger.warning(
            "leads index is EMPTY — no folder children found under leads root %s. "
            "Check DRIVE_LEADS_ROOT_FOLDER_ID + that the runtime SA has read on that folder.",
            leads_root_id,
        )

    conferences: list[ConferenceFolder] = []
    if conferences_root_id:
        conferences = build_conference_index(conferences_root_id)
        logger.info("indexed conferences: count=%d", len(conferences))
        if conferences:
            for conf in conferences:
                sample_kws = sorted(conf.keywords)[:8]
                logger.info("  conference: %r kws=%s%s", conf.name, sample_kws,
                            " ..." if len(conf.keywords) > 8 else "")
        else:
            logger.warning(
                "conferences index is EMPTY — no date-prefixed subfolders found "
                "under conferences root %s. Check the folder ID + SA access.",
                conferences_root_id,
            )

    iflytek_children = list_folder_files(iflytek_id)
    groups = group_iflytek_files(iflytek_children)
    logger.info(
        "grouped iflytek files: total_files=%d groups=%d",
        len(iflytek_children), len(groups),
    )

    stats = SweepStats()
    needs_routing_folder_id: Optional[str] = None

    # Per-lead stage-children cache so we don't re-list a lead's children
    # for every group routed to it.
    stage_children_cache: dict[str, list[dict]] = {}

    for group in groups:
        stats.groups_seen += 1

        # Skip files still in flight.
        if any(_too_recent(meta, args.settle_seconds) for meta in group.files.values()):
            logger.info(
                "deferring — basename=%r kinds=%s (modified within settle window)",
                group.basename, list(group.files.keys()),
            )
            stats.groups_deferred += 1
            continue

        match = find_lead_for_group(group, leads)
        if match is None:
            # Lead miss — try a conference match before giving up.
            conf_match = (
                find_conference_for_group(group, conferences) if conferences else None
            )
            if conf_match is not None:
                conf, conf_kw = conf_match
                logger.info(
                    "conference match — basename=%r → conference=%r (matched_kw=%r)",
                    group.basename, conf.name, conf_kw,
                )
                stats.groups_routed_conference += 1
                move_group_to_folder(
                    group, conf.folder_id, iflytek_id, args.dry_run, stats
                )
                continue

            logger.info(
                "no lead/conference match — basename=%r remainder=%r stage=%s → _needs_routing",
                group.basename, group.parsed.client_remainder, group.parsed.stage_number,
            )
            stats.groups_unmatched += 1
            if needs_routing_folder_id is None and not args.dry_run:
                needs_routing_folder_id = find_or_create_folder(
                    NEEDS_ROUTING_FOLDER_NAME, iflytek_id
                )
            if needs_routing_folder_id is not None:
                move_group_to_folder(
                    group, needs_routing_folder_id, iflytek_id, args.dry_run, stats
                )
                append_triage_line(
                    needs_routing_folder_id, group, "no_lead_match", args.dry_run
                )
            continue

        lead, matched_kw = match

        # No stage in filename — drop at the lead root for PM to slot.
        if group.parsed.stage_number is None:
            logger.info(
                "lead matched, no stage — basename=%r lead=%r matched_kw=%r → lead root",
                group.basename, lead.name, matched_kw,
            )
            stats.groups_routed_lead += 1
            move_group_to_folder(group, lead.folder_id, iflytek_id, args.dry_run, stats)
            continue

        # Normal path — find or create the stage subfolder, move there.
        if lead.folder_id not in stage_children_cache:
            stage_children_cache[lead.folder_id] = (
                list_subfolders(lead.folder_id) if not args.dry_run else []
            )
        stage_folder_id = find_or_create_stage_folder(
            lead,
            group.parsed.stage_number,
            existing_children=stage_children_cache[lead.folder_id],
            dry_run=args.dry_run,
        )
        if stage_folder_id is None and not args.dry_run:
            stats.errors += 1
            logger.warning(
                "stage folder unresolved",
                extra={"basename": group.basename, "lead": lead.name},
            )
            continue

        logger.info(
            "routing — basename=%r → lead=%r stage=%d (matched_kw=%r)",
            group.basename, lead.name, group.parsed.stage_number, matched_kw,
        )
        stats.groups_routed_lead += 1
        if stage_folder_id is not None:
            move_group_to_folder(group, stage_folder_id, iflytek_id, args.dry_run, stats)

    elapsed = time.monotonic() - started
    logger.info(
        "sweeper finished — seen=%d routed=%d (lead=%d conf=%d) deferred=%d "
        "unmatched=%d files_moved=%d errors=%d elapsed=%.1fs dry_run=%s",
        stats.groups_seen, stats.groups_routed,
        stats.groups_routed_lead, stats.groups_routed_conference,
        stats.groups_deferred, stats.groups_unmatched,
        stats.files_moved, stats.errors, elapsed, args.dry_run,
    )


if __name__ == "__main__":
    main()
