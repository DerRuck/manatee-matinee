"""
Agent trigger endpoints.

The frontend (and eventually internal workflows) hit these to kick off
agent runs. All handlers return a run_id immediately and do the real work
in a background task / Cloud Task queue.

V1 agents (per sprint plan):
  - email_drafter
  - deep_research
  - presentation_outliner
  - letter

Handlers are thin placeholders in Sprint 1; wired in Sprints 2-3.
"""
import logging
import uuid
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter()


AgentName = Literal[
    "email_drafter",
    "deep_research",
    "presentation_outliner",
    "letter",
]


class AgentRunRequest(BaseModel):
    agent: AgentName
    contact_id: str = Field(..., description="GHL contact ID (or Firestore doc ID).")
    payload: dict = Field(
        default_factory=dict,
        description="Agent-specific inputs (e.g. letter type, tone overrides).",
    )


class AgentRunResponse(BaseModel):
    run_id: str
    agent: AgentName
    contact_id: str
    status: Literal["queued"] = "queued"


@router.post(
    "/run",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AgentRunResponse,
)
async def run_agent(
    req: AgentRunRequest,
    background_tasks: BackgroundTasks,
) -> AgentRunResponse:
    run_id = str(uuid.uuid4())

    logger.info(
        "agent run queued",
        extra={"run_id": run_id, "agent": req.agent, "contact_id": req.contact_id},
    )

    # TODO: write agent_run record to Firestore (status=queued).
    # TODO: dispatch to the right agent via a registry; for now, no-op.
    # background_tasks.add_task(AGENT_REGISTRY[req.agent].run, run_id, req)

    return AgentRunResponse(
        run_id=run_id, agent=req.agent, contact_id=req.contact_id
    )


@router.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict:
    # TODO: fetch agent_run from Firestore.
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="agent_runs lookup not wired yet; Sprint 2 task.",
    )
