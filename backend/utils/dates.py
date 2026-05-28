"""Date helpers shared across agents.

Centralizes the "today's date" format so every agent that injects it into
its prompt context produces the same string. Prompts that want to reason
relative to today should reference the value rather than parsing dates out
of retrieved chunks (which carry their own timestamps from when they were
authored, not today).

Format choice: ISO 8601 calendar date (``YYYY-MM-DD``). Unambiguous, sorts
lexicographically, and matches every other date string already in the
codebase (``event_time``, ``ingested_at``, Drive filenames, etc.).
"""

from __future__ import annotations

from datetime import date, datetime, timezone


def today_iso_date() -> str:
    """Return today's date in UTC as a ``YYYY-MM-DD`` string.

    UTC on purpose — Cloud Run containers run in UTC, and pinning to UTC
    keeps the value reproducible regardless of the caller's timezone.
    Florida-local dates would drift by up to 5 hours from container time
    and produce flaky agent reasoning around midnight.
    """
    return datetime.now(tz=timezone.utc).date().isoformat()


def today_iso_date_from(d: date | datetime) -> str:
    """Format an existing ``date`` or ``datetime`` as ``YYYY-MM-DD``.

    Useful in tests that want to fix "today" to a known value without
    monkeypatching ``datetime.now``.
    """
    if isinstance(d, datetime):
        d = d.date()
    return d.isoformat()
