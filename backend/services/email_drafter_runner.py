"""
Email Drafter agent runner — orchestration layer.

Composes the four side-effects of an Email Drafter run into one safe-to-
call entry point:
  1. EmailDrafterAgent.run_for_lead()  — the model call
  2. services.gmail.create_draft()     — the Gmail draft
  3. services.drive.upload_text_file() — the Drive record (audit trail)
  4. services.firestore.put_agent_run() — the agent_runs row

Mirrors the shape of services/hello_world_runner.py so the webhook
dispatcher (D3) can BackgroundTask-add either runner without
restructuring the call site.

Failure model: the runner returns a typed result with `status` set to
`completed | partial | failed` so the caller can decide what to surface
without parsing exceptions. Internally each side-effect after the model
call is in its own try/except — a Drive write failure shouldn't lose
the Gmail draft or the agent_runs log.

GHL contact-note write is deliberately NOT done here for V1 — Sprint
2.0 decision (Gmail draft only, team isn't using GHL email UI yet).
Easy to add when V2 wants it.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from agents.email_drafter import (
    EmailDrafterAgent,
    EmailDrafterInput,
    EmailDraftResult,
)
from core.settings import get_settings
from services.drive.client import find_or_create_folder, upload_text_file
from services.firestore.client import put_agent_run
from services.gmail import create_draft, resolve_from_user
from utils.municipality import slug_for_municipality, slugify

logger = logging.getLogger(__name__)


RunStatus = Literal["completed", "partial", "failed"]


@dataclass
class EmailDrafterRunResult:
    """
    Result of one orchestrated Email Drafter run. `status` is the easy
    flag for callers; the per-step fields tell you which side-effects
    actually landed.
    """

    run_id: str
    contact_id: str
    status: RunStatus

    # The model's output. Present whenever status != "failed" before the
    # model call. None only when the agent itself blew up.
    draft: Optional[EmailDraftResult] = None

    # Gmail draft side-effect.
    gmail_draft_id: Optional[str] = None
    gmail_web_link: Optional[str] = None
    gmail_error: Optional[str] = None

    # Drive record side-effect.
    drive_file_id: Optional[str] = None
    drive_web_link: Optional[str] = None
    drive_error: Optional[str] = None

    # Top-level error message if the agent itself failed.
    error: Optional[str] = None

    started_at: Optional[str] = None
    finished_at: Optional[str] = None


def run_email_drafter_for_lead(
    input_: EmailDrafterInput,
    *,
    skip_gmail: bool = False,
    skip_drive: bool = False,
) -> EmailDrafterRunResult:
    """
    Run the Email Drafter end-to-end for one lead.

    Args:
        input_: structured agent input (lead profile + triggering event +
            retrieval-filter overrides + optional from_user override).
        skip_gmail: skip Gmail draft creation. Useful for prompt
            iteration smoke tests where you want to inspect the model
            output without creating real drafts.
        skip_drive: skip Drive record write. Useful for the same reason
            as skip_gmail.

    Returns:
        EmailDrafterRunResult — never raises. Failures are captured on
        the result object so a BackgroundTask runner can log + move on.
    """
    run_id = str(uuid.uuid4())
    started_at = datetime.now(tz=timezone.utc)

    log_extra = {
        "run_id": run_id,
        "contact_id": input_.contact_id,
        "agent": "email_drafter",
    }

    # 1. Model call.
    try:
        agent = EmailDrafterAgent(version=1)
        draft = agent.run_for_lead(input_)
    except Exception as exc:
        logger.exception("email_drafter agent failed", extra=log_extra)
        finished_at = datetime.now(tz=timezone.utc)
        result = EmailDrafterRunResult(
            run_id=run_id,
            contact_id=input_.contact_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
            started_at=started_at.isoformat(timespec="seconds"),
            finished_at=finished_at.isoformat(timespec="seconds"),
        )
        _safe_put_agent_run(result, input_)
        return result

    logger.info(
        "email_drafter model call complete",
        extra={
            **log_extra,
            "input_tokens": draft.input_tokens,
            "output_tokens": draft.output_tokens,
            "model": draft.model,
            "context_chunks": draft.context_chunk_count,
        },
    )

    gmail_draft_id: Optional[str] = None
    gmail_web_link: Optional[str] = None
    gmail_error: Optional[str] = None
    drive_file_id: Optional[str] = None
    drive_web_link: Optional[str] = None
    drive_error: Optional[str] = None

    # 2. Gmail draft creation.
    if skip_gmail:
        logger.info("skipping gmail draft creation (skip_gmail=True)", extra=log_extra)
    elif not input_.contact_email:
        gmail_error = "no contact_email — cannot create Gmail draft"
        logger.warning(gmail_error, extra=log_extra)
    else:
        try:
            from_user = resolve_from_user(input_.from_user)
            gmail_response = create_draft(
                from_user=from_user,
                to=input_.contact_email,
                subject=draft.subject,
                body=draft.body,
            )
            gmail_draft_id = gmail_response.get("id")
            gmail_web_link = gmail_response.get("web_link")
        except Exception as exc:
            gmail_error = f"{type(exc).__name__}: {exc}"
            logger.exception("gmail draft creation failed", extra=log_extra)

    # 3. Drive record write. Navigates the locked per-lead path
    # `<output_root>/<municipality>/<contact>/email-drafts/<file>` using
    # find_or_create_folder so the folder structure materializes on
    # first run for each new lead.
    if skip_drive:
        logger.info("skipping drive record write (skip_drive=True)", extra=log_extra)
    else:
        try:
            settings = get_settings()
            output_root = settings.drive_output_root_folder_id
            if not output_root:
                drive_error = (
                    "DRIVE_OUTPUT_ROOT_FOLDER_ID not set — skipping Drive record"
                )
                logger.warning(drive_error, extra=log_extra)
            else:
                drafts_folder_id = _resolve_email_drafts_folder(input_, output_root)
                drive_response = upload_text_file(
                    folder_id=drafts_folder_id,
                    filename=_build_drive_filename(started_at),
                    content=_format_drive_record(
                        run_id=run_id,
                        input_=input_,
                        draft=draft,
                        gmail_draft_id=gmail_draft_id,
                        gmail_web_link=gmail_web_link,
                        started_at=started_at,
                    ),
                    mime_type="text/markdown",
                )
                drive_file_id = drive_response.get("id")
                drive_web_link = drive_response.get("webViewLink")
        except Exception as exc:
            drive_error = f"{type(exc).__name__}: {exc}"
            logger.exception("drive record write failed", extra=log_extra)

    finished_at = datetime.now(tz=timezone.utc)

    # 4. Compute status. Model call already succeeded by here; status
    # reflects whether the optional side-effects landed.
    if not skip_gmail and gmail_error and not skip_drive and drive_error:
        status: RunStatus = "partial"  # both failed — but draft is still useful
    elif (not skip_gmail and gmail_error) or (not skip_drive and drive_error):
        status = "partial"
    else:
        status = "completed"

    result = EmailDrafterRunResult(
        run_id=run_id,
        contact_id=input_.contact_id,
        status=status,
        draft=draft,
        gmail_draft_id=gmail_draft_id,
        gmail_web_link=gmail_web_link,
        gmail_error=gmail_error,
        drive_file_id=drive_file_id,
        drive_web_link=drive_web_link,
        drive_error=drive_error,
        started_at=started_at.isoformat(timespec="seconds"),
        finished_at=finished_at.isoformat(timespec="seconds"),
    )

    # 5. agent_runs log.
    _safe_put_agent_run(result, input_)

    logger.info(
        "email_drafter run complete",
        extra={
            **log_extra,
            "status": status,
            "gmail_draft_id": gmail_draft_id,
            "drive_file_id": drive_file_id,
        },
    )
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_drive_filename(started_at: datetime) -> str:
    """
    Per-lead folder structure carries the identity context, so the
    filename is just a sortable timestamp. Pattern:
        2026-05-11T13-32-04Z_email-draft.md

    Sub-second collisions are vanishingly unlikely for a human-triggered
    agent run; second-level precision is plenty.
    """
    timestamp = started_at.strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"{timestamp}_email-draft.md"


def _build_contact_slug(input_: EmailDrafterInput) -> str:
    """
    Slug for the contact's Drive folder. Prefers a human-readable name
    slug (e.g. `jane_doe`) so the folder tree is browsable; falls back
    to the opaque contact_id when name fields are missing.
    """
    name = " ".join(
        v for v in (input_.contact_first_name, input_.contact_last_name) if v
    ).strip()
    if name:
        return slugify(name)
    return slugify(input_.contact_id) or "unknown_contact"


def _resolve_email_drafts_folder(
    input_: EmailDrafterInput, output_root: str
) -> str:
    """
    Walk (creating as needed) the locked path
    `<output_root>/<municipality>/<contact>/email-drafts/`. Returns the
    leaf folder ID for the upload call.

    If `contact_municipality` is unset we use a literal `_no_municipality`
    bucket so the file still lands somewhere predictable rather than at
    the output root. PM/dev can move stray files later.
    """
    municipality = input_.contact_municipality or "_no_municipality"
    contact = _build_contact_slug(input_)

    municipality_folder = find_or_create_folder(municipality, output_root)
    contact_folder = find_or_create_folder(contact, municipality_folder)
    drafts_folder = find_or_create_folder("email-drafts", contact_folder)
    return drafts_folder


def _format_drive_record(
    *,
    run_id: str,
    input_: EmailDrafterInput,
    draft: EmailDraftResult,
    gmail_draft_id: Optional[str],
    gmail_web_link: Optional[str],
    started_at: datetime,
) -> str:
    """
    Markdown record file for the Drive audit trail. Frontmatter carries
    structured metadata so a human or future tool can grep it; body is
    a readable rendering of the draft for human review.
    """
    contact_name = " ".join(
        v for v in (input_.contact_first_name, input_.contact_last_name) if v
    ).strip() or "(unknown)"

    frontmatter_lines = [
        "---",
        f"run_id: {run_id}",
        f"agent: email_drafter v{draft.prompt_version}",
        f"model: {draft.model}",
        f"generated_at: {started_at.isoformat(timespec='seconds')}",
        f"contact_id: {input_.contact_id}",
        f"contact: {contact_name}",
        f"organization: {input_.contact_organization}",
        f"municipality: {input_.contact_municipality or '(unset)'}",
        f"triggering_event: {input_.triggering_event}",
        f"context_chunk_count: {draft.context_chunk_count}",
        f"input_tokens: {draft.input_tokens}",
        f"output_tokens: {draft.output_tokens}",
        f"cache_creation_tokens: {draft.cache_creation_tokens}",
        f"cache_read_tokens: {draft.cache_read_tokens}",
        f"suggested_send: {draft.suggested_send}",
    ]
    if gmail_draft_id:
        frontmatter_lines.append(f"gmail_draft_id: {gmail_draft_id}")
    if gmail_web_link:
        frontmatter_lines.append(f"gmail_web_link: {gmail_web_link}")
    frontmatter_lines.append("---")

    sections = [
        "\n".join(frontmatter_lines),
        "",
        "# Subject",
        "",
        draft.subject,
        "",
        "# Body",
        "",
        draft.body,
        "",
        "# Tone notes",
        "",
        draft.tone_notes,
        "",
        "# Suggested send",
        "",
        f"**{draft.suggested_send}** — {draft.suggested_send_reason}",
        "",
    ]
    return "\n".join(sections)


def run_email_drafter_for_ghl_payload(payload: dict[str, Any]) -> None:
    """
    Background-task entry that takes a raw GHL workflow webhook payload,
    builds an EmailDrafterInput from it, and runs the orchestrator.

    Never raises — the webhook handler returned 202 before this function
    ran, so failures land in agent_runs + Cloud Logging rather than
    bubbling up. Signature matches `services.hello_world_runner` so the
    dispatcher in app/routes/webhooks.py can register either uniformly.

    Payload fields consumed (snake_case per GHL Workflow shape):

        Identity:
          contact_id | id     → contact_id
          first_name          → contact_first_name
          last_name           → contact_last_name
          job_title           → contact_title
          company             → contact_organization
          city                → contact_municipality (slug-normalized)
          email               → contact_email

        Routing:
          from_user           → from_user (Gmail mailbox override)

        Triggering event:
          triggering_event           → triggering_event
          triggering_event_date      → triggering_event_date
          triggering_event_summary   → triggering_event_summary
                                       (falls back to contact_notes)

    Missing/empty fields fall through to sensible defaults; the simmer
    prompt's content guardrail handles thin context.
    """
    try:
        input_ = _payload_to_input(payload)
        run_email_drafter_for_lead(input_)
    except Exception:
        logger.exception(
            "email_drafter dispatch failed",
            extra={
                "payload_keys": sorted(payload.keys()) if isinstance(payload, dict) else None,
                "contact_id": (payload or {}).get("contact_id") or (payload or {}).get("id"),
            },
        )


def _payload_to_input(payload: dict[str, Any]) -> EmailDrafterInput:
    """Map a GHL workflow payload (flat snake_case) into EmailDrafterInput."""
    contact_id = payload.get("contact_id") or payload.get("id") or "unknown"

    # Municipality slug: prefer `city`, fall back to `company`. Both go
    # through the shared slug normalizer so chunks and runtime payloads
    # agree on the join key.
    municipality_slug: Optional[str] = None
    if payload.get("city"):
        municipality_slug = slug_for_municipality(payload["city"])
    elif payload.get("company"):
        municipality_slug = slug_for_municipality(payload["company"])

    return EmailDrafterInput(
        contact_id=contact_id,
        contact_first_name=payload.get("first_name") or "",
        contact_last_name=payload.get("last_name") or "",
        contact_title=payload.get("job_title") or None,
        contact_organization=payload.get("company") or "(unknown organization)",
        contact_municipality=municipality_slug,
        contact_email=payload.get("email") or None,
        from_user=payload.get("from_user") or None,
        triggering_event=payload.get("triggering_event") or "(no event specified)",
        triggering_event_date=payload.get("triggering_event_date") or None,
        triggering_event_summary=(
            payload.get("triggering_event_summary")
            or payload.get("contact_notes")
            or None
        ),
    )


def _safe_put_agent_run(
    result: EmailDrafterRunResult, input_: EmailDrafterInput
) -> None:
    """
    Best-effort write to the agent_runs collection. Logging-only on
    failure — the run itself is the source of truth, the Firestore
    record is an audit trail.
    """
    record: dict = {
        "run_id": result.run_id,
        "agent": "email_drafter",
        "agent_version": 1,
        "contact_id": input_.contact_id,
        "status": result.status,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "from_user": input_.from_user,
        "contact_email": input_.contact_email,
        "contact_municipality": input_.contact_municipality,
        "triggering_event": input_.triggering_event,
        "gmail_draft_id": result.gmail_draft_id,
        "gmail_web_link": result.gmail_web_link,
        "gmail_error": result.gmail_error,
        "drive_file_id": result.drive_file_id,
        "drive_web_link": result.drive_web_link,
        "drive_error": result.drive_error,
        "error": result.error,
    }
    if result.draft:
        record.update(
            {
                "model": result.draft.model,
                "prompt_version": result.draft.prompt_version,
                "input_tokens": result.draft.input_tokens,
                "output_tokens": result.draft.output_tokens,
                "cache_creation_tokens": result.draft.cache_creation_tokens,
                "cache_read_tokens": result.draft.cache_read_tokens,
                "context_chunk_count": result.draft.context_chunk_count,
                "suggested_send": result.draft.suggested_send,
                "subject": result.draft.subject,
            }
        )
    try:
        put_agent_run(result.run_id, record)
    except Exception:
        logger.exception(
            "agent_runs write failed — run not logged",
            extra={"run_id": result.run_id, "contact_id": input_.contact_id},
        )
