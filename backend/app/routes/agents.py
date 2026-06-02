"""
Agent orchestration endpoints.

POST /agents/run            — Dispatch an agent run. Returns {run_id, status: "pending"}.
GET  /agents/runs/{run_id}  — Poll for status + result.

Auth: `X-CHAWQ-Secret` header matched against settings.chawq_shared_secret.
Same pattern as /webhooks/ghl, different secret per the 5/13 architecture
decision (the workbook + Cowork are the human-driven trigger surface;
/webhooks/ghl handles the event-driven path. Sprint 4 will split org policy
so /agents/* stays IAM-protected while /webhooks/* is public).

Dispatch model
--------------
1. POST writes a pending stub to `agent_runs` (status="pending", triggered_by,
   inputs, created_at, contact_id, municipality), then enqueues a BackgroundTask
   and returns {run_id, status: "pending"} immediately.
2. The BackgroundTask invokes the per-agent dispatcher, which flips the doc
   to status="running" with started_at, then calls the runner with the stub's
   run_id.
3. The runner's terminal write (via put_agent_run, now using merge=True) layers
   the agent-specific output on top of the stub — `created_at`, `triggered_by`,
   and `inputs` survive.

The /webhooks/ghl path remains fire-and-forget — no pending stub, no
intermediate `running` state. If parity becomes useful for the GHL-as-dashboard
view, the GHL handler can write a stub before dispatching.
"""
from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, status
from pydantic import BaseModel, Field

from agents.email_drafter import EmailDrafterInput
from core.settings import get_settings
from services.email_drafter_runner import (
    run_email_drafter_for_lead,
    run_email_reply_for_lead,
)
from services.firestore.client import (
    get_agent_run,
    put_agent_run,
    update_agent_run,
)
from services.research_agent_runner import run_research_for_lead

logger = logging.getLogger(__name__)

router = APIRouter()


CHAWQ_SECRET_HEADER = "X-CHAWQ-Secret"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _verify_chawq_shared_secret(provided: str | None) -> None:
    """
    Compare the provided X-CHAWQ-Secret header against the configured
    chawq_shared_secret. Raises HTTPException(401) on mismatch. Logs and
    accepts when the secret is unset (preserves local-dev ergonomics).

    Constant-time comparison via secrets.compare_digest blocks timing
    attacks even though the secret is short.
    """
    expected = get_settings().chawq_shared_secret
    if not expected:
        logger.warning(
            "chawq_shared_secret unset — accepting /agents request "
            "unauthenticated. Set CHAWQ_SHARED_SECRET in prod."
        )
        return

    if not provided:
        logger.info(
            "agents request missing %s header — rejecting",
            CHAWQ_SECRET_HEADER,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing agents secret header",
        )

    if not secrets.compare_digest(provided, expected):
        logger.info("agents request secret mismatch — rejecting")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid agents secret",
        )


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

TriggeredBy = Literal["workbook", "manual", "cron"]


class AgentRunRequest(BaseModel):
    agent: str = Field(
        ...,
        description="Registered agent name, e.g. 'email_drafter'.",
    )
    inputs: dict[str, Any] = Field(
        default_factory=dict,
        description="Agent-specific inputs. Shape varies per agent.",
    )
    triggered_by: TriggeredBy = Field(
        default="workbook",
        description="How this run was initiated. Defaults to 'workbook'.",
    )


class AgentRunResponse(BaseModel):
    run_id: str
    status: Literal["pending"] = "pending"


# ---------------------------------------------------------------------------
# Per-agent dispatchers
# ---------------------------------------------------------------------------

