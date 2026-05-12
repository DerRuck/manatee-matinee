"""
Canonical municipality slug normalization.

The slug is the join key between ingest, retrieval, and runtime — chunks
store municipality as e.g. `['rookery_bay_fl']`, agents retrieve by that
exact string, and GHL contacts carry the human-readable city ("Rookery
Bay"). This module turns the latter into the former, deterministically,
so both sides agree.

Used by:
  - scripts/ingest_demo_corpus.py (resolvers stamp slugs on chunks at ingest)
  - services/email_drafter_runner.py (translates GHL payloads at dispatch)

Extending the slug map: add a `<lowercase keyword>: <canonical slug>`
entry. Keep slugs in `<city>_<state>` shape so they sort by state and
read like place names.
"""
from __future__ import annotations

import re


KNOWN_MUNICIPALITY_SLUGS: dict[str, str] = {
    "rookery bay": "rookery_bay_fl",
    "sfwmd": "sfwmd_fl",
    "boynton beach": "boynton_beach_fl",
    "st.petersburg": "st_petersburg_fl",
    "st petersburg": "st_petersburg_fl",
    "naples": "naples_fl",
    "marco island": "marco_island_fl",
}


def slugify(name: str) -> str:
    """Lowercase, spaces/dashes -> underscores, drop everything else non-alnum."""
    s = name.strip().lower()
    s = re.sub(r"[\s\-]+", "_", s)
    s = re.sub(r"[^a-z0-9_]", "", s)
    return s.strip("_")


def slug_for_municipality(name: str) -> str:
    """
    Best-effort canonical slug for a single municipality name.

    Returns the canonical slug if `name` matches a known entry;
    otherwise slugifies and appends `_fl` (V1 pilot is Florida-only).
    """
    norm = name.strip().lower()
    if norm in KNOWN_MUNICIPALITY_SLUGS:
        return KNOWN_MUNICIPALITY_SLUGS[norm]
    return f"{slugify(name)}_fl"


def scan_known_municipalities(tokens: list[str]) -> list[str]:
    """
    Scan a list of strings (path segments + filename + any other tokens)
    for known municipality keywords. Returns canonical slugs in
    deterministic order.

    Only matches on the KNOWN map — no slugify fallback here. We don't
    want a stray word in a filename to invent a brand-new municipality
    slug.
    """
    found: list[str] = []
    seen: set[str] = set()
    haystack = " ".join(tokens).lower()
    for keyword, slug in KNOWN_MUNICIPALITY_SLUGS.items():
        if keyword in haystack and slug not in seen:
            found.append(slug)
            seen.add(slug)
    return found
