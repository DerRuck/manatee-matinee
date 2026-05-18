"""
Letter agent.

Generates a "Yellow Brick Road" letter (Proven Process Stage 4 — Buy-In)
for a lead. Loads letter/v1.yaml, fetches the lead's recent context from
the chunks collection, calls Claude, parses the structured JSON
response, and returns a typed LetterResult.

V1 retrieval mirrors the Email Drafter: filter chunks by municipality
slug (contact_id isn't populated by today's ingest resolvers). The
prompt's content guardrail handles thin-context cases.

This agent is intentionally decoupled from the PDF renderer and the
Drive uploader. Orchestration pattern (in services/letter_runner.py):

    agent = LetterAgent(version=1)
    result = agent.run_for_lead(LetterInput(...))
    pdf_bytes = render_letter_to_pdf(result, sender_title=...)
    services.drive.upload_bytes_file(..., pdf_bytes, "application/pdf")
    services.drive.upload_text_file(..., audit_markdown, "text/markdown")

Keeps the agent reusable for non-PDF callers (e.g., a future attachment path, 
a CLI smoke that just inspects the structured output,
or an evaluation harness comparing prompt versions).

# TODO(signature image): V1 renders the signature_name as typed text in
# the signature block. V2 embeds a PNG handwritten signature when the
# asset lands. Deferred per the 5/12 letter agent plan.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from agents.base import BaseAgent
from services.firestore.client import find_chunks_by_filters

logger = logging.getLogger(__name__)


@dataclass
class LetterInput:
    """
    Inputs the runner needs to draft a Yellow Brick Road letter.

    Identity fields drive both the prompt's recipient-profile section
    and the chunk filter. Sender fields populate the letter's signature
    block and the letterhead's "From:" header.

    `triggering_event` + `_summary` ground the letter in the prior
    conversation so the model has something concrete to reference; the
    prompt's content guardrail handles the thin-context case.
    """

    # Recipient (the lead — receives the letter).
    contact_id: str
    contact_first_name: str
    contact_last_name: str
    contact_organization: str

    # Triggering event — the prior meeting/call this letter follows up on.
    triggering_event: str

    # Optional recipient detail.
    contact_title: Optional[str] = None
    contact_municipality: Optional[str] = None  # canonical slug, e.g. "rookery_bay_fl"

    triggering_event_date: Optional[str] = None  # ISO date "YYYY-MM-DD"
    triggering_event_summary: Optional[str] = None

    # Sender (the C-HAWQ staff member whose signature the letter carries).
    # If sender_name is None, runner falls back to settings.letter_default_sender_name.
    # Mirror of the email drafter's from_user pattern — V1 = per-run override,
    # V2 = read from a GHL contact custom field (lead_owner_*).
    sender_name: Optional[str] = None
    sender_title: Optional[str] = None
    sender_email: Optional[str] = None

    # Retrieval filter overrides. Default behavior is "filter by
    # municipality and pull the top N chunks." Tests / specialized
    # callers can override.
    context_chunk_limit: int = 8
    context_filter_municipalities: Optional[list[str]] = None
    context_filter_contact_ids: Optional[list[str]] = None
    context_filter_document_types: Optional[list[str]] = None


@dataclass
class LetterResult:
    """
    Structured result the runner returns. Downstream callers (PDF
    renderer, Drive uploader, agent_runs logger) read these fields
    directly — no JSON re-parsing required.
    """

    recipient_name: str
    recipient_title: str
    recipient_organization: str
    subject_line: str
    opening_paragraph: str
    observation_paragraphs: list[str]
    ideas_paragraphs: list[str]
    offer_paragraph: str
    closing_paragraph: str
    signature_name: str
    tone_notes: str

    # Retrieval + run metadata for logging / debugging.
    context_chunk_count: int
    raw_model_output: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    prompt_version: int
    model: str

    # Echoed from input for downstream convenience.
    sender_name: Optional[str] = None
    sender_title: Optional[str] = None
    sender_email: Optional[str] = None


class LetterAgent(BaseAgent):
    """
    Subclass of BaseAgent that loads the letter prompt and adds a
    lead-aware run_for_lead() entry point. The plain run() inherited
    from BaseAgent stays available for testing the prompt in isolation.
    """

    def __init__(self, version: int = 1) -> None:
        super().__init__("letter", version)

    def run_for_lead(self, input_: LetterInput) -> LetterResult:
        """
        Build context, call Claude, parse JSON, return typed result.

        Retrieval filters resolve in this order:
          1. Explicit context_filter_contact_ids if provided.
          2. Explicit context_filter_municipalities if provided.
          3. Implicit fallback: contact_municipality (single-element list).
          4. No filter (returns whatever Firestore yields up to limit).

        The letter prompt's content guardrail handles the empty-context
        case ("if context is thin, keep observations shorter").
        """
        context_chunks = _retrieve_context_chunks(input_)
        user_message = _build_user_message(input_, context_chunks)

        result = self.run(user_message)
        parsed = _parse_letter_json(result.content)

        return LetterResult(
            recipient_name=parsed["recipient_name"],
            recipient_title=parsed["recipient_title"],
            recipient_organization=parsed["recipient_organization"],
            subject_line=parsed["subject_line"],
            opening_paragraph=parsed["opening_paragraph"],
            observation_paragraphs=list(parsed["observation_paragraphs"]),
            ideas_paragraphs=list(parsed["ideas_paragraphs"]),
            offer_paragraph=parsed["offer_paragraph"],
            closing_paragraph=parsed["closing_paragraph"],
            signature_name=parsed["signature_name"],
            tone_notes=parsed["tone_notes"],
            context_chunk_count=len(context_chunks),
            raw_model_output=result.content,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_creation_tokens=result.cache_creation_tokens,
            cache_read_tokens=result.cache_read_tokens,
            prompt_version=self.config.version,
            model=result.model,
            sender_name=input_.sender_name,
            sender_title=input_.sender_title,
            sender_email=input_.sender_email,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _retrieve_context_chunks(input_: LetterInput) -> list[dict[str, Any]]:
    """Resolve the filter and call find_chunks_by_filters."""
    contact_ids: Optional[Iterable[str]] = input_.context_filter_contact_ids
    municipalities: Optional[Iterable[str]] = input_.context_filter_municipalities

    if not contact_ids and not municipalities and input_.contact_municipality:
        municipalities = [input_.contact_municipality]

    try:
        return find_chunks_by_filters(
            contact_ids=contact_ids,
            municipalities=municipalities,
            document_types=input_.context_filter_document_types,
            limit=input_.context_chunk_limit,
        )
    except Exception:
        # Retrieval shouldn't kill the run — the prompt handles empty
        # context. Log and fall through with no chunks.
        logger.exception(
            "letter context retrieval failed — continuing without chunks",
            extra={
                "contact_id": input_.contact_id,
                "municipality": input_.contact_municipality,
            },
        )
        return []


def _build_user_message(
    input_: LetterInput,
    context_chunks: list[dict[str, Any]],
) -> str:
    """Render recipient profile + sender + triggering event + retrieved context."""
    lines: list[str] = []
    lines.append("RECIPIENT PROFILE")
    lines.append(f"  First name: {input_.contact_first_name}")
    lines.append(f"  Last name: {input_.contact_last_name}")
    if input_.contact_title:
        lines.append(f"  Title: {input_.contact_title}")
    lines.append(f"  Organization: {input_.contact_organization}")
    if input_.contact_municipality:
        lines.append(f"  Municipality (slug): {input_.contact_municipality}")

    lines.append("")
    lines.append("SENDER (C-HAWQ staff signing this letter)")
    lines.append(f"  Name: {input_.sender_name or '(unset — runner will fill in default)'}")
    if input_.sender_title:
        lines.append(f"  Title: {input_.sender_title}")
    if input_.sender_email:
        lines.append(f"  Email: {input_.sender_email}")

    lines.append("")
    lines.append("TRIGGERING EVENT")
    lines.append(f"  Event: {input_.triggering_event}")
    if input_.triggering_event_date:
        lines.append(f"  Date: {input_.triggering_event_date}")
    if input_.triggering_event_summary:
        lines.append(f"  Discussed: {input_.triggering_event_summary}")

    lines.append("")
    lines.append("CONVERSATION CONTEXT (from C-HAWQ knowledge base)")
    if not context_chunks:
        lines.append(
            "  (no prior context retrieved — keep observations shorter and "
            "lean on what's in the triggering-event summary per the system "
            "prompt's content guardrail)"
        )
    else:
        for i, chunk in enumerate(context_chunks, start=1):
            doc_type = chunk.get("document_type", "unknown")
            data_source = chunk.get("data_source", "unknown")
            text = (chunk.get("text") or "").strip()
            lines.append(f"  [{i}] document_type={doc_type} source={data_source}")
            for chunk_line in text.splitlines():
                lines.append(f"      {chunk_line}")
            lines.append("")

    lines.append("")
    lines.append(
        "Draft the Yellow Brick Road letter per the system prompt. Return "
        "JSON only — no markdown fences, no preamble, no trailing commentary."
    )
    return "\n".join(lines)


def _parse_letter_json(raw: str) -> dict[str, Any]:
    """
    Parse the model's JSON output. Strips a code fence if Claude wrapped
    it despite the system prompt asking otherwise. Validates required
    fields and the list-typed fields' element types.

    Raises ValueError with the raw output (truncated) attached on
    failure so the caller can log it for prompt-iteration purposes.
    """
    text = raw.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"letter output was not valid JSON: {exc}. Raw[:500]: {raw[:500]}"
        ) from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"letter output was not a JSON object. Raw[:500]: {raw[:500]}"
        )

    required = {
        "recipient_name",
        "recipient_title",
        "recipient_organization",
        "subject_line",
        "opening_paragraph",
        "observation_paragraphs",
        "ideas_paragraphs",
        "offer_paragraph",
        "closing_paragraph",
        "signature_name",
        "tone_notes",
    }
    missing = required - data.keys()
    if missing:
        raise ValueError(
            f"letter output missing required fields: {sorted(missing)}. "
            f"Raw[:500]: {raw[:500]}"
        )

    for list_field in ("observation_paragraphs", "ideas_paragraphs"):
        value = data[list_field]
        if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
            raise ValueError(
                f"letter output field {list_field!r} must be a list of strings. "
                f"Raw[:500]: {raw[:500]}"
            )
        if not value:
            raise ValueError(
                f"letter output field {list_field!r} cannot be empty. "
                f"Raw[:500]: {raw[:500]}"
            )

    return data
