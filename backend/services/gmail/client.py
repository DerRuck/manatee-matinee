"""
Gmail client.

Creates drafts in user mailboxes via domain-wide delegation. The runtime
SA `chawq-api-runtime` has DWD configured in Workspace admin (Security ->
Access and data control -> Manage Domain Wide Delegation, Client ID
matched, scopes listed).

Scopes used:
  - gmail.compose        - draft creation (configured 2026-05-08).
  - gmail.settings.basic - read the user's sendAs signature so the runner
    can append it to drafts (added 2026-05-29). MUST be authorized for the
    runtime SA's client ID in Workspace admin before get_signature works;
    until then get_signature raises and the runner falls back to no
    signature.

Auth strategy:
  - Cloud Run (K_SERVICE set): default() returns the runtime SA's creds
    via metadata server. impersonated_credentials.Credentials wraps them
    with subject=user_email to mint a DWD-delegated token via signJwt.
  - Local dev: default() returns user OAuth creds. The same flow runs,
    requiring the developer to have `iam.serviceAccountTokenCreator` on
    the runtime SA so signJwt calls succeed. Add it to LOCAL_DEV_GUIDE
    if a new dev hits a 403 here.

Multi-author plan:
  - V1: caller passes `from_user` per call; helper `resolve_from_user`
    falls back to settings.gmail_simmer_default_user (tyler@chawq.org).
  - V2: caller reads `from_user` from a GHL contact custom field
    (lead_owner_email) for auto-routing.

Threading (stretch, DWD path - not wired end-to-end yet):
  create_draft accepts an optional `thread_id`. Passing it associates the
  draft with an existing thread in Gmail. Proper in-thread replies also
  need the In-Reply-To / References headers, which means reading the
  thread first (gmail.readonly scope). That read path is intentionally
  not built here yet; `thread_id` is plumbed now so the reply work is a
  small follow-up rather than a signature change.
"""
from __future__ import annotations

import base64
import logging
import re
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape, unescape
from typing import Any, Optional, Union

from google.auth import default, impersonated_credentials
from googleapiclient.discovery import build

from core.settings import get_settings

logger = logging.getLogger(__name__)


SA_EMAIL = "chawq-api-runtime@chawq-manatee-matinee.iam.gserviceaccount.com"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
GMAIL_SETTINGS_SCOPE = "https://www.googleapis.com/auth/gmail.settings.basic"

# Both scopes ride on every delegated credential. The compose scope can't
# read settings and the settings scope can't compose, so the token needs
# both for a single client to draft and fetch signatures.
GMAIL_SCOPES = [GMAIL_COMPOSE_SCOPE, GMAIL_SETTINGS_SCOPE]

Recipients = Union[str, list, None]


def _build_delegated_credentials(from_user: str):
    """
    Build a Gmail credential that acts as `from_user` via DWD on the
    chawq-api-runtime SA.

    Works in Cloud Run (source = SA metadata creds) and locally (source
    = user ADC, requires `iam.serviceAccountTokenCreator` on the SA).
    `subject` triggers the DWD path inside impersonated_credentials.
    """
    source_creds, _ = default()
    return impersonated_credentials.Credentials(
        source_credentials=source_creds,
        target_principal=SA_EMAIL,
        target_scopes=GMAIL_SCOPES,
        lifetime=3600,
        subject=from_user,
    )


def _get_gmail_service(from_user: str):
    """Build a Gmail v1 client impersonating `from_user`."""
    creds = _build_delegated_credentials(from_user)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def resolve_from_user(explicit: Optional[str]) -> str:
    """
    Pick the Gmail mailbox that a draft should land in.

    Resolution order:
      1. Explicit value (passed in from caller - typically EmailDrafterInput.from_user).
      2. settings.gmail_simmer_default_user (env var override).
      3. Hard default tyler@chawq.org (set in settings).

    Always returns a non-empty string. Callers can pass the result
    directly to create_draft(from_user=...) without further checks.
    """
    if explicit:
        return explicit
    return get_settings().gmail_simmer_default_user


