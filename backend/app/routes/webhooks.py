"""
Inbound webhook endpoints.

Two producers send us webhooks:
  1. Google Drive push notifications (file added/changed in watched folders).
  2. GoHighLevel workflow actions (stage change, form submit, inbound SMS/email).

Both endpoints return 200 fast and hand work off to a background task,
per the V1 "asynchronous tasks only" guardrail.

All signature verification is TODO — wired in during Sprint 2 GHL integration.
"""
import json
import logging

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, status

from services.hello_world_runner import run_hello_world_for_ghl_contact

logger = logging.getLogger(__name__)

router = APIRouter()


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

    # --- SPIKE DEBUG (Sprint 1 Task 3) — remove before Sprint 2 merge ---------
    # Goal: capture every X-Goog-* header Drive actually sends so we can lock
    # down the handler shape + channel-registry schema for Sprint 2.
    goog_headers = {
        k: v for k, v in request.headers.items() if k.lower().startswith("x-goog-")
    }
    try:
        body_preview = (await request.body()).decode("utf-8")[:1000]
    except UnicodeDecodeError:
        body_preview = "<non-utf8 body>"
    logger.info(
        "drive webhook SPIKE debug",
        extra={"goog_headers": goog_headers, "body_preview": body_preview},
    )
    # --- end SPIKE DEBUG ------------------------------------------------------

    # Handshake event — Drive sends one of these the moment the channel is
    # created. No file changed; just confirm receipt and move on.
    if x_goog_resource_state == "sync":
        logger.info(
            "drive webhook sync handshake",
            extra={"channel_id": x_goog_channel_id, "resource_id": x_goog_resource_id},
        )
        return {"status": "accepted", "event": "sync"}

    # TODO: validate x_goog_channel_token against the expected shared secret.
    # TODO: validate x_goog_channel_id against stored watch channels in Firestore.
    # TODO: enqueue ingestion task.
    # background_tasks.add_task(ingest_drive_resource, x_goog_resource_id)

    return {"status": "accepted", "event": x_goog_resource_state}


# -------------------- GoHighLevel --------------------

@router.post("/ghl", status_code=status.HTTP_202_ACCEPTED)
async def ghl_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_wh_signature: str | None = Header(default=None),
    x_ghl_signature: str | None = Header(default=None),
):
    """
    Receives outbound webhooks from GHL workflows.

    GHL is migrating from HMAC-SHA256 (X-WH-Signature) to Ed25519 (X-GHL-Signature)
    on 2026-07-01. We read both headers now and verify whichever is present.
    """
    body = await request.body()

    # TODO: verify_ghl_signature(body, x_ghl_signature or x_wh_signature)
    # For now, log and return 202 so we can wire the spike without auth.
    logger.info(
        "ghl webhook received",
        extra={
            "body_bytes": len(body),
            "has_ghl_sig": bool(x_ghl_signature),
            "has_legacy_sig": bool(x_wh_signature),
        },
    )

    # --- SPIKE DEBUG — remove before Sprint 2 (Agents) merge ---------
    # Goal: see what GHL actually sends so we can lock the signature header name,
    # the event-type field, and the payload shape for the Sprint 2 handler.
    try:
        body_preview = body.decode("utf-8")[:2000]
    except UnicodeDecodeError:
        body_preview = repr(body[:1000])
    safe_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in {"authorization", "cookie", "proxy-authorization"}
    }
    logger.info(
        "ghl webhook SPIKE debug",
        extra={"headers": safe_headers, "body_preview": body_preview},
    )
    # --- end SPIKE DEBUG ------------------------------------------------------

    # Parse the JSON payload defensively. GHL Workflow webhooks send JSON,
    # but we don't want a malformed body to 500 the endpoint — log and accept
    # so the workflow doesn't see a failure.
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

    # Sprint demo: every GHL webhook fires the Hello World agent.
    # Drive output + Firestore log layer into
    # run_hello_world_for_ghl_contact, not this handler.
    if payload:
        background_tasks.add_task(run_hello_world_for_ghl_contact, payload)
        logger.info(
            "ghl webhook -> hello_world runner enqueued",
            extra={"contact_id": payload.get("contact_id") or payload.get("id")},
        )

    return {"status": "accepted"}