def _dispatch_email_drafter(run_id: str, inputs: dict[str, Any]) -> None:
    """
    BackgroundTask entry for email_drafter via /agents/run.

    Flips the stub's status to "running" with started_at, then invokes
    run_email_drafter_for_lead with the stub's run_id so the runner's
    terminal write (status="completed"|"partial"|"failed") merges into the
    same doc.

    Never raises — failures are caught and recorded on agent_runs so the
    GET endpoint can surface them.
    """
    started_at = datetime.now(tz=timezone.utc)
    try:
        update_agent_run(
            run_id,
            {"status": "running", "started_at": started_at},
        )
    except Exception:
        logger.exception(
            "agents.run failed to flip status to running",
            extra={"run_id": run_id},
        )
        # Continue — the runner's terminal write will still land.

    try:
        input_ = EmailDrafterInput(
            contact_id=inputs.get("contact_id") or "unknown",
            contact_first_name=inputs.get("contact_first_name") or "",
            contact_last_name=inputs.get("contact_last_name") or "",
            contact_title=inputs.get("contact_title"),
            contact_organization=(
                inputs.get("contact_organization") or "(unknown organization)"
            ),
            contact_municipality=inputs.get("contact_municipality"),
            contact_email=inputs.get("contact_email"),
            from_user=inputs.get("from_user"),
            triggering_event=(
                inputs.get("triggering_event") or "(no event specified)"
            ),
            triggering_event_date=inputs.get("triggering_event_date"),
            triggering_event_summary=inputs.get("triggering_event_summary"),
        )
        run_email_drafter_for_lead(input_, run_id=run_id)
    except Exception as exc:
        logger.exception(
            "agents.run email_drafter dispatch failed",
            extra={"run_id": run_id},
        )
        try:
            update_agent_run(
                run_id,
                {
                    "status": "failed",
                    "finished_at": datetime.now(tz=timezone.utc),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        except Exception:
            logger.exception(
                "agents.run failed to record failure",
                extra={"run_id": run_id},
            )


def _dispatch_research(run_id: str, inputs: dict[str, Any]) -> None:
    """
    BackgroundTask entry for the Research Agent via /agents/run.

    Flips the stub's status to "running" with started_at, then invokes
    run_research_for_lead with the stub's run_id so the runner's
    terminal write (status="completed"|"partial"|"failed") merges into
    the same doc.

    The Research Agent's "many steps" (LOBBY-1, PW-3, S1-4, S3-PREP, ...)
    are prompt YAMLs under backend/prompts/research_agent/<TYPE>/v1.yaml.
    The dispatcher doesn't need to know which type was requested — it
    passes `inputs` straight through. The runner picks the YAML from
    inputs["research_type"]. Adding a new type = drop a YAML in. No
    dispatcher change.

    Never raises — failures are caught and recorded on agent_runs so the
    GET endpoint can surface them.
    """
    started_at = datetime.now(tz=timezone.utc)
    try:
        update_agent_run(
            run_id,
            {"status": "running", "started_at": started_at},
        )
    except Exception:
        logger.exception(
            "agents.run failed to flip status to running",
            extra={"run_id": run_id, "agent": "research"},
        )
        # Continue — the runner's terminal write will still land.

    try:
        run_research_for_lead(inputs, run_id=run_id)
    except Exception as exc:
        logger.exception(
            "agents.run research dispatch failed",
            extra={"run_id": run_id},
        )
        try:
            update_agent_run(
                run_id,
                {
                    "status": "failed",
                    "finished_at": datetime.now(tz=timezone.utc),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        except Exception:
            logger.exception(
                "agents.run failed to record research failure",
                extra={"run_id": run_id},
            )


def _dispatch_email_reply(run_id: str, inputs: dict[str, Any]) -> None:
    """
    BackgroundTask entry for the in-thread reply drafter via /agents/run.

    Same lifecycle as _dispatch_email_drafter. `inputs.triggering_event`
    carries what the reply should convey; `inputs.thread_id` (preferred) or
    `inputs.reply_to_contact` / `inputs.contact_email` selects the thread.
    Recipients default to the thread's last sender; to_recipients /
    cc_recipients on inputs override. Never raises.
    """
    started_at = datetime.now(tz=timezone.utc)
    try:
        update_agent_run(run_id, {"status": "running", "started_at": started_at})
    except Exception:
        logger.exception(
            "agents.run failed to flip status to running",
            extra={"run_id": run_id, "agent": "email_drafter_reply"},
        )

    try:
        input_ = EmailDrafterInput(
            contact_id=inputs.get("contact_id") or "unknown",
            contact_first_name=inputs.get("contact_first_name") or "",
            contact_last_name=inputs.get("contact_last_name") or "",
            contact_title=inputs.get("contact_title"),
            contact_organization=(
                inputs.get("contact_organization") or "(unknown organization)"
            ),
            contact_municipality=inputs.get("contact_municipality"),
            contact_email=inputs.get("contact_email"),
            to_recipients=inputs.get("to_recipients"),
            cc_recipients=inputs.get("cc_recipients"),
            append_signature=inputs.get("append_signature", True),
            from_user=inputs.get("from_user"),
            triggering_event=inputs.get("triggering_event") or "(no specific ask)",
            triggering_event_summary=inputs.get("triggering_event_summary"),
        )
        run_email_reply_for_lead(
            input_,
            thread_id=inputs.get("thread_id"),
            contact_email_for_search=inputs.get("reply_to_contact"),
            run_id=run_id,
        )
    except Exception as exc:
        logger.exception(
            "agents.run email_drafter_reply dispatch failed",
            extra={"run_id": run_id},
        )
        try:
            update_agent_run(
                run_id,
                {
                    "status": "failed",
                    "finished_at": datetime.now(tz=timezone.utc),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        except Exception:
            logger.exception(
                "agents.run failed to record failure",
                extra={"run_id": run_id},
            )


# Maps `agent` field on POST body to dispatcher function.
# Each dispatcher takes (run_id, inputs) and is responsible for its own
# agent_runs lifecycle writes after the POST handler writes the stub.
# Add a new agent by wiring its dispatcher here.
AGENT_DISPATCH: dict[str, Callable[[str, dict[str, Any]], None]] = {
    "email_drafter": _dispatch_email_drafter,
    "email_drafter_reply": _dispatch_email_reply,
    "research": _dispatch_research,
    # "scoring": _dispatch_scoring,              # Phase 2
}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post(
    "/run",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AgentRunResponse,
)
async def run_agent(
    body: AgentRunRequest,
    background_tasks: BackgroundTasks,
    x_chawq_secret: str | None = Header(
        default=None, alias=CHAWQ_SECRET_HEADER
    ),
) -> AgentRunResponse:
    _verify_chawq_shared_secret(x_chawq_secret)

    dispatcher = AGENT_DISPATCH.get(body.agent)
    if dispatcher is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"agent not wired: {body.agent}",
        )

    run_id = str(uuid.uuid4())
    now = datetime.now(tz=timezone.utc)

    # Surface contact_id and municipality at the top level for indexed
    # queries on agent_runs. Both pulled from inputs since the shape is
    # agent-agnostic — every agent that operates on a contact has them.
    # Research runs send `municipality_name` instead of `contact_municipality`;
    # accept either so the stub stays comparable across agents.
    contact_id = body.inputs.get("contact_id")
    municipality = (
        body.inputs.get("contact_municipality")
        or body.inputs.get("municipality_name")
    )

    stub: dict[str, Any] = {
        "run_id": run_id,
        "agent": body.agent,
        "status": "pending",
        "triggered_by": body.triggered_by,
        "contact_id": contact_id,
        "municipality": municipality,
        "inputs": body.inputs,
        "created_at": now,
    }

    try:
        put_agent_run(run_id, stub)
    except Exception:
        logger.exception(
            "agents.run failed to write pending stub",
            extra={"run_id": run_id, "agent": body.agent},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="failed to register run",
        )

    background_tasks.add_task(dispatcher, run_id, body.inputs)
    logger.info(
        "agents.run dispatched",
        extra={
            "run_id": run_id,
            "agent": body.agent,
            "contact_id": contact_id,
            "triggered_by": body.triggered_by,
        },
    )
    return AgentRunResponse(run_id=run_id, status="pending")


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    x_chawq_secret: str | None = Header(
        default=None, alias=CHAWQ_SECRET_HEADER
    ),
) -> dict[str, Any]:
    _verify_chawq_shared_secret(x_chawq_secret)
    doc = get_agent_run(run_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="run not found",
        )
    return doc
