"""
Email Drafter agent.

Generates a "Simmer" email for a lead post-event. Loads the
email_drafter/v1.yaml prompt, fetches the lead's recent context from the
chunks collection, calls Claude, parses the structured JSON response,
and returns a typed EmailDraftResult.

V1 retrieval: filter chunks by municipality (contact_id is not yet
populated by the ingest resolvers, so municipality is the realistic
filter for the Rookery Bay + SFWMD pilot leads). Future V2 switches to
contact_id once the ingest path tags chunks with GHL contact IDs.

This agent is intentionally decoupled from the Gmail draft creator and
the Drive record writer. The orchestration pattern is:

    agent = EmailDrafterAgent(version=1)
    result = agent.run_for_lead(EmailDrafterInput(...))
    services.gmail.create_draft(
        from_user=resolve_from_user(input_),
        to=lead_email,
        subject=result.subject,
        body=result.body,
    )
    services.drive.upload_text_file(..., content=json.dumps(result, ...))

Keeps the agent reusable for non-Gmail callers (e.g., a future Outlook
path, a CLI smoke that just inspects the output, or an evaluation
harness comparing prompt versions).

# TODO(signature): default Gmail UI signatures do NOT auto-append to
# drafts created via API. Future work: prompt takes a `signature_name`
# input matching the From: user, and/or the runner appends the user's
# stored signature via Gmail API before draft creation. Deferred 5/8.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Iterable, Literal, Optional

from agents.base import BaseAgent
from services.firestore.client import find_chunks_by_filters
from utils.dates import today_iso_date

logger = logging.getLogger(__name__)


SuggestedSend = Literal["next_business_day", "in_3_days", "next_week"]


@dataclass
class EmailDrafterInput:
    """
    Inputs the runner needs to draft a Simmer email for one lead.

    Identity fields drive both the prompt's lead-profile section and the
    chunk filter. Triggering-event fields ground the email in the actual
    interaction so the model has something concrete to reference.

    `from_user` is the Gmail mailbox that should own the resulting draft.
    The runner does NOT consume it directly — downstream code (the Gmail
    draft creator) reads it. V1 supplies it per run; V2 will populate it
    from a GHL custom field auto-routed per contact.
    """

    contact_id: str
    contact_first_name: str
    contact_last_name: str
    contact_organization: str
    triggering_event: str

    contact_title: Optional[str] = None
    contact_municipality: Optional[str] = None  # canonical slug, e.g. "rookery_bay_fl"
    contact_email: Optional[str] = None  # the lead's email — single-recipient shorthand
    triggering_event_date: Optional[str] = None  # ISO date "YYYY-MM-DD"
    triggering_event_summary: Optional[str] = None  # one-line of what was discussed

    # Multi-recipient support. `to_recipients` are the primary addressees;
    # the FIRST To address is the one the email is personalized to (drives
    # the prompt's "Hi <first name>," opener via the lead profile fields).
    # `cc_recipients` are copied. `contact_email` stays as the
    # single-recipient shorthand: when `to_recipients` is empty it seeds
    # the first (and only) To. Resolve via resolved_to_recipients() /
    # resolved_cc_recipients() rather than reading the raw fields so the
    # back-compat fallback and de-duplication happen in one place.
    to_recipients: Optional[list[str]] = None
    cc_recipients: Optional[list[str]] = None

    # When True (default), the runner fetches the from_user's live Gmail
    # signature (sendAs settings) and appends it to the draft, stripping
    # the prompt's "— C-HAWQ team" sign-off so the email doesn't close
    # twice. Requires the gmail.settings.basic DWD scope authorized for
    # the runtime SA; if the fetch fails the runner falls back to the
    # draft body as-is.
    append_signature: bool = True

    # Per-run Gmail author override. If None, downstream Gmail code falls
    # back to settings.gmail_simmer_default_user. Populated per V1 plan;
    # V2 auto-routes from a GHL contact custom field.
    from_user: Optional[str] = None

    # Retrieval filter overrides. Default behavior is "filter by
    # municipality and pull the top N chunks." Tests / specialized
    # callers can override.
    # Reply mode: the prior thread rendered as text, used as the
    # model's primary context when drafting an in-thread reply.
    # None for the Simmer flow.
    thread_text: Optional[str] = None

    context_chunk_limit: int = 8
    context_filter_municipalities: Optional[list[str]] = None
    context_filter_contact_ids: Optional[list[str]] = None
    context_filter_document_types: Optional[list[str]] = None

    def resolved_to_recipients(self) -> list[str]:
        """
        The To: list, de-duplicated and order-preserving. Falls back to
        the single `contact_email` shorthand when `to_recipients` is empty
        so existing single-recipient callers keep working unchanged.
        """
        emails = list(self.to_recipients or [])
        if not emails and self.contact_email:
            emails = [self.contact_email]
        return _dedupe_emails(emails)

    def resolved_cc_recipients(self) -> list[str]:
        """The Cc: list, de-duplicated and stripped of any address that is
        already a To: recipient (Gmail would otherwise double-send)."""
        to_lower = {e.lower() for e in self.resolved_to_recipients()}
        cc = [e for e in (self.cc_recipients or []) if e.lower() not in to_lower]
        return _dedupe_emails(cc)


@dataclass
class EmailDraftResult:
    """
    Structured result the runner returns. Downstream callers (Gmail draft
    creator, Drive record writer, agent_runs logger) read these fields
    directly — no JSON re-parsing required.
    """

    subject: str
    body: str
    tone_notes: str
    suggested_send: SuggestedSend
    suggested_send_reason: str

    # Retrieval + run metadata for logging / debugging.
    context_chunk_count: int
    raw_model_output: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    prompt_version: int
    model: str

    # Echoed from input for downstream draft-creation convenience —
    # callers don't need to thread the input dataclass through their
    # pipeline alongside the result.
    from_user: Optional[str] = None
    contact_email: Optional[str] = None
    to_recipients: Optional[list[str]] = None
    cc_recipients: Optional[list[str]] = None


class EmailDrafterAgent(BaseAgent):
    """
    Subclass of BaseAgent that loads the email_drafter prompt and adds a
    lead-aware run_for_lead() entry point. The plain run() inherited from
    BaseAgent stays available for testing the prompt in isolation.
    """

    def __init__(self, version: int = 1, prompt_name: str = "email_drafter") -> None:
        super().__init__(prompt_name, version)

    def run_for_lead(self, input_: EmailDrafterInput) -> EmailDraftResult:
        """
        Build context, call Claude, parse JSON, return typed result.

        Retrieval filters resolve in this order:
          1. Explicit context_filter_contact_ids if provided.
          2. Explicit context_filter_municipalities if provided.
          3. Implicit fallback: contact_municipality (single-element list).
          4. No filter (returns whatever Firestore yields up to limit).

        The simmer prompt's content guardrail handles the empty-context
        case ("if context is thin, keep the email short and general").
        """
        context_chunks = _retrieve_context_chunks(input_)
        user_message = _build_user_message(input_, context_chunks)

        result = self.run(user_message)
        parsed = _parse_email_json(result.content)

        return EmailDraftResult(
            subject=parsed["subject"],
            body=parsed["body"],
            tone_notes=parsed["tone_notes"],
            suggested_send=parsed["suggested_send"],
            suggested_send_reason=parsed["suggested_send_reason"],
            context_chunk_count=len(context_chunks),
            raw_model_output=result.content,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_creation_tokens=result.cache_creation_tokens,
            cache_read_tokens=result.cache_read_tokens,
            prompt_version=self.config.version,
            model=result.model,
            from_user=input_.from_user,
            contact_email=input_.contact_email,
            to_recipients=input_.resolved_to_recipients(),
            cc_recipients=input_.resolved_cc_recipients(),
        )

    def run_reply(self, input_: EmailDrafterInput) -> EmailDraftResult:
        """
        Draft an in-thread reply. Same shape as run_for_lead, but the user
        message centers on the prior thread (input_.thread_text) plus the
        sender's intent (triggering_event / triggering_event_summary). Org
        context chunks are still pulled (municipality-scoped) as background.
        Reuses the JSON parser and EmailDraftResult. Construct the agent with
        prompt_name="email_drafter_reply" so the reply system prompt loads.
        """
        context_chunks = _retrieve_context_chunks(input_)
        user_message = _build_reply_message(input_, context_chunks)

        result = self.run(user_message)
        parsed = _parse_email_json(result.content)

        return EmailDraftResult(
            subject=parsed["subject"],
            body=parsed["body"],
            tone_notes=parsed["tone_notes"],
            suggested_send=parsed["suggested_send"],
            suggested_send_reason=parsed["suggested_send_reason"],
            context_chunk_count=len(context_chunks),
            raw_model_output=result.content,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_creation_tokens=result.cache_creation_tokens,
            cache_read_tokens=result.cache_read_tokens,
            prompt_version=self.config.version,
            model=result.model,
            from_user=input_.from_user,
            contact_email=input_.contact_email,
            to_recipients=input_.resolved_to_recipients(),
            cc_recipients=input_.resolved_cc_recipients(),
        )



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dedupe_emails(emails: Iterable[str]) -> list[str]:
    """Order-preserving de-dupe, case-insensitive, dropping blanks."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in emails:
        addr = (raw or "").strip()
        if not addr:
            continue
        key = addr.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(addr)
    return out


