"""
Drive folder management for agent outputs.

Two concerns:

1. Idempotent find-or-create of a named subfolder inside a parent folder.
   Re-runs of the same agent for the same contact never create duplicate
   folders. Drive lets two folders share a name in the same parent, so we
   match by name + parent + non-trashed and create only if absent.

2. Choosing the right folder NAME for a given run. Different research/
   presentation types carry their identity on different fields
   (municipality_name, jurisdiction_name, conference_name, contact_name).
   resolve_contact_folder_name() walks a fallback chain so every run has
   a sensible home.

Layout that emerges (with the DEFAULT_FOLDER_ID as the root):

    {root}/
      {Contact folder}/
        Research Briefs/
          pw3_*.docx
          s4_deck_*.docx
        Presentation Outlines/
          pa_curiosity_*.json
          pa_curiosity_*.docx
"""

from __future__ import annotations

import re
from typing import Any

FOLDER_MIME = "application/vnd.google-apps.folder"

# Per-process cache. Keyed by (parent_id, normalized_name) -> folder_id.
# Bounded by total contact count; safe to keep for the lifetime of the
# process (Cloud Run instance recycles every few minutes anyway).
_FOLDER_CACHE: dict[tuple[str, str], str] = {}


# ---------------------------------------------------------------------------
# Folder name normalization
# ---------------------------------------------------------------------------

_DRIVE_BANNED_CHARS = re.compile(r'[\\/]')
_COLLAPSE_WHITESPACE = re.compile(r"\s+")


def normalize_folder_name(name: str) -> str:
    """Make a string safe to use as a Drive folder name.

    Drive accepts almost anything in folder names, but path separators
    confuse downstream tools and trailing whitespace is invisible-but-
    consequential. Keep capitalization — folder names are read by humans.
    """
    cleaned = _DRIVE_BANNED_CHARS.sub(" ", name)
    cleaned = _COLLAPSE_WHITESPACE.sub(" ", cleaned).strip()
    return cleaned or "Misc"


def _escape_drive_query_literal(value: str) -> str:
    """Escape a string for safe inclusion in a Drive `q` query literal.

    Drive's query language uses single quotes around string literals;
    embedded single quotes and backslashes must be backslash-escaped.
    Failing to escape would let a contact name like "O'Hara" break the
    query — or worse, let a crafted name inject query clauses.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


# ---------------------------------------------------------------------------
# Find-or-create
# ---------------------------------------------------------------------------

def ensure_subfolder(service: Any, parent_id: str, name: str) -> str:
    """Return the Drive folder ID for `name` under `parent_id`, creating it if missing.

    Idempotent. Cached per-process. Tolerates concurrent creates by re-
    querying on conflict.
    """
    folder_name = normalize_folder_name(name)
    cache_key = (parent_id, folder_name)
    if cache_key in _FOLDER_CACHE:
        return _FOLDER_CACHE[cache_key]

    escaped_name = _escape_drive_query_literal(folder_name)
    q = (
        f"name = '{escaped_name}' "
        f"and '{parent_id}' in parents "
        f"and mimeType = '{FOLDER_MIME}' "
        f"and trashed = false"
    )
    existing = service.files().list(
        q=q,
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        pageSize=1,
    ).execute().get("files", [])

    if existing:
        folder_id = existing[0]["id"]
    else:
        created = service.files().create(
            body={
                "name": folder_name,
                "mimeType": FOLDER_MIME,
                "parents": [parent_id],
            },
            fields="id, name",
            supportsAllDrives=True,
        ).execute()
        folder_id = created["id"]

    _FOLDER_CACHE[cache_key] = folder_id
    return folder_id


def clear_folder_cache() -> None:
    """Drop the in-memory folder cache. Mostly for tests."""
    _FOLDER_CACHE.clear()


# ---------------------------------------------------------------------------
# Contact folder name resolution
#
# Different research/presentation types carry their identity on different
# fields. Walk a fallback chain so every run lands somewhere sensible.
# ---------------------------------------------------------------------------

# Order matters: most specific / human-readable first.
_CONTACT_FOLDER_FIELDS = (
    "municipality_name",
    "jurisdiction_name",
    "conference_name",
    "contact_name",
    "audience",
    "contact_id",
)


def resolve_contact_folder_name(obj: Any) -> str:
    """Pick the best folder name for a brief or outline.

    Walks the envelope first, then any nested findings object, so types
    like LOBBY-1 (jurisdiction_name lives on findings) and PW-1
    (conference_name lives on findings) still find their identifying
    field. Returns "Misc" only if nothing usable exists.
    """
    candidates: list[Any] = [obj]
    findings = getattr(obj, "findings", None)
    if findings is not None:
        candidates.append(findings)

    # Walk fields in priority order so a higher-priority field on findings
    # (e.g. conference_name) beats a lower-priority field on the envelope
    # (e.g. contact_id).
    for field in _CONTACT_FOLDER_FIELDS:
        for source in candidates:
            value = getattr(source, field, None)
            if value:
                return normalize_folder_name(str(value))

    return "Misc"
