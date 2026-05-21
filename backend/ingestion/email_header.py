"""
Parse the structured header block at the top of email-scraper summary files.

Each .txt the scraper writes to Drive looks like:

    Date: Wed, 20 May 2026 14:32:11 -0400
    From: Bill Smith <bill.smith@cityofnaples.gov>
    To: tyler@chawq.org
    Cc:
    Subject: RE: Marco canal study
    Message-ID: <abc123@example.com>
    Thread-ID: 19e2c3d75b6b4739
    In-Reply-To: <prev@example.com>
    References:
    Direction: inbound
    Counterparty: bill.smith@cityofnaples.gov
    Scraped-Inbox: tyler@chawq.org
    Labels: Rookery Bay, Important

    --- EXTRACTED LINKS ---
    ...

    --- EMAIL BODY ---
    ...

This module is the canonical reader. The ingestion resolver calls
parse_email_summary_header() to pull out fields the watcher needs for
Firestore document/chunk metadata.
"""
from __future__ import annotations

import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Optional

# Header lines look like "Key: value" up until the first "--- " separator.
_HEADER_LINE_RE = re.compile(r'^([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*)$')

# Sentinel that ends the header block in the scraped summary.
_HEADER_END_SENTINEL = "--- EXTRACTED LINKS ---"

# Headers we care about. Keys here match the scraper's output exactly
# (see jobs/email_scraper/main.py). Values are the dict keys we return,
# lowercased and snake-cased so call sites stay clean.
_HEADER_FIELD_MAP = {
    "Date": "date",
    "From": "from_addr",
    "To": "to_addr",
    "Cc": "cc_addr",
    "Subject": "subject",
    "Message-ID": "message_id",
    "Thread-ID": "thread_id",
    "In-Reply-To": "in_reply_to",
    "References": "references",
    "Direction": "direction",
    "Counterparty": "counterparty",
    "Scraped-Inbox": "scraped_inbox",
    "Labels": "labels_raw",
}


def parse_email_summary_header(text: str) -> dict:
    """
    Extract the structured header block from a scraper summary file.

    Returns a dict with snake-cased keys. Missing fields default to "".
    `labels` is post-processed into a list (the raw header is a
    comma-separated string like "Rookery Bay, Important" or "(none)").

    Robust to:
      - Missing fields (returns "")
      - Extra whitespace
      - Header block ending early (no body)
      - "(none)" labels sentinel
    """
    out: dict = {key: "" for key in _HEADER_FIELD_MAP.values()}
    out["labels"] = []

    for line in text.splitlines():
        stripped = line.strip()
        if stripped == _HEADER_END_SENTINEL:
            break
        if not stripped:
            continue

        m = _HEADER_LINE_RE.match(stripped)
        if not m:
            # Not a Key: value line, skip. Lines inside the header block
            # that don't match the pattern are rare but harmless to skip.
            continue

        key, value = m.group(1), m.group(2).strip()
        snake_key = _HEADER_FIELD_MAP.get(key)
        if snake_key is None:
            continue
        out[snake_key] = value

    # Post-process labels: comma-separated string -> list of label names.
    labels_raw = out.pop("labels_raw", "")
    if labels_raw and labels_raw != "(none)":
        out["labels"] = [lbl.strip() for lbl in labels_raw.split(",") if lbl.strip()]

    return out


def parse_email_date_to_datetime(date_str: str) -> Optional[datetime]:
    """
    Parse the RFC 2822 Date header from a summary into a timezone-aware
    datetime. Returns None on unparseable input.
    """
    if not date_str or date_str == "Unknown Date":
        return None
    try:
        return parsedate_to_datetime(date_str)
    except (TypeError, ValueError, IndexError):
        return None