def _retrieve_context_chunks(input_: EmailDrafterInput) -> list[dict[str, Any]]:
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
            "context retrieval failed — continuing without chunks",
            extra={
                "contact_id": input_.contact_id,
                "municipality": input_.contact_municipality,
            },
        )
        return []


def _build_user_message(
    input_: EmailDrafterInput,
    context_chunks: list[dict[str, Any]],
) -> str:
    """Render lead profile + triggering event + retrieved context block."""
    lines: list[str] = []
    # Inject today's date FIRST so the model can reason about recency
    # without parsing dates out of retrieved chunks (chunks carry the
    # event timestamps from when they were authored, not today). The
    # prompt's suggested_send section references this value explicitly.
    lines.append(f"TODAY'S DATE: {today_iso_date()}")
    lines.append("")
    lines.append("LEAD PROFILE")
    lines.append(f"  First name: {input_.contact_first_name}")
    lines.append(f"  Last name: {input_.contact_last_name}")
    if input_.contact_title:
        lines.append(f"  Title: {input_.contact_title}")
    lines.append(f"  Organization: {input_.contact_organization}")
    if input_.contact_municipality:
        lines.append(f"  Municipality (slug): {input_.contact_municipality}")

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
            "  (no prior context retrieved — keep the email short and general "
            "per the system prompt's content guardrail)"
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
        "Draft the Simmer email per the system prompt. Return JSON only — "
        "no markdown fences, no preamble, no trailing commentary."
    )
    return "\n".join(lines)


