"""
Letter agent runner — orchestration layer.

Composes the four side-effects of a Letter Agent run into one safe-to-
call entry point:
  1. LetterAgent.run_for_lead()       — the model call
  2. services.letter.render_letter_to_pdf() — HTML → PDF
  3. services.drive.upload_bytes_file()     — PDF to Drive (binary)
     services.drive.upload_text_file()      — audit .md companion
  4. services.firestore.put_agent_run()     — the agent_runs row

Mirrors the shape of services/email_drafter_runner.py so the webhook
dispatcher can BackgroundTask-add either runner without restructuring
the call site.

Drive layout (locked 5/11):
    /<DRIVE_OUTPUT_ROOT>/<municipality_slug>/<contact_slug>/letters/
        <timestamp>_letter.pdf     ← human review artifact
        <timestamp>_letter.md      ← audit companion (frontmatter + body)

Failure model: runner returns a typed result with `status` set to
`completed | partial | failed`. Each side-effect after the model call
is in its own try/except — a Drive upload failure shouldn't lose the
PDF bytes or the agent_runs log. The result carries per-step error
fields so the caller can decide what to surface.

# TODO(signature image): V1 renders signature_name as cursive-font
# styled typed text. V2 embeds a PNG handwritten signature once the
# asset pipeline lands. Same deferral as the email signature plan.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from agents.letter import LetterAgent, LetterInput, LetterResult
from core.settings import get_settings
from services.drive.client import (
    find_or_create_folder,
    upload_bytes_file,
    upload_text_file,
)
from services.firestore.client import put_agent_run
from services.letter import render_letter_to_pdf
from utils.municipality import slug_for_municipality, slugify

logger = logging.getLogger(__name__)


RunStatus = Literal["completed", "partial", "failed"]


@dataclass
class LetterRunResult:
    """
    Result of one orchestrated Letter Agent run. `status` is the easy
    flag for callers; the per-step fields tell you which side-effects
    actually landed.
    """

    run_id: str
    contact_id: str
    status: RunStatus

    # The model's output. Present whenever the agent itself succeeded.
    result: Optional[LetterResult] = None

    # PDF render side-effect.
    pdf_drive_file_id: Optional[str] = None
    pdf_drive_web_link: Optional[str] = None
    pdf_error: Optional[str] = None

    # Audit-markdown side-effect.
    audit_drive_file_id: Optional[str] = None
    audit_drive_web_link: Optional[str] = None
    audit_error: Optional[str] = None

    # Top-level error message if the agent itself failed.
    error: Optional[str] = None

    started_at: Optional[str] = None
    finished_at: Optional[str] = None


def run_letter_for_lead(
    input_: LetterInput,
    *,
    skip_pdf: bool = False,
    skip_drive: bool = False,
) -> LetterRunResult:
    """
    Run the Letter Agent end-to-end for one lead.

    Args:
        input_: structured agent input (recipient + sender + triggering
            event + retrieval filter overrides).
        skip_pdf: skip PDF render + Drive PDF upload. Useful for prompt
            iteration smoke tests that just inspect the model output.
        skip_drive: skip the Drive audit-md companion write as well.
            When True alongside skip_pdf, nothing lands in Drive.

    Returns:
        LetterRunResult — never raises. Failures are captured on the
        result so a BackgroundTask runner can log + move on.
    """
    run_id = str(uuid.uuid4())
    started_at = datetime.now(tz=timezone.utc)

    log_extra = {
        "run_id": run_id,
        "contact_id": input_.contact_id,
        "agent": "letter",
    }

    # 1. Model call.
    try:
        agent = LetterAgent(version=1)
        result = agent.run_for_lead(input_)
    except Exception as exc:
        logger.exception("letter agent failed", extra=log_extra)
        finished_at = datetime.now(tz=timezone.utc)
        run_result = LetterRunResult(
            run_id=run_id,
            contact_id=input_.contact_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
            started_at=started_at.isoformat(timespec="seconds"),
            finished_at=finished_at.isoformat(timespec="seconds"),
        )
        _safe_put_agent_run(run_result, input_)
        return run_result

    logger.info(
        "letter model call complete",
        extra={
            **log_extra,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "model": result.model,
            "context_chunks": result.context_chunk_count,
        },
    )

    pdf_drive_file_id: Optional[str] = None
    pdf_drive_web_link: Optional[str] = None
    pdf_error: Optional[str] = None
    audit_drive_file_id: Optional[str] = None
    audit_drive_web_link: Optional[str] = None
    audit_error: Optional[str] = None

    # 2 + 3. Drive output (PDF + audit MD companion).
    if skip_pdf and skip_drive:
        logger.info(
            "skipping drive output (skip_pdf=True, skip_drive=True)",
            extra=log_extra,
        )
    else:
        settings = get_settings()
        output_root = settings.drive_output_root_folder_id
        if not output_root:
            msg = "DRIVE_OUTPUT_ROOT_FOLDER_ID not set — skipping Drive writes"
            logger.warning(msg, extra=log_extra)
            pdf_error = msg if not skip_pdf else None
            audit_error = msg if not skip_drive else None
        else:
            try:
                letters_folder_id = _resolve_letters_folder(input_, output_root)
            except Exception as exc:
                msg = f"folder resolve failed: {type(exc).__name__}: {exc}"
                logger.exception("letter drive folder resolve failed", extra=log_extra)
                pdf_error = msg if not skip_pdf else None
                audit_error = msg if not skip_drive else None
                letters_folder_id = None

            if letters_folder_id:
                # PDF render + upload.
                if skip_pdf:
                    logger.info("skipping pdf render (skip_pdf=True)", extra=log_extra)
                else:
                    try:
                        pdf_bytes = render_letter_to_pdf(
                            result,
                            sender_name=input_.sender_name or result.signature_name,
                            sender_title=input_.sender_title,
                            sender_email=input_.sender_email,
                            generated_at=started_at,
                        )
                        pdf_response = upload_bytes_file(
                            folder_id=letters_folder_id,
                            filename=_build_pdf_filename(started_at),
                            content=pdf_bytes,
                            mime_type="application/pdf",
                        )
                        pdf_drive_file_id = pdf_response.get("id")
                        pdf_drive_web_link = pdf_response.get("webViewLink")
                    except Exception as exc:
                        pdf_error = f"{type(exc).__name__}: {exc}"
                        logger.exception("letter pdf upload failed", extra=log_extra)

                # Audit-md companion.
                if skip_drive:
                    logger.info(
                        "skipping audit-md companion (skip_drive=True)",
                        extra=log_extra,
                    )
                else:
                    try:
                        audit_md = _format_audit_record(
                            run_id=run_id,
                            input_=input_,
                            result=result,
                            pdf_drive_file_id=pdf_drive_file_id,
                            pdf_drive_web_link=pdf_drive_web_link,
                            started_at=started_at,
                        )
                        audit_response = upload_text_file(
                            folder_id=letters_folder_id,
                            filename=_build_audit_filename(started_at),
                            content=audit_md,
                            mime_type="text/markdown",
                        )
                        audit_drive_file_id = audit_response.get("id")
                        audit_drive_web_link = audit_response.get("webViewLink")
                    except Exception as exc:
                        audit_error = f"{type(exc).__name__}: {exc}"
                        logger.exception(
                            "letter audit-md upload failed", extra=log_extra
                        )

    finished_at = datetime.now(tz=timezone.utc)

    # 4. Compute status.
    failed_steps = []
    if not skip_pdf and pdf_error:
        failed_steps.append("pdf")
    if not skip_drive and audit_error:
        failed_steps.append("audit")

    status: RunStatus = "completed" if not failed_steps else "partial"

    run_result = LetterRunResult(
        run_id=run_id,
        contact_id=input_.contact_id,
        status=status,
        result=result,
        pdf_drive_file_id=pdf_drive_file_id,
        pdf_drive_web_link=pdf_drive_web_link,
        pdf_error=pdf_error,
        audit_drive_file_id=audit_drive_file_id,
        audit_drive_web_link=audit_drive_web_link,
        audit_error=audit_error,
        started_at=started_at.isoformat(timespec="seconds"),
        finished_at=finished_at.isoformat(timespec="seconds"),
    )

    _safe_put_agent_run(run_result, input_)

    logger.info(
        "letter run complete",
        extra={
            **log_extra,
            "status": status,
            "pdf_drive_file_id": pdf_drive_file_id,
            "audit_drive_file_id": audit_drive_file_id,
        },
    )
    return run_result


def run_letter_for_ghl_payload(payload: dict[str, Any]) -> None:
    """
    Background-task entry that takes a raw GHL workflow webhook payload,
    builds a LetterInput from it, and runs the orchestrator.

    Never raises — the webhook handler returned 202 before this function
    ran. Signature matches services.email_drafter_runner so the dispatch
    registry can register either uniformly.

    Payload fields consumed (snake_case per GHL Workflow shape):

        Identity (recipient):
          contact_id | id     → contact_id
          first_name          → contact_first_name
          last_name           → contact_last_name
          job_title           → contact_title
          company             → contact_organization
          city                → contact_municipality (slug-normalized)

        Sender (C-HAWQ staff signing the letter):
          sender_name         → sender_name
          sender_title        → sender_title
          sender_email        → sender_email
          (V2: from_user / lead_owner_* once the GHL custom field lands)

        Triggering event:
          triggering_event           → triggering_event
          triggering_event_date      → triggering_event_date
          triggering_event_summary   → triggering_event_summary
                                       (falls back to contact_notes)
    """
    try:
        input_ = _payload_to_input(payload)
        run_letter_for_lead(input_)
    except Exception:
        logger.exception(
            "letter dispatch failed",
            extra={
                "payload_keys": (
                    sorted(payload.keys()) if isinstance(payload, dict) else None
                ),
                "contact_id": (payload or {}).get("contact_id")
                or (payload or {}).get("id"),
            },
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _payload_to_input(payload: dict[str, Any]) -> LetterInput:
    """Map a GHL workflow payload (flat snake_case) into LetterInput."""
    contact_id = payload.get("contact_id") or payload.get("id") or "unknown"

    municipality_slug: Optional[str] = None
    if payload.get("city"):
        municipality_slug = slug_for_municipality(payload["city"])
    elif payload.get("company"):
        municipality_slug = slug_for_municipality(payload["company"])

    return LetterInput(
        contact_id=contact_id,
        contact_first_name=payload.get("first_name") or "",
        contact_last_name=payload.get("last_name") or "",
        contact_title=payload.get("job_title") or None,
        contact_organization=payload.get("company") or "(unknown organization)",
        contact_municipality=municipality_slug,
        triggering_event=payload.get("triggering_event") or "(no event specified)",
        triggering_event_date=payload.get("triggering_event_date") or None,
        triggering_event_summary=(
            payload.get("triggering_event_summary")
            or payload.get("contact_notes")
            or None
        ),
        sender_name=payload.get("sender_name") or None,
        sender_title=payload.get("sender_title") or None,
        sender_email=payload.get("sender_email") or None,
    )


def _resolve_letters_folder(input_: LetterInput, output_root: str) -> str:
    """
    Walk (creating as needed) the locked path
    `<output_root>/<municipality>/<contact>/letters/`. Returns the
    leaf folder ID for the upload calls.

    Matches the per-lead Drive convention used by the email drafter,
    only with `letters` as the typed subfolder instead of `email-drafts`.
    """
    municipality = input_.contact_municipality or "_no_municipality"
    contact = _build_contact_slug(input_)

    municipality_folder = find_or_create_folder(municipality, output_root)
    contact_folder = find_or_create_folder(contact, municipality_folder)
    letters_folder = find_or_create_folder("letters", contact_folder)
    return letters_folder


def _build_contact_slug(input_: LetterInput) -> str:
    """Slug for the contact's Drive folder. Prefers human-readable name."""
    name = " ".join(
        v for v in (input_.contact_first_name, input_.contact_last_name) if v
    ).strip()
    if name:
        return slugify(name)
    return slugify(input_.contact_id) or "unknown_contact"