def get_signature(from_user: str) -> Optional[str]:
    """
    Fetch `from_user`'s live Gmail signature (HTML) from their sendAs
    settings.

    Returns the signature HTML for the sendAs entry whose address matches
    `from_user`, falling back to the primary sendAs entry. Returns None
    when no signature is configured.

    Requires the gmail.settings.basic DWD scope. Raises whatever
    googleapiclient surfaces (HttpError on a missing scope, etc.); the
    caller is expected to treat a failure as "no signature" and continue.
    """
    service = _get_gmail_service(from_user)
    response = service.users().settings().sendAs().list(userId="me").execute()
    entries = response.get("sendAs", []) or []

    target = from_user.lower()
    match = next(
        (e for e in entries if (e.get("sendAsEmail") or "").lower() == target),
        None,
    )
    if match is None:
        match = next((e for e in entries if e.get("isPrimary")), None)

    signature = ((match or {}).get("signature") or "").strip()
    return signature or None


def _as_list(value: Recipients) -> list:
    """Normalize a str | list | None recipients argument to a clean list."""
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    else:
        items = list(value)
    return [v.strip() for v in items if v and v.strip()]


def _body_to_html(body: str) -> str:
    """Render the plain-text model body as minimal HTML (escape + nl2br)."""
    return escape(body).replace("\n", "<br>\n")


_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
_BLOCK_RE = re.compile(r"<\s*/?\s*(p|div|tr|table)\b[^>]*>", re.IGNORECASE)


def _html_to_text(html: str) -> str:
    """Rough HTML->text for the plain-text alternative part of a signature."""
    text = _BR_RE.sub("\n", html)
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = unescape(text)
    # Collapse runs of 3+ newlines the block substitutions can create.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def create_draft(
    from_user: str,
    to: Recipients,
    subject: str,
    body: str,
    cc: Recipients = None,
    signature_html: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> dict:
    """
    Create a Gmail draft in `from_user`'s mailbox.

    Args:
        from_user: Gmail account that owns the resulting draft. The SA
            impersonates this user via DWD.
        to: lead recipient(s) - a single address or a list. The first
            address is the one the email was personalized to.
        subject: subject line.
        body: plain-text email body.
        cc: optional Cc recipient(s) - single address or list.
        signature_html: optional HTML signature to append. When provided,
            the draft is sent as multipart/alternative (HTML + a
            text-rendered fallback) so the signature renders in Gmail.
            When None, the draft is a plain-text message as before.
        thread_id: optional Gmail thread ID to associate the draft with an
            existing thread (groundwork for in-thread replies; see module
            docstring).

    Returns:
        Dict from Gmail's drafts.create response, augmented with:
            id        - draft ID
            message   - {id, threadId, ...} of the underlying message
            web_link  - clickable Gmail URL the reviewer can open

    Raises whatever googleapiclient surfaces (HttpError, etc.) on
    failure, plus ValueError if no To recipient resolves. Caller should
    log and continue if this is one of several side-effects.
    """
    service = _get_gmail_service(from_user)

    to_list = _as_list(to)
    cc_list = _as_list(cc)
    if not to_list:
        raise ValueError("create_draft requires at least one To recipient")

    if signature_html:
        msg = MIMEMultipart("alternative")
        text_body = body + "\n\n" + _html_to_text(signature_html)
        html_body = _body_to_html(body) + "\n" + signature_html
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    else:
        msg = MIMEText(body, "plain", "utf-8")

    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg["From"] = from_user
    msg["Subject"] = subject

    raw_b64 = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

    message_body = {"raw": raw_b64}
    if thread_id:
        message_body["threadId"] = thread_id

    response = (
        service.users()
        .drafts()
        .create(userId="me", body={"message": message_body})
        .execute()
    )

    # Convenience link for human review. Gmail draft URLs follow this
    # pattern but aren't part of the API contract - the reviewer can
    # also open Drafts in Gmail and find the message manually.
    draft_id = response.get("id")
    response["web_link"] = (
        f"https://mail.google.com/mail/u/0/#drafts?compose={draft_id}"
        if draft_id
        else None
    )

    logger.info(
        "gmail draft created",
        extra={
            "from_user": from_user,
            "to_count": len(to_list),
            "cc_count": len(cc_list),
            "subject": subject,
            "draft_id": draft_id,
            "signature_appended": bool(signature_html),
            "threaded": bool(thread_id),
        },
    )
    return response
