"""
Iflytek filename parsing + quartet grouping.

The iflytek scraper dumps four files per recording into the flat
`Iflytek Files/` folder on Drive:

    <basename>.opus            — audio
    <basename>_Transcript.txt  — transcript
    <basename>.pdf             — notes/metadata sheet (image-only)
    <basename> Summary         — Google Doc auto-summary (no extension)

The PM-documented naming scheme for `<basename>` is:

    YYYY-MM-DD_Client{-optionalProject}_MeetingType_{optionalTopic}

…where `MeetingType` is the slot Stage info lives in when the convention
is followed (e.g., `Stage6`, `Stage3`). Real-world filenames often
deviate — email-automation uploads can be timestamp-only autogen names,
and manual uploads sometimes use spaces or dashes where the convention
calls for underscores. This module is the tolerant parser that powers
`scripts/route_iflytek.py`.

Match strategy (filename only — PM owns the convention):
  1. Strip suffix to find `<basename>`.
  2. Group sibling files by `<basename>`.
  3. Extract:
       - date prefix `YYYY-MM-DD` if present
       - stage number from `Stage\\s*\\d+` regex (case-insensitive)
       - client/lead keyword from the rest (caller decides which lead
         folder it maps to — this module just returns candidate tokens)
  4. Caller routes the group; unmatched groups go to `_needs_routing/`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# Suffix → "kind" mapping. Order matters because some suffixes overlap
# (e.g. `_Transcript.txt` strictly extends `.txt`). The basename is
# whatever's left after stripping the longest matching suffix.
_SUFFIX_KINDS: list[tuple[str, str]] = [
    ("_transcript.txt", "transcript"),  # iflytek transcript
    (" summary", "summary"),            # Google Doc auto-summary (no ext)
    (".opus", "audio"),                 # source audio
    (".pdf", "notes"),                  # notes/metadata sheet
]


# Stage number regex — matches "Stage 3", "Stage3", "STAGE  6", "stage_4"
_STAGE_RE = re.compile(r"\bstage[\s_-]*(\d+)\b", re.IGNORECASE)

# Date prefix regex — leading YYYY-MM-DD followed by an underscore or space.
# Convention says underscore; email-automation often has both date prefixes
# concatenated (e.g. "2026-05-14_05_12_2026 10_00_42") — we keep the first.
_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[_\s]+(.*)$")


@dataclass(frozen=True)
class ParsedIflytek:
    """
    Structured view of one iflytek basename.

    `client_remainder` is the basename text with the date prefix and the
    stage substring stripped out — what's left is the caller's best
    chance at picking a lead. Callers should lower-case + tokenize this
    when matching against the lead-folder index.
    """

    basename: str
    date_prefix: Optional[str]          # "YYYY-MM-DD" or None
    stage_number: Optional[int]         # 3, 4, 5, 6, … or None
    client_remainder: str               # basename minus date + stage


@dataclass
class IflytekGroup:
    """
    A quartet (or partial quartet) of iflytek files that share a basename.
    Each `files` entry is the Drive API metadata dict (`id`, `name`,
    `mimeType`, `modifiedTime`, `parents`, etc.) keyed by `kind`
    (`audio` / `transcript` / `notes` / `summary`).

    Missing kinds are allowed — legacy recordings often don't have a
    summary Doc, and partial uploads happen.
    """

    basename: str
    parsed: ParsedIflytek
    files: dict[str, dict] = field(default_factory=dict)

    @property
    def file_ids(self) -> list[str]:
        return [meta["id"] for meta in self.files.values()]

    @property
    def newest_modified_time(self) -> str:
        """ISO timestamp of the most-recently-modified file in the group."""
        return max((meta.get("modifiedTime", "") for meta in self.files.values()), default="")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def strip_suffix(name: str) -> tuple[str, Optional[str]]:
    """
    Return `(basename, kind)` for a single filename. `kind` is one of
    `transcript | audio | notes | summary` if any known suffix matches,
    None otherwise.

    Suffix matching is case-insensitive. The basename keeps its original
    casing so log output reads naturally.
    """
    lower = name.lower()
    for suffix, kind in _SUFFIX_KINDS:
        if lower.endswith(suffix):
            return name[: -len(suffix)], kind
    return name, None


def parse_basename(basename: str) -> ParsedIflytek:
    """
    Pull date prefix + stage number out of a basename. Whatever's left
    becomes `client_remainder` for the caller's lead-folder match.
    """
    date_prefix: Optional[str] = None
    remainder = basename

    m = _DATE_PREFIX_RE.match(basename)
    if m:
        date_prefix = m.group(1)
        remainder = m.group(2)

    stage_number: Optional[int] = None
    m_stage = _STAGE_RE.search(remainder)
    if m_stage:
        stage_number = int(m_stage.group(1))
        # Carve the stage substring out so it doesn't pollute the client
        # remainder (otherwise "Stage6" or "STAGE 6" can look like a token).
        remainder = (remainder[: m_stage.start()] + remainder[m_stage.end():]).strip()

    # Tidy: collapse internal whitespace, strip dangling separators.
    remainder = re.sub(r"[\s_-]+", " ", remainder).strip(" _-")

    return ParsedIflytek(
        basename=basename,
        date_prefix=date_prefix,
        stage_number=stage_number,
        client_remainder=remainder,
    )


def group_iflytek_files(files: list[dict]) -> list[IflytekGroup]:
    """
    Walk Drive metadata for the Iflytek Files folder and return one
    `IflytekGroup` per recognized basename.

    `files` is the list returned by `drive.client.list_folder_files()`
    for the iflytek folder — each entry must have at least `id`, `name`,
    and `mimeType`.

    Files whose names don't match a known suffix are skipped (they're
    almost always the `iflytek export paths` Google Doc, a `_needs_routing`
    subfolder, or junk). Subfolders are always skipped.

    The returned list is sorted by basename for stable iteration.
    """
    from services.drive.client import FOLDER_MIME  # local to avoid cycle at import time

    groups: dict[str, IflytekGroup] = {}
    for meta in files:
        if meta.get("mimeType") == FOLDER_MIME:
            continue
        name = meta.get("name", "")
        if not name:
            continue

        basename, kind = strip_suffix(name)
        if kind is None:
            continue

        group = groups.get(basename)
        if group is None:
            group = IflytekGroup(basename=basename, parsed=parse_basename(basename))
            groups[basename] = group

        # If two files claim the same kind (e.g., two `_Transcript.txt`
        # uploads of the same basename) keep the newer one — iflytek
        # sometimes re-uploads on retry.
        existing = group.files.get(kind)
        if existing is None or meta.get("modifiedTime", "") > existing.get("modifiedTime", ""):
            group.files[kind] = meta

    return sorted(groups.values(), key=lambda g: g.basename)


def keyword_candidates(client_remainder: str) -> list[str]:
    """
    Tokenize the client remainder into match candidates the caller can
    look up against a lead-folder index.

    Yields (in order, deduped):
      - the full remainder, lowercased
      - the remainder with whitespace/underscores stripped (lowercased)
      - each individual token lowercased
      - adjacent pairs lowercased (catches "PALM BEACH", "BREVARD COUNTY")

    The caller decides which match wins (exact > substring > slug
    fallback). Returning candidates rather than a single guess keeps the
    matching policy in one place — the caller — instead of split across
    parser + matcher.
    """
    if not client_remainder:
        return []

    lower = client_remainder.lower().strip()
    candidates: list[str] = []
    seen: set[str] = set()

    def add(c: str) -> None:
        c = c.strip()
        if c and c not in seen:
            candidates.append(c)
            seen.add(c)

    add(lower)
    add(re.sub(r"[\s_]+", "", lower))  # "boynton beach" -> "boyntonbeach"

    tokens = [t for t in re.split(r"[\s_\-]+", lower) if t]
    for tok in tokens:
        add(tok)

    for i in range(len(tokens) - 1):
        add(f"{tokens[i]} {tokens[i + 1]}")

    return candidates
