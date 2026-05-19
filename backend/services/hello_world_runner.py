"""
Hello World agent runner — coordination layer between the GHL webhook
and the agent itself.

The webhook handler returns 202 immediately and enqueues this function
as a FastAPI BackgroundTask. It's responsible for:
  1. Building a context dict from the GHL Workflow payload.
  2. Running HelloWorldAgent.run() with that context.
  3. Logging the result (output + token usage).

Sprint demo scope: log only. Drive output and Firestore agent_runs writes
get layered into THIS function in the next two steps — keep the public
signature stable so the webhook handler doesn't need to change again.

GHL Workflow webhooks send a flat snake_case JSON payload. The exact set
of fields depends on the Workflow's "Add Custom Data" config, but common
fields we map into the agent context:
  - first_name / last_name   -> contact name
  - email                    -> fallback identity
  - city / state             -> municipality + state
  - contact_notes            -> notes (custom field, fieldKey contact.contact_notes)
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from agents.hello_world import HelloWorldAgent
from core.settings import get_settings
from services.drive.client import upload_text_file
from services.firestore.client import put_agent_run
from services.ghl.client import update_contact_sync

logger = logging.getLogger(__name__)


# Custom field ID for "Contact Notes" in the C-HAWQ GHL tenant.
# Source: ghl_smoke.py output 2026-04-21 (memory: project_chawq_ghl_tenant).
# fieldKey on the definition is `contact.contact_notes`, dataType=LARGE_TEXT.
# Re-fetch with GET /locations/{id}/customFields if anyone reports this stale.
GHL_CONTACT_NOTES_FIELD_ID = "u7nkCuvWJdcfe4mZLqjR"


def run_hello_world_for_ghl_contact(payload: dict[str, Any]) -> None:
    """
    Background task entry point. Never raises — failures are logged and swallowed
    so an agent error can't crash the webhook handler's worker thread.
    """
    settings = get_settings()
    run_id = str(uuid.uuid4())
    contact_id = payload.get("contact_id") or payload.get("id") or "unknown"
    started_at = datetime.now(tz=timezone.utc)

    try:
        context = _build_context(payload)
        logger.info(
            "hello_world runner starting",
            extra={
                "run_id": run_id,
                "contact_id": contact_id,
                "context_keys": list(context.keys()),
            },
        )

        agent = HelloWorldAgent()
        result = agent.run(
            "Write a brief summary for outreach purposes.",
            context=context,
        )

        # --- Drive output ----------------------------------------------------
        drive_file: dict[str, str] | None = None
        if settings.drive_watch_folder_id:
            try:
                filename = _build_drive_filename(payload, contact_id)
                drive_file = upload_text_file(
                    folder_id=settings.drive_watch_folder_id,
                    filename=filename,
                    content=_format_markdown(result.content, payload, context),
                )
                logger.info(
                    "hello_world runner drive upload",
                    extra={
                        "run_id": run_id,
                        "contact_id": contact_id,
                        "drive_file_id": drive_file.get("id"),
                        "drive_file_name": drive_file.get("name"),
                        "drive_file_link": drive_file.get("webViewLink"),
                    },
                )
            except Exception:
                # Don't let a Drive failure tank the rest of the run — log and
                # carry on so we still get the agent_runs record.
                logger.exception(
                    "hello_world runner drive upload failed",
                    extra={"run_id": run_id, "contact_id": contact_id},
                )
        else:
            logger.warning(
                "hello_world runner skipping drive upload — DRIVE_WATCH_FOLDER_ID not set",
                extra={"run_id": run_id, "contact_id": contact_id},
            )

        # --- Firestore agent_runs row ---------------------------------------
        finished_at = datetime.now(tz=timezone.utc)
        record = {
            "run_id": run_id,
            "agent": "hello_world",
            "agent_version": 1,
            "contact_id": contact_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": (finished_at - started_at).total_seconds(),
            "status": "completed",
            "model": result.model,
            "stop_reason": result.stop_reason,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cache_creation_tokens": result.cache_creation_tokens,
            "cache_read_tokens": result.cache_read_tokens,
            "drive_file_id": (drive_file or {}).get("id"),
            "drive_file_name": (drive_file or {}).get("name"),
            "drive_file_link": (drive_file or {}).get("webViewLink"),
            "context": context,
            "content_preview": result.content[:500],
        }
        try:
            put_agent_run(run_id, record)
            logger.info(
                "hello_world runner agent_runs written",
                extra={"run_id": run_id, "contact_id": contact_id},
            )
        except Exception:
            logger.exception(
                "hello_world runner agent_runs write failed",
                extra={"run_id": run_id, "contact_id": contact_id},
            )

        # --- GHL writeback (stretch step 9) ----------------------------------
        # Drop the Drive link + run metadata into the contact's "Contact Notes"
        if drive_file and contact_id != "unknown" and settings.ghl_pit:
            try:
                notes_value = (
                    f"Hello World agent run @ {finished_at.isoformat(timespec='seconds')}\n"
                    f"Drive: {drive_file.get('webViewLink', '(no link)')}\n"
                    f"Run ID: {run_id}\n"
                    f"Tokens (in/out): {result.input_tokens}/{result.output_tokens}"
                )
                update_contact_sync(
                    contact_id,
                    {
                        "customFields": [
                            {"id": GHL_CONTACT_NOTES_FIELD_ID, "value": notes_value},
                        ],
                    },
                )
                logger.info(
                    "hello_world runner ghl writeback",
                    extra={"run_id": run_id, "contact_id": contact_id},
                )
            except Exception:
                logger.exception(
                    "hello_world runner ghl writeback failed",
                    extra={"run_id": run_id, "contact_id": contact_id},
                )
        else:
            logger.info(
                "hello_world runner skipping ghl writeback",
                extra={
                    "run_id": run_id,
                    "contact_id": contact_id,
                    "reason": (
                        "no drive_file" if not drive_file
                        else "no contact_id" if contact_id == "unknown"
                        else "GHL_PIT not set"
                    ),
                },
            )

        logger.info(
            "hello_world runner finished",
            extra={
                "run_id": run_id,
                "contact_id": contact_id,
                "model": result.model,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cache_creation_tokens": result.cache_creation_tokens,
                "cache_read_tokens": result.cache_read_tokens,
                "stop_reason": result.stop_reason,
                "drive_file_id": (drive_file or {}).get("id"),
                "content_preview": result.content[:500],
            },
        )
    except Exception:
        logger.exception(
            "hello_world runner failed",
            extra={"run_id": run_id, "contact_id": contact_id},
        )


_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _build_drive_filename(payload: dict[str, Any], contact_id: str) -> str:
    """
    Return a filesystem-safe filename like `Naples_Test-User_2026-04-29T15-32-24Z.md`.
    Falls back to contact_id when name fields are missing.
    """
    parts: list[str] = []
    city = payload.get("city")
    if city:
        parts.append(city)
    name = " ".join(
        v for v in (payload.get("first_name"), payload.get("last_name")) if v
    ).strip()
    if name:
        parts.append(name)
    if not parts:
        parts.append(contact_id)

    timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    parts.append(timestamp)

    raw = "_".join(parts) + ".md"
    return _FILENAME_SAFE.sub("-", raw).strip("-_")


def _format_markdown(
    body: str,
    payload: dict[str, Any],
    context: dict[str, Any],
) -> str:
    """
    Wrap the agent's output in a small markdown header block so a human
    skimming the file knows which contact + run produced it.
    """
    contact_id = payload.get("contact_id") or payload.get("id") or "unknown"
    timestamp = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    header = (
        "---\n"
        f"contact_id: {contact_id}\n"
        f"municipality: {context.get('municipality')}\n"
        f"state: {context.get('state')}\n"
        f"contact: {context.get('contact')}\n"
        f"generated_at: {timestamp}\n"
        f"agent: hello_world v1\n"
        "---\n\n"
    )
    return header + body


def _build_context(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Map the flat GHL Workflow payload into the context dict the
    hello_world prompt expects (municipality, state, contact, notes).
    """
    name = " ".join(
        v for v in (payload.get("first_name"), payload.get("last_name")) if v
    ).strip()
    contact = name or payload.get("email") or "(unknown contact)"

    return {
        "municipality": payload.get("city") or payload.get("company") or "(unknown)",
        "state": payload.get("state", "(unknown)"),
        "contact": contact,
        "notes": payload.get("contact_notes", "") or payload.get("notes", ""),
    }
