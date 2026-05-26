"""
Inbound webhook endpoints.

Two producers send us webhooks:
  1. Google Drive push notifications (file added/changed in watched folders).
  2. GoHighLevel workflow actions (stage change, form submit, inbound SMS/email).

Both endpoints return 202 fast and hand work off to a background task,
per the V1 "asynchronous tasks only" guardrail.

GHL auth: shared-secret header check (D2, landed 5/8). Workflow webhooks
send no signature, so a static `X-CHawq-Webhook-Secret` header matched
against `settings.ghl_webhook_secret` is the practical Sprint 2.0 auth.
If `ghl_webhook_secret` is unset, the endpoint accepts without checking
and logs a warning — preserves local-dev ergonomics. Tighten in prod by
populating the secret via Secret Manager.

GHL agent dispatch (D3, landed 5/8): the payload's `agent` field selects
which runner picks up the work. The DISPATCH_HANDLERS registry maps
agent name → background-task function. Extend by adding one entry; the
runner must take a single `payload: dict` argument and never raise.
"""
import json
import logging
import secrets
from typing import Any, Callable

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, status

from core.settings import get_settings
from ingestion import ingest_one_drive_file
from services.drive.client import (
    get_drive_start_page_token,
    list_drive_changes,
)
from services.email_drafter_runner import run_email_drafter_for_ghl_payload
from services.firestore.client import (
    get_drive_watch_state,
    set_drive_watch_state,
)
# Restored when feat/research-agent merges to dev (carries the research_agent module).
# Commented out on feat/agent-runs so the app boots without that module present.
# from services.research_agent.research_ import run_research_agent_for_ghl_payload

logger = logging.getLogger(__name__)

router = APIRouter()


# Maps `payload["agent"]` to the background-task entry point. Each
# handler MUST accept a single `payload: dict[str, Any]` argument and
# MUST NOT raise — the webhook has already returned 202 by the time the
# handler runs, so unhandled exceptions vanish into the BackgroundTask
# worker. Per-agent runners wrap their bodies in try/except for this.
#
# Add a new agent by registering its `*_for_ghl_payload` function here.
# Luis's deep_research runner registers when that side lands.
DISPATCH_HANDLERS: dict[str, Callable[[dict[str, Any]], None]] = {
    "email_drafter": run_email_drafter_for_ghl_payload,
    # "deep_research": run_research_agent_for_ghl_payload,  # restored with the import above when feat/research-agent merges
}


GHL_WEBHOOK_SECRET_HEADER = "X-CHawq-Webhook-Secret"


def _verify_ghl_shared_secret(provided: str | None) -> None:
    """
    Compare the provided X-CHawq-Webhook-Secret header against the
    configured `ghl_webhook_secret`. Raises HTTPException(401) on
    mismatch. Logs + accepts when the secret is unset (local dev).

    Constant-time comparison via secrets.compare_digest blocks timing
    attacks even though the secret is short.
    """
    expected = get_settings().ghl_webhook_secret
    if not expected:
        logger.warning(
            "ghl_webhook_secret unset — accepting webhook unauthenticated. "
            "Set GHL_WEBHOOK_SECRET in prod."
        )
        return

    if not provided:
        logger.info("ghl webhook missing %s header — rejecting", GHL_WEBHOOK_SECRET_HEADER)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing webhook secret header",
        )

    if not secrets.compare_digest(provided, expected):
        logger.info("ghl webhook secret mismatch — rejecting")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid webhook secret",
        )


# -------------------- Google Drive --------------------