def _build_pdf_filename(started_at: datetime) -> str:
    """Sortable timestamp; identity context is in the folder structure."""
    return f"{started_at.strftime('%Y-%m-%dT%H-%M-%SZ')}_letter.pdf"


def _build_audit_filename(started_at: datetime) -> str:
    """Audit-md companion shares the timestamp prefix with the PDF."""
    return f"{started_at.strftime('%Y-%m-%dT%H-%M-%SZ')}_letter.md"


def _format_audit_record(
    *,
    run_id: str,
    input_: LetterInput,
    result: LetterResult,
    pdf_drive_file_id: Optional[str],
    pdf_drive_web_link: Optional[str],
    started_at: datetime,
) -> str:
    """
    Markdown audit companion for the Drive record. Frontmatter carries
    structured metadata so a human or future tool can grep it; body
    re-renders the letter content as readable markdown for inline review
    without opening the PDF.
    """
    recipient = " ".join(
        v for v in (input_.contact_first_name, input_.contact_last_name) if v
    ).strip() or "(unknown)"

    frontmatter_lines = [
        "---",
        f"run_id: {run_id}",
        f"agent: letter v{result.prompt_version}",
        f"model: {result.model}",
        f"generated_at: {started_at.isoformat(timespec='seconds')}",
        f"contact_id: {input_.contact_id}",
        f"recipient: {recipient}",
        f"recipient_title: {result.recipient_title}",
        f"recipient_organization: {result.recipient_organization}",
        f"municipality: {input_.contact_municipality or '(unset)'}",
        f"sender_name: {result.sender_name or input_.sender_name or '(unset)'}",
        f"sender_title: {result.sender_title or input_.sender_title or '(unset)'}",
        f"sender_email: {result.sender_email or input_.sender_email or '(unset)'}",
        f"triggering_event: {input_.triggering_event}",
        f"context_chunk_count: {result.context_chunk_count}",
        f"input_tokens: {result.input_tokens}",
        f"output_tokens: {result.output_tokens}",
        f"cache_creation_tokens: {result.cache_creation_tokens}",
        f"cache_read_tokens: {result.cache_read_tokens}",
    ]
    if pdf_drive_file_id:
        frontmatter_lines.append(f"pdf_drive_file_id: {pdf_drive_file_id}")
    if pdf_drive_web_link:
        frontmatter_lines.append(f"pdf_drive_web_link: {pdf_drive_web_link}")
    frontmatter_lines.append("---")

    paragraphs: list[str] = [result.opening_paragraph]
    paragraphs.extend(result.observation_paragraphs)
    paragraphs.extend(result.ideas_paragraphs)
    paragraphs.append(result.offer_paragraph)
    paragraphs.append(result.closing_paragraph)

    sections = [
        "\n".join(frontmatter_lines),
        "",
        f"# {result.subject_line}",
        "",
        f"**To:** {result.recipient_name}, {result.recipient_title}, "
        f"{result.recipient_organization}",
        "",
        *(f"{p}\n" for p in paragraphs),
        "---",
        "",
        f"_{result.signature_name}_",
        "",
        "# Tone notes",
        "",
        result.tone_notes,
        "",
    ]
    return "\n".join(sections)


