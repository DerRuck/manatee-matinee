"""
Gmail client.

Creates drafts in user mailboxes via domain-wide delegation. The runtime
SA `chawq-api-runtime` has DWD configured for scope `gmail.compose` in
Workspace admin (Security → Access and data control → Manage Domain Wide
Delegation, Client ID matched, scope listed). Configured 2026-05-08.

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

# TODO(signature): default Gmail UI signatures don't auto-append to
# drafts created via API. Future: append the user's stored signature
# via Gmail API's users.settings.sendAs.signature, or have the prompt
# generate a per-user sign-off.
"""
from __future__ import annotations

import base64
import logging
from email.mime.text import MIMEText
from typing import Any

from google.auth import default, impersonated_credentials
from googleapiclient.discovery import build

from core.settings import get_settings

logger = logging.getLogger(__name__)


SA_EMAIL = "chawq-api-runtime@chawq-manatee-matinee.iam.gserviceaccount.com"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"


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
        target_scopes=[GMAIL_COMPOSE_SCOPE],
        lifetime=3600,
        subject=from_user,
    )


def _get_gmail_service(from_user: str):
    """Build a Gmail v1 client impersonating `from_user`."""
    creds = _build_delegated_credentials(from_user)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def resolve_from_user(explicit: str | None) -> str:
    """
    Pick the Gmail mailbox that a draft should land in.

    Resolution order:
      1. Explicit value (passed in from caller — typically EmailDrafterInput.from_user).
      2. settings.gmail_simmer_default_user (env var override).
      3. Hard default tyler@chawq.org (set in settings).

    Always returns a non-empty string. Callers can pass the result
    directly to create_draft(from_user=...) without further checks.
    """
    if explicit:
        return explicit
    return get_settings().gmail_simmer_default_user


def create_draft(
    from_user: str,
    to: str,
    subject: str,
    body: str,
) -> dict[str, Any]:
    """
    Create a Gmail draft in `from_user`'s mailbox.

    Args:
        from_user: Gmail account that owns the resulting draft. The SA
            impersonates this user via DWD.
        to: lead's email address (To: header).
        subject: subject line.
        body: plain-text email body.

    Returns:
        Dict from Gmail's drafts.create response, augmented with:
            id        — draft ID
            message   — {id, threadId, ...} of the underlying message
            web_link  — clickable Gmail URL the reviewer can open

    Raises whatever googleapiclient surfaces (HttpError, etc.) on
    failure. Caller should log and continue if this is one of several
    side-effects (e.g., the agent run shouldn't 500 if the Drive write
    succeeds but Gmail draft creation fails).
    """
    service = _get_gmail_service(from_user)

    msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = to
    msg["From"] = from_user
    msg["Subject"] = subject

    raw_b64 = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

    response = (
        service.users()
        .drafts()
        .create(userId="me", body={"message": {"raw": raw_b64}})
        .execute()
    )

    # Convenience link for human review. Gmail draft URLs follow this
    # pattern but aren't part of the API contract — the reviewer can
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
            "to": to,
            "subject": subject,
            "draft_id": draft_id,
        },
    )
    return response