def _build_reply_message(
    input_: EmailDrafterInput,
    context_chunks: list[dict[str, Any]],
) -> str:
    """Render the prior thread + reply intent + org context for reply mode."""
    lines: list[str] = []
    lines.append(f"TODAY'S DATE: {today_iso_date()}")
    lines.append("")
    lines.append("YOU ARE DRAFTING A REPLY to the email thread below.")
    lines.append("")
    lines.append("LEAD PROFILE")
    lines.append(f"  First name: {input_.contact_first_name}")
    lines.append(f"  Last name: {input_.contact_last_name}")
    if input_.contact_title:
        lines.append(f"  Title: {input_.contact_title}")
    lines.append(f"  Organization: {input_.contact_organization}")

    lines.append("")
    lines.append("WHAT TO CONVEY IN THE REPLY")
    lines.append(f"  {input_.triggering_event}")
    if input_.triggering_event_summary:
        lines.append(f"  {input_.triggering_event_summary}")

    lines.append("")
    lines.append("PRIOR THREAD (oldest to newest)")
    if input_.thread_text:
        for ln in input_.thread_text.splitlines():
            lines.append(f"  {ln}")
    else:
        lines.append("  (thread text unavailable)")

    lines.append("")
    lines.append("ORG CONTEXT (from C-HAWQ knowledge base)")
    if not context_chunks:
        lines.append("  (none retrieved)")
    else:
        for i, chunk in enumerate(context_chunks, start=1):
            text = (chunk.get("text") or "").strip()
            lines.append(f"  [{i}] {text}")

    lines.append("")
    lines.append(
        "Draft the reply per the system prompt. Return JSON only - no "
        "markdown fences, no preamble, no trailing commentary."
    )
    return "\n".join(lines)


def _parse_email_json(raw: str) -> dict[str, Any]:
    """
    Parse the model's JSON output. Strips a code fence if Claude wrapped
    it despite the system prompt asking otherwise. Validates the
    expected fields are present.

    Raises ValueError with the raw output (truncated) attached on
    failure so the caller can log it for prompt-iteration purposes.
    """
    text = raw.strip()
    if text.startswith("```"):
        # Drop the opening fence (with optional language tag) and the
        # closing fence if present.
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1 :]
        if text.endswith("```"):
            text = text[: -3]
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"email_drafter output was not valid JSON: {exc}. "
            f"Raw[:500]: {raw[:500]}"
        ) from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"email_drafter output was not a JSON object. "
            f"Raw[:500]: {raw[:500]}"
        )

    required = {
        "subject",
        "body",
        "tone_notes",
        "suggested_send",
        "suggested_send_reason",
    }
    missing = required - data.keys()
    if missing:
        raise ValueError(
            f"email_drafter output missing required fields: {sorted(missing)}. "
            f"Raw[:500]: {raw[:500]}"
        )

    valid_send_values = {"next_business_day", "in_3_days", "next_week"}
    if data["suggested_send"] not in valid_send_values:
        raise ValueError(
            f"email_drafter suggested_send='{data['suggested_send']}' not in "
            f"{sorted(valid_send_values)}. Raw[:500]: {raw[:500]}"
        )

    return data