def _safe_put_agent_run(
    run_result: LetterRunResult, input_: LetterInput
) -> None:
    """Best-effort write to agent_runs. Logging-only on failure."""
    record: dict = {
        "run_id": run_result.run_id,
        "agent": "letter",
        "agent_version": 1,
        "contact_id": input_.contact_id,
        "status": run_result.status,
        "started_at": run_result.started_at,
        "finished_at": run_result.finished_at,
        "contact_municipality": input_.contact_municipality,
        "triggering_event": input_.triggering_event,
        "sender_name": input_.sender_name,
        "sender_email": input_.sender_email,
        "pdf_drive_file_id": run_result.pdf_drive_file_id,
        "pdf_drive_web_link": run_result.pdf_drive_web_link,
        "pdf_error": run_result.pdf_error,
        "audit_drive_file_id": run_result.audit_drive_file_id,
        "audit_drive_web_link": run_result.audit_drive_web_link,
        "audit_error": run_result.audit_error,
        "error": run_result.error,
    }
    if run_result.result is not None:
        r = run_result.result
        record.update(
            {
                "model": r.model,
                "prompt_version": r.prompt_version,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "cache_creation_tokens": r.cache_creation_tokens,
                "cache_read_tokens": r.cache_read_tokens,
                "context_chunk_count": r.context_chunk_count,
                "subject_line": r.subject_line,
                "recipient_name": r.recipient_name,
                "recipient_organization": r.recipient_organization,
            }
        )
    try:
        put_agent_run(run_result.run_id, record)
    except Exception:
        logger.exception(
            "letter agent_runs write failed — run not logged",
            extra={"run_id": run_result.run_id, "contact_id": input_.contact_id},
        )
