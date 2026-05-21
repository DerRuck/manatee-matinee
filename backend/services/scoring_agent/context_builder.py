"""Assemble the scoring agent's input context from Firestore.

The scoring agent's whole job is to read EVERYTHING we know about a
contact and emit a structured assessment. build_scoring_context() is the
"everything we know" layer — one function that pulls the contact record,
all prior agent_runs for that contact, and (optionally) recent
communications, and packages them into a flat dict the prompt template
can render.

Isolated as a module-level helper so:
  - The CLI (--contact-id) hits the same code path as the webhook
    dispatcher and the daily-cron scoring sweep.
  - Tests can patch _fetch_contact / _fetch_agent_runs without needing
    the google-cloud-firestore package installed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_scoring_context(
    contact_id: str,
    triggered_by: str = "manual",
    max_agent_runs: int = 50,
    max_communications: int = 20,
) -> dict[str, Any]:
    """Aggregate everything we know about `contact_id` into a prompt context.

    Returns a flat dict matching the inputs declared in
    prompts/scoring_agent/PIPELINE-SCORE/v1.yaml:
        contact_id, contact_record, agent_runs_summary,
        recent_communications (when present), ghl_pipeline_stage,
        triggered_by, municipality_name.

    Raises ValueError when the contact doesn't exist in Firestore — the
    scoring agent has nothing to score without a record.
    """
    raw_contact = _fetch_contact(contact_id)
    if raw_contact is None:
        raise ValueError(
            f"Contact {contact_id!r} not found in Firestore. "
            "Cannot score a contact that doesn't exist."
        )

    from services.firestore.contact_context import build_context_from_contact

    contact_record = build_context_from_contact(raw_contact)

    agent_runs = _fetch_agent_runs(contact_id, limit=max_agent_runs)
    agent_runs_summary = _summarize_agent_runs(agent_runs)

    communications = _fetch_communications(contact_id, limit=max_communications)
    days_since = _days_since_last_signal(communications, agent_runs)

    return {
        "contact_id": contact_id,
        "municipality_name": contact_record.get("municipality_name"),
        "contact_record": contact_record,
        "agent_runs_summary": agent_runs_summary,
        "recent_communications": communications or None,
        "ghl_pipeline_stage": raw_contact.get("ghl_pipeline_stage"),
        "triggered_by": triggered_by,
        "days_since_last_signal": days_since,
    }


# ---------------------------------------------------------------------------
# Firestore fetches — small wrappers so tests can patch
# ---------------------------------------------------------------------------

def _fetch_contact(contact_id: str) -> dict[str, Any] | None:
    from services.firestore.client import get_contact
    return get_contact(contact_id)


def _fetch_agent_runs(contact_id: str, limit: int) -> list[dict[str, Any]]:
    """Pull every agent_runs row for this contact, newest first.

    Falls back to an empty list if the Firestore call fails — a scoring
    agent that can score with no run history is still useful for brand
    new Step 1 contacts.
    """
    try:
        from services.firestore.client import _get_client
        from core.settings import get_settings

        client = _get_client()
        settings = get_settings()
        query = (
            client.collection(settings.firestore_agent_runs_collection)
            .where("contact_id", "==", contact_id)
            .order_by("finished_at", direction="DESCENDING")
            .limit(limit)
        )
        return [snap.to_dict() for snap in query.stream()]
    except Exception:
        logger.exception(
            "scoring context: agent_runs fetch failed",
            extra={"contact_id": contact_id},
        )
        return []


def _fetch_communications(contact_id: str, limit: int) -> list[dict[str, Any]]:
    """Recent inbound + outbound communications.

    Sprint scope: returns []. The team hasn't ingested email threads
    into Firestore yet (per transcript 2026-05-22 — "we're getting that
    scheduled a bit more"). This function exists so the wire-up is in
    place — when the email_threads collection lands, only this one body
    changes.
    """
    return []


# ---------------------------------------------------------------------------
# Summarization
# ---------------------------------------------------------------------------

# Map agent names to the step(s) they belong to in the Proven Process so
# the prompt can quickly see "this contact has S3-PREP + S4-LETTER done".
_AGENT_TO_STEP: dict[str, int] = {
    "S1-2": 1, "S1-4": 1,
    "LOBBY-1": 1, "PW-1": 1, "PW-3": 2,
    "S3-PREP": 3, "S3-3": 3,
    "S4-DECK": 4, "S4-LETTER": 4,
    "PA-STEP4": 4,
    "S5-1": 5, "S5-2": 5,
    "PA-CURIOSITY": 5,
    "S6-1": 6, "S6-2": 6, "S6-3": 6,
    "S7-PLAN": 7, "S7-1": 7,
    "S8-1": 8, "S8-2": 8, "S8-3": 8,
    "S9-1": 9, "S9-2": 9, "S9-3": 9, "S9-4": 9, "S9-5": 9,
    "PA-KICKOFF": 9,
    "S10-1": 10, "S10-2": 10,
}


def _summarize_agent_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reduce verbose agent_run docs to compact summaries the prompt can ingest.

    Each summary entry shape:
      {run_id, agent_type, proven_process_step, finished_at, key_finding}
    """
    out: list[dict[str, Any]] = []
    for r in runs:
        agent_id = (
            r.get("research_type_id")
            or r.get("outline_type_id")
            or r.get("score_type_id")
            or r.get("agent")
            or "unknown"
        )
        step = _AGENT_TO_STEP.get(agent_id)
        key_finding = _extract_key_finding(r)
        out.append({
            "run_id": r.get("run_id"),
            "agent_type": agent_id,
            "proven_process_step": step,
            "finished_at": _format_dt(r.get("finished_at") or r.get("generated_at")),
            "key_finding": key_finding,
            "model": r.get("model"),
            "status": r.get("status"),
        })
    return out


def _extract_key_finding(run: dict[str, Any]) -> str | None:
    """Pull the single most-useful field from a run for the scorer to see.

    Different agent types surface different headlines:
      - research_agent: brief.findings.summary_one_line / overall_confidence
      - presentation_agent: outline.findings.suggested_next_step
      - hello_world: content_preview
    """
    findings = run.get("findings") or {}
    if isinstance(findings, dict):
        for key in (
            "summary_one_line",
            "executive_summary",
            "suggested_next_step",
            "recommended_next_action",
            "overall_finding",
        ):
            value = findings.get(key)
            if value:
                return str(value)[:300]
    preview = run.get("content_preview")
    if preview:
        return str(preview)[:300]
    return None


def _format_dt(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _days_since_last_signal(
    communications: list[dict[str, Any]],
    agent_runs: list[dict[str, Any]],
) -> int | None:
    """Newest of (recent_communications, agent_runs[finished_at])."""
    now = datetime.now(tz=timezone.utc)
    candidates: list[datetime] = []

    for c in communications:
        ts = c.get("timestamp") or c.get("sent_at")
        if isinstance(ts, datetime):
            candidates.append(_to_utc(ts))

    for r in agent_runs:
        ts = r.get("finished_at") or r.get("generated_at")
        if isinstance(ts, datetime):
            candidates.append(_to_utc(ts))

    if not candidates:
        return None
    delta = now - max(candidates)
    return max(0, delta.days)


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