@router.post("/drive", status_code=status.HTTP_202_ACCEPTED)
async def drive_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_goog_resource_state: str | None = Header(default=None),
    x_goog_channel_id: str | None = Header(default=None),
    x_goog_resource_id: str | None = Header(default=None),
    x_goog_channel_token: str | None = Header(default=None),
    x_goog_message_number: str | None = Header(default=None),
    x_goog_changed: str | None = Header(default=None),
    x_goog_resource_uri: str | None = Header(default=None),
):
    """
    Receives Drive change notifications.

    Drive push notifications carry their payload entirely in headers; the body
    is usually empty. We validate the channel ID against our watch registry
    (TODO) and enqueue an ingestion job.

    Resource state values we'll see:
      - "sync"   : sent once immediately after files.watch() creates the channel.
                   Not a real change — just a handshake confirming the webhook
                   is reachable. Acknowledge and skip ingestion.
      - "add", "update", "remove", "trash", "untrash": real changes.
    """
    logger.info(
        "drive webhook received",
        extra={
            "resource_state": x_goog_resource_state,
            "channel_id": x_goog_channel_id,
            "resource_id": x_goog_resource_id,
            "message_number": x_goog_message_number,
            "changed": x_goog_changed,
        },
    )

    # Handshake event — Drive sends one of these the moment the channel is
    # created. No file changed; just confirm receipt and move on.
    if x_goog_resource_state == "sync":
        logger.info(
            "drive webhook sync handshake",
            extra={"channel_id": x_goog_channel_id, "resource_id": x_goog_resource_id},
        )
        return {"status": "accepted", "event": "sync"}

    # Resolve the watch state cursor. First push after deploy initializes it
    # via Drive's startPageToken so we don't try to walk all-of-history.
    state = get_drive_watch_state()
    page_token = state.get("page_token") if state else None
    if not page_token:
        page_token = get_drive_start_page_token()
        logger.info(
            "drive webhook: initialized watch state cursor",
            extra={"page_token": page_token},
        )

    # Walk all pages of changes since the last cursor. Dispatch each
    # changed file_id to the orchestrator as a background task so the
    # webhook returns 202 immediately and ingestion happens out-of-band.
    dispatched = 0
    next_page_token = page_token
    while next_page_token:
        resp = list_drive_changes(next_page_token)
        for change in resp.get("changes", []):
            file_id = change.get("fileId")
            if not file_id:
                continue
            if change.get("removed"):
                # File was deleted in Drive. V1 leaves the documents/chunks
                # rows in place; cleanup is a separate concern. Log so the
                # divergence is visible.
                logger.info(
                    "drive webhook: file removed in Drive, leaving Firestore rows",
                    extra={"file_id": file_id},
                )
                continue
            background_tasks.add_task(ingest_one_drive_file, file_id)
            dispatched += 1

        # Advance the cursor. On the final page Drive sends newStartPageToken;
        # earlier pages send nextPageToken.
        next_page_token = resp.get("nextPageToken")
        if not next_page_token:
            new_start = resp.get("newStartPageToken")
            if new_start:
                set_drive_watch_state(
                    new_start,
                    channel_id=x_goog_channel_id,
                    resource_id=x_goog_resource_id,
                )
            break

    logger.info(
        "drive webhook: dispatched %d ingest task(s)",
        dispatched,
        extra={"event": x_goog_resource_state},
    )
    return {
        "status": "accepted",
        "event": x_goog_resource_state,
        "dispatched": dispatched,
    }


# -------------------- GoHighLevel --------------------

@router.post("/ghl", status_code=status.HTTP_202_ACCEPTED)
async def ghl_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_wh_signature: str | None = Header(default=None),
    x_ghl_signature: str | None = Header(default=None),
    x_chawq_webhook_secret: str | None = Header(default=None),
):
    """
    Receives outbound webhooks from GHL workflows.

    Auth: workflow webhooks send no signature, so we require a static
    `X-CHawq-Webhook-Secret` header that matches settings.ghl_webhook_secret.
    Configure the header on the GHL Workflow's webhook step. API-subscribed
    webhooks (HMAC-SHA256 today, Ed25519 after 2026-07-01) carry signatures
    via x_wh_signature / x_ghl_signature — that path is wired separately
    when API webhooks come into scope.

    Dispatch (D3 task): the workflow payload carries `agent` and either
    `contact_id` or `id`; the handler resolves the runner and enqueues
    it as a background task so the webhook returns 202 immediately.
    """
    _verify_ghl_shared_secret(x_chawq_webhook_secret)

    body = await request.body()

    # TODO(API-subscribed): verify_ghl_signature(body, x_ghl_signature
    # or x_wh_signature) — only relevant once API-subscribed webhooks
    # are in use.

    # Parse the JSON payload defensively. GHL Workflow webhooks send JSON,
    # but a malformed body shouldn't 500 the endpoint — log and 202 so the
    # workflow doesn't surface a failure to the operator.
    try:
        payload = json.loads(body) if body else {}
        if not isinstance(payload, dict):
            logger.warning(
                "ghl webhook payload not an object — ignoring",
                extra={"payload_type": type(payload).__name__},
            )
            payload = {}
    except json.JSONDecodeError:
        logger.warning("ghl webhook body was not valid JSON — ignoring")
        payload = {}

    agent_name = payload.get("agent") if isinstance(payload, dict) else None
    contact_id = (
        payload.get("contact_id") or payload.get("id")
        if isinstance(payload, dict)
        else None
    )

    logger.info(
        "ghl webhook received",
        extra={
            "body_bytes": len(body),
            "agent": agent_name,
            "contact_id": contact_id,
            "has_ghl_sig": bool(x_ghl_signature),
            "has_legacy_sig": bool(x_wh_signature),
        },
    )

    # Dispatch by `agent` field. Unknown agents are accepted (so the GHL
    # workflow doesn't see a failure) but logged so misconfigurations
    # show up in Cloud Logging instead of failing silently.
    if not agent_name:
        logger.warning(
            "ghl webhook missing `agent` field — no dispatch",
            extra={"contact_id": contact_id},
        )
        return {"status": "accepted", "dispatched": False, "reason": "missing_agent"}

    handler = DISPATCH_HANDLERS.get(agent_name)
    if handler is None:
        logger.info(
            "ghl webhook agent not wired — accepted but not dispatched",
            extra={"agent": agent_name, "contact_id": contact_id},
        )
        return {
            "status": "accepted",
            "dispatched": False,
            "reason": "agent_not_wired",
            "agent": agent_name,
        }

    background_tasks.add_task(handler, payload)
    logger.info(
        "ghl webhook dispatched",
        extra={"agent": agent_name, "contact_id": contact_id},
    )
    return {"status": "accepted", "dispatched": True, "agent": agent_name}
