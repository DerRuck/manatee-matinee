"""GHL webhook → agent dispatcher.

The /webhooks/ghl handler returns 202 immediately and hands the payload off
to dispatch_ghl_payload as a FastAPI BackgroundTask. The dispatcher reads
an explicit `agent_type` field from the GHL Workflow payload and routes to
the right agent.

agent_type convention (set as a Custom Data field on each GHL Workflow):

    hello_world                    -> HelloWorldAgent (legacy default)
    research:<TYPE>                -> ResearchAgent(TYPE)         e.g. "research:LOBBY-1"
    presentation:<TYPE>            -> PresentationAgent(TYPE)     e.g. "presentation:PA-STEP4"
    scoring                        -> ScoringAgent (default PIPELINE-SCORE)
    scoring:<TYPE>                 -> ScoringAgent(TYPE)          e.g. "scoring:PIPELINE-SCORE"
    comm:ingest                    -> write the payload into the communications
                                       collection so the scoring agent can see
                                       the signal on the next run

If agent_type is missing or unknown, the dispatcher falls back to
hello_world so a misconfigured Workflow can't drop the contact entirely.

Each branch:
  - Builds a context dict from the payload
  - Runs the agent
  - Uploads the result to Drive
  - Never raises (failures are logged + swallowed so the background-task
    worker stays healthy)
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def dispatch_ghl_payload(payload: dict[str, Any]) -> None:
    """Background-task entry point. Routes payload → agent.

    The `agent_type` field is the authoritative router. Convention:
      - 'hello_world'              -> legacy hello-world flow
      - 'research:<RESEARCH_TYPE>' -> Research Agent for that type
      - 'presentation:<OUTLINE>'   -> Presentation Agent for that outline type

    Anything else logs a warning and falls back to hello_world.
    """
    agent_type = (payload.get("agent_type") or "").strip()
    contact_id = payload.get("contact_id") or payload.get("id") or "unknown"

    logger.info(
        "ghl dispatch start",
        extra={"agent_type": agent_type or "(absent)", "contact_id": contact_id},
    )

    try:
        normalized = agent_type.lower()
        if not agent_type or normalized == "hello_world":
            _run_hello_world(payload)
        elif normalized.startswith("research:"):
            _run_research(agent_type.split(":", 1)[1].strip().upper(), payload)
        elif normalized.startswith("presentation:"):
            _run_presentation(agent_type.split(":", 1)[1].strip().upper(), payload)
        elif normalized == "scoring":
            _run_scoring("PIPELINE-SCORE", payload)
        elif normalized.startswith("scoring:"):
            _run_scoring(agent_type.split(":", 1)[1].strip().upper(), payload)
        elif normalized in ("comm:ingest", "communication:ingest"):
            _ingest_communication(payload)
        else:
            logger.warning(
                "ghl dispatch unknown agent_type — falling back to hello_world",
                extra={"agent_type": agent_type, "contact_id": contact_id},
            )
            _run_hello_world(payload)
    except Exception:
        logger.exception(
            "ghl dispatch failed",
            extra={"agent_type": agent_type, "contact_id": contact_id},
        )


# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------

def _run_hello_world(payload: dict[str, Any]) -> None:
    from services.hello_world_runner import run_hello_world_for_ghl_contact
    run_hello_world_for_ghl_contact(payload)


def _run_research(research_type: str, payload: dict[str, Any]) -> None:
    from agents.research_agent import ResearchAgent
    from services.research_agent.drive_sync import upload_brief

    contact = _payload_to_context(payload)
    agent = ResearchAgent(research_type)
    brief, meta = agent.run(contact)

    upload_brief(brief)
    logger.info(
        "ghl dispatch research run completed",
        extra={
            "research_type": research_type,
            "run_id": brief.run_id,
            "contact_id": contact.get("contact_id"),
            "input_tokens": meta.get("input_tokens"),
            "output_tokens": meta.get("output_tokens"),
            "elapsed_sec": meta.get("elapsed_sec"),
        },
    )


def _run_presentation(outline_type: str, payload: dict[str, Any]) -> None:
    from agents.presentation_agent import PresentationAgent
    from services.presentation_agent.drive_sync import upload_outline

    context = _payload_to_context(payload)
    agent = PresentationAgent(outline_type)
    outline, meta = agent.run(context)

    upload_outline(outline)
    logger.info(
        "ghl dispatch presentation run completed",
        extra={
            "outline_type": outline_type,
            "run_id": outline.run_id,
            "contact_id": context.get("contact_id"),
            "input_tokens": meta.get("input_tokens"),
            "output_tokens": meta.get("output_tokens"),
            "elapsed_sec": meta.get("elapsed_sec"),
        },
    )


def _run_scoring(score_type: str, payload: dict[str, Any]) -> None:
    """Score a contact end-to-end: context build → Claude → Firestore writeback.

    Unlike research/presentation, scoring needs a contact_id — the agent's
    job is to score ONE contact. The dispatcher hard-fails (logged) when
    the payload doesn't carry one.
    """
    from agents.scoring_agent import ScoringAgent
    from services.scoring_agent.context_builder import build_scoring_context
    from services.scoring_agent.firestore_sync import persist_score

    contact_id = payload.get("contact_id") or payload.get("id")
    if not contact_id:
        logger.warning(
            "ghl dispatch scoring: no contact_id — refusing to score",
            extra={"agent_type": f"scoring:{score_type}"},
        )
        return

    triggered_by = (payload.get("triggered_by") or "webhook").strip()
    context = build_scoring_context(contact_id, triggered_by=triggered_by)

    agent = ScoringAgent(score_type)
    result, meta = agent.run(context)

    persist_score(result, meta)
    logger.info(
        "ghl dispatch scoring run completed",
        extra={
            "score_type": score_type,
            "run_id": result.run_id,
            "contact_id": result.contact_id,
            "current_step": result.findings.current_step,
            "lead_heat": result.findings.lead_heat,
            "lead_heat_score": result.findings.lead_heat_score,
            "input_tokens": meta.get("input_tokens"),
            "output_tokens": meta.get("output_tokens"),
            "elapsed_sec": meta.get("elapsed_sec"),
        },
    )


# ---------------------------------------------------------------------------
# Communications ingestion
# ---------------------------------------------------------------------------

# GHL Workflow payload field aliases — different message types arrive with
# slightly different keys. Map to the Communication schema in one place so
# the workflow operator can use either GHL's native field names or our
# canonical ones interchangeably.
_COMM_ALIASES: dict[str, tuple[str, ...]] = {
    "channel":     ("channel", "messageType", "message_type", "type"),
    "direction":   ("direction", "messageDirection"),
    "timestamp":   ("timestamp", "dateAdded", "messageDate", "date_added"),
    "subject":     ("subject", "emailSubject"),
    "body":        ("body", "message", "transcript", "text", "emailBody"),
    "author":      ("author", "fromEmail", "from", "sender"),
    "source_ref":  ("messageId", "message_id", "id", "source_ref"),
}


def _pick(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in payload and payload[k] not in (None, ""):
            return payload[k]
    return None


_GHL_TYPE_TO_CHANNEL: dict[str, str] = {
    "TYPE_EMAIL":    "email",
    "TYPE_SMS":      "sms",
    "TYPE_CALL":     "call",
    "TYPE_VOICEMAIL": "call",
    "EMAIL":         "email",
    "SMS":           "sms",
}


def _normalize_channel(raw: Any) -> str:
    if isinstance(raw, str):
        key = raw.strip().upper()
        if key in _GHL_TYPE_TO_CHANNEL:
            return _GHL_TYPE_TO_CHANNEL[key]
        lower = raw.strip().lower()
        if lower in {"email", "sms", "voice_transcript", "note", "call"}:
            return lower
    return "note"


def _normalize_direction(raw: Any) -> str:
    if isinstance(raw, str):
        lower = raw.strip().lower()
        if lower in {"inbound", "outbound", "internal"}:
            return lower
    return "inbound"


def _parse_timestamp(raw: Any) -> "datetime":
    from datetime import datetime, timezone
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(tz=timezone.utc)


def _ingest_communication(payload: dict[str, Any]) -> None:
    """Write a GHL inbound message into the communications collection.

    Returns silently when contact_id is missing — there's nothing to attach
    the comm to. Logs and moves on; the workflow's 202 already shipped.
    """
    from services.firestore.communications import (
        Communication, make_comm_id, put_communication,
    )

    contact_id = payload.get("contact_id") or payload.get("contactId") or payload.get("id")
    if not contact_id:
        logger.warning(
            "comm ingest: no contact_id — refusing to write",
            extra={"keys": sorted(payload.keys())[:10]},
        )
        return

    body = _pick(payload, _COMM_ALIASES["body"]) or ""
    if not body and not _pick(payload, _COMM_ALIASES["subject"]):
        logger.warning(
            "comm ingest: empty body + no subject — skipping",
            extra={"contact_id": contact_id},
        )
        return

    source_ref = _pick(payload, _COMM_ALIASES["source_ref"])
    comm = Communication(
        comm_id=make_comm_id("ghl", source_ref, str(body)),
        contact_id=str(contact_id),
        channel=_normalize_channel(_pick(payload, _COMM_ALIASES["channel"])),
        direction=_normalize_direction(_pick(payload, _COMM_ALIASES["direction"])),
        timestamp=_parse_timestamp(_pick(payload, _COMM_ALIASES["timestamp"])),
        subject=_pick(payload, _COMM_ALIASES["subject"]),
        body=str(body),
        source="ghl",
        source_ref=str(source_ref) if source_ref else None,
        author=_pick(payload, _COMM_ALIASES["author"]),
    )

    put_communication(comm)
    logger.info(
        "comm ingest stored",
        extra={
            "contact_id": contact_id,
            "channel": comm.channel,
            "direction": comm.direction,
            "comm_id": comm.comm_id,
        },
    )


# ---------------------------------------------------------------------------
# Payload → context
# ---------------------------------------------------------------------------

_ROUTING_KEYS = {"agent_type"}


def _hydrate_contact_from_firestore(contact_id: str) -> dict[str, Any] | None:
    """Load and flatten the GHL backfill row for `contact_id`.

    Isolated as a module-level helper so tests can patch
    services.webhook_router._hydrate_contact_from_firestore directly
    without needing the google-cloud-firestore package installed.
    """
    from services.firestore.client import get_contact
    from services.firestore.contact_context import build_context_from_contact

    raw = get_contact(contact_id)
    if raw is None:
        return None
    return build_context_from_contact(raw)


def _payload_to_context(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip routing-only keys; keep everything else for the agent's input resolver.

    GHL Workflow operators configure Custom Data fields on each Workflow with
    names matching the agent's required inputs (e.g. municipality_name,
    audience, champion_name). The runner's resolve_inputs picks the right ones.

    When the payload carries a contact_id, the dispatcher hydrates the
    context from the Firestore `contacts` collection (GHL backfill) and
    layers the webhook's per-event fields on top. That way a Workflow can
    fire with just {agent_type, contact_id, meeting_date} and the rest of
    the context is filled from the canonical contact record.
    """
    contact_id = payload.get("contact_id") or payload.get("id")
    base: dict[str, Any] = {}

    if contact_id:
        try:
            hydrated = _hydrate_contact_from_firestore(contact_id)
            if hydrated is not None:
                base = hydrated
        except Exception:
            logger.exception(
                "ghl dispatch firestore hydrate failed — continuing with payload only",
                extra={"contact_id": contact_id},
            )

    # Webhook-supplied fields win — they're the per-event context (meeting
    # date, audience, project focus) that overrides the contact-doc baseline.
    payload_fields = {k: v for k, v in payload.items() if k not in _ROUTING_KEYS}
    base.update(payload_fields)
    base.setdefault("contact_id", contact_id)
    return base
