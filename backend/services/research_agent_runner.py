"""
Research Agent runner — orchestration layer.

Composes the three side-effects of a Research Agent run into one safe-to-
call entry point:
  1. services.research_agent.runner.run()    — the model call (web_search + web_fetch)
  2. services.research_agent.drive_sync.upload_brief() — the Drive write (.docx + .json)
  3. services.firestore.put_agent_run()      — the agent_runs row

Mirrors the shape of services/email_drafter_runner.py so the dispatcher
behind POST /agents/run can BackgroundTask-add either runner without
restructuring the call site.

The "many steps" of the Research Agent (LOBBY-1, PW-3, S1-4, S3-PREP, etc.)
are prompt YAMLs under backend/prompts/research_agent/<TYPE>/v<n>.yaml.
This runner picks the YAML at call time from the `research_type` field in
the inputs dict — adding a new research type means dropping a YAML in.
No dispatcher, runner, or schema change.

Failure model: the runner returns a typed result with `status` set to
`completed | partial | failed` so the caller can decide what to surface
without parsing exceptions. The model call and the Drive upload each get
their own try/except — a Drive write failure shouldn't lose the brief or
the agent_runs log.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from core.settings import get_settings
from services.firestore.client import put_agent_run
from services.research_agent.runner import run as run_research_core
from services.research_agent.schema import ResearchBrief

logger = logging.getLogger(__name__)


PROMPTS_DIR = Path(__file__).parent.parent / "prompts" / "research_agent"

RunStatus = Literal["completed", "partial", "failed"]


@dataclass
class ResearchAgentRunResult:
    """
    Result of one orchestrated Research Agent run. `status` tells the
    caller the overall outcome; the per-step fields tell you which side-
    effects actually landed.
    """

    run_id: str
    research_type: str
    contact_id: Optional[str]
    status: RunStatus

    # Validated typed brief from the model. Present whenever the model
    # call itself succeeded. None only when the agent blew up.
    brief: Optional[ResearchBrief] = None

    # Drive upload side-effect. Both .docx and .json land; the workbook
    # reads the .docx for inline render, the .json is the structured
    # backup used by downstream ingest + the Scoring Agent's confidence
    # filter.
    drive_docx_file_id: Optional[str] = None
    drive_docx_web_link: Optional[str] = None
    drive_json_file_id: Optional[str] = None
    drive_json_web_link: Optional[str] = None
    drive_error: Optional[str] = None

    # Top-level error message if the model call itself failed.
    error: Optional[str] = None

    # Token + tool usage from the model call, for cost tracking.
    model: Optional[str] = None
    prompt_version: Optional[int] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    web_searches: Optional[int] = None
    web_fetches: Optional[int] = None
    elapsed_sec: Optional[float] = None

    # Datetime (not str) so Firestore stores them as native Timestamps —
    # queryable for "runs since X" and latency analysis.
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


def run_research_for_lead(
    inputs: dict[str, Any],
    *,
    skip_drive: bool = False,
    no_web_search: bool = False,
    run_id: str | None = None,
) -> ResearchAgentRunResult:
    """
    Run the Research Agent end-to-end for one lead.

    Args:
        inputs: dict carrying `research_type` (e.g. "PW-3") plus whatever
            fields that type's YAML declares under inputs.required and
            inputs.optional. Extra fields are tolerated — the YAML's
            resolve_inputs step picks only what it needs. Two control
            flags can also be passed on the inputs dict and are popped
            before resolve_inputs runs: `_no_web_search` (bool) and
            `_skip_drive` (bool). Useful for cheap end-to-end smoke
            tests over the public API.
        skip_drive: skip Drive upload. Useful for smoke tests where you
            want to inspect the brief without writing to Drive.
        no_web_search: pass through to the core runner. When True, the
            runner skips web_search + web_fetch tools and tells the
            model to keep the output small — a cheap pipeline test.
        run_id: optional run_id to use. When POST /agents/run has already
            written a pending stub, the dispatcher passes that run_id here
            so the terminal write merges into the same doc. When None,
            the runner generates its own UUID.

    Returns:
        ResearchAgentRunResult — never raises. Failures are captured on
        the result object so a BackgroundTask runner can log + move on.
    """
    run_id = run_id or str(uuid.uuid4())
    started_at = datetime.now(tz=timezone.utc)

    # Pull control flags off the inputs dict, defaulting to the function
    # kwargs. Lets callers flip cheap-mode without an envelope schema
    # change.
    if isinstance(inputs.get("_no_web_search"), bool):
        no_web_search = inputs["_no_web_search"]
    if isinstance(inputs.get("_skip_drive"), bool):
        skip_drive = inputs["_skip_drive"]

    research_type = (inputs.get("research_type") or "").strip()
    contact_id = inputs.get("contact_id")

    log_extra = {
        "run_id": run_id,
        "research_type": research_type,
        "contact_id": contact_id,
        "agent": "research",
    }

    # Routing key — required. Without it we can't pick a YAML.
    if not research_type:
        finished_at = datetime.now(tz=timezone.utc)
        result = ResearchAgentRunResult(
            run_id=run_id,
            research_type="",
            contact_id=contact_id,
            status="failed",
            error="missing required input: research_type",
            started_at=started_at,
            finished_at=finished_at,
        )
        _safe_put_agent_run(result, inputs)
        return result

    yaml_path = PROMPTS_DIR / research_type / "v1.yaml"
    if not yaml_path.exists():
        finished_at = datetime.now(tz=timezone.utc)
        result = ResearchAgentRunResult(
            run_id=run_id,
            research_type=research_type,
            contact_id=contact_id,
            status="failed",
            error=f"unknown research_type '{research_type}' (no prompt at {yaml_path.name})",
            started_at=started_at,
            finished_at=finished_at,
        )
        _safe_put_agent_run(result, inputs)
        return result

    # The core runner expects a flat contact dict. Routing keys and
    # control flags are popped so resolve_inputs() doesn't try to match
    # them against the YAML's input schema.
    contact = {k: v for k, v in inputs.items() if k not in _ROUTING_KEYS}

    # 1. Model call.
    try:
        brief, meta = run_research_core(
            yaml_path,
            contact,
            no_web_search=no_web_search,
        )
    except Exception as exc:
        logger.exception("research_agent model call failed", extra=log_extra)
        finished_at = datetime.now(tz=timezone.utc)
        result = ResearchAgentRunResult(
            run_id=run_id,
            research_type=research_type,
            contact_id=contact_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
            started_at=started_at,
            finished_at=finished_at,
        )
        _safe_put_agent_run(result, inputs)
        return result

    logger.info(
        "research_agent model call complete",
        extra={
            **log_extra,
            "input_tokens": meta.get("input_tokens"),
            "output_tokens": meta.get("output_tokens"),
            "web_searches": meta.get("web_searches"),
            "web_fetches": meta.get("web_fetches"),
            "overall_confidence": brief.overall_confidence,
            "sources_consulted_count": len(brief.sources_consulted or []),
        },
    )

    drive_docx_file_id: Optional[str] = None
    drive_docx_web_link: Optional[str] = None
    drive_json_file_id: Optional[str] = None
    drive_json_web_link: Optional[str] = None
    drive_error: Optional[str] = None

    # 2. Drive upload (.docx + .json sibling).
    if skip_drive:
        logger.info("skipping drive upload (skip_drive=True)", extra=log_extra)
    else:
        try:
            from services.research_agent.drive_sync import (
                DEFAULT_FOLDER_ID,
                upload_brief,
            )

            settings = get_settings()
            folder_id = settings.drive_output_root_folder_id or DEFAULT_FOLDER_ID
            uploaded = upload_brief(brief, folder_id=folder_id)

            docx_meta = uploaded.get("docx") or {}
            drive_docx_file_id = docx_meta.get("id")
            drive_docx_web_link = docx_meta.get("webViewLink")

            json_meta = uploaded.get("json") or {}
            drive_json_file_id = json_meta.get("id")
            drive_json_web_link = json_meta.get("webViewLink")
        except Exception as exc:
            drive_error = f"{type(exc).__name__}: {exc}"
            logger.exception("research_agent drive upload failed", extra=log_extra)

    finished_at = datetime.now(tz=timezone.utc)

    # 3. Compute status. Model call already succeeded by here; status
    # reflects whether the Drive upload landed.
    if not skip_drive and drive_error:
        status: RunStatus = "partial"
    else:
        status = "completed"

    result = ResearchAgentRunResult(
        run_id=run_id,
        research_type=research_type,
        contact_id=contact_id,
        status=status,
        brief=brief,
        drive_docx_file_id=drive_docx_file_id,
        drive_docx_web_link=drive_docx_web_link,
        drive_json_file_id=drive_json_file_id,
        drive_json_web_link=drive_json_web_link,
        drive_error=drive_error,
        model=meta.get("model"),
        prompt_version=brief.prompt_version,
        input_tokens=meta.get("input_tokens"),
        output_tokens=meta.get("output_tokens"),
        web_searches=meta.get("web_searches"),
        web_fetches=meta.get("web_fetches"),
        elapsed_sec=meta.get("elapsed_sec"),
        started_at=started_at,
        finished_at=finished_at,
    )

    # 4. agent_runs log.
    _safe_put_agent_run(result, inputs)

    logger.info(
        "research_agent run complete",
        extra={
            **log_extra,
            "status": status,
            "drive_docx_file_id": drive_docx_file_id,
        },
    )
    return result


# ---------------------------------------------------------------------------
# GHL webhook entry — same shape as email_drafter's
# ---------------------------------------------------------------------------

def run_research_agent_for_ghl_payload(payload: dict[str, Any]) -> None:
    """
    Background-task entry that takes a raw GHL workflow webhook payload
    and runs the research orchestrator.

    Never raises — the webhook handler returned 202 before this function
    ran, so failures land in agent_runs + Cloud Logging rather than
    bubbling up. Signature matches `run_email_drafter_for_ghl_payload`
    so the DISPATCH_HANDLERS registry in app/routes/webhooks.py can
    register either uniformly.

    GHL workflow operators set an `agent` Custom Data field to
    `research` and a `research_type` field to one of the registered types
    (e.g. "PW-3"). All other fields on the payload are passed through to
    the runner as inputs — the YAML's resolve_inputs picks the ones it
    needs.
    """
    try:
        run_research_for_lead(payload)
    except Exception:
        logger.exception(
            "research_agent dispatch failed",
            extra={
                "payload_keys": (
                    sorted(payload.keys()) if isinstance(payload, dict) else None
                ),
                "research_type": (payload or {}).get("research_type"),
                "contact_id": (payload or {}).get("contact_id") or (payload or {}).get("id"),
            },
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Keys that live on the agents/webhook envelope, not on the contact
# dict the YAML's resolve_inputs sees. Popped before calling the core
# runner so it doesn't trip on unexpected fields. The underscore-prefixed
# entries are control flags (cheap-mode, skip-drive) consumed by the
# orchestrator and not exposed to the YAML.
_ROUTING_KEYS = {
    "research_type",
    "agent",
    "triggered_by",
    "_no_web_search",
    "_skip_drive",
}


def _safe_put_agent_run(
    result: ResearchAgentRunResult, inputs: dict[str, Any]
) -> None:
    """
    Best-effort write to the agent_runs collection. Logging-only on
    failure — the run itself is the source of truth, the Firestore
    record is an audit trail. set(merge=True) so a pending stub from
    POST /agents/run merges cleanly.
    """
    duration_seconds: Optional[float] = None
    if result.started_at and result.finished_at:
        duration_seconds = (
            result.finished_at - result.started_at
        ).total_seconds()

    record: dict[str, Any] = {
        "run_id": result.run_id,
        "agent": "research",
        "research_type": result.research_type,
        "contact_id": result.contact_id,
        "municipality": inputs.get("municipality_name") or inputs.get("contact_municipality"),
        "status": result.status,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "duration_seconds": duration_seconds,
        "drive_docx_file_id": result.drive_docx_file_id,
        "drive_docx_web_link": result.drive_docx_web_link,
        "drive_json_file_id": result.drive_json_file_id,
        "drive_json_web_link": result.drive_json_web_link,
        # Cowork workbook reads `drive_file_id` and `drive_web_link`
        # by convention (matches email_drafter); point them at the docx.
        "drive_file_id": result.drive_docx_file_id,
        "drive_web_link": result.drive_docx_web_link,
        "drive_error": result.drive_error,
        "error": result.error,
        "model": result.model,
        "prompt_version": result.prompt_version,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "web_searches": result.web_searches,
        "web_fetches": result.web_fetches,
        "elapsed_sec": result.elapsed_sec,
    }

    if result.brief is not None:
        record.update(
            {
                "overall_confidence": result.brief.overall_confidence,
                "sources_consulted_count": len(result.brief.sources_consulted or []),
                "municipality_name": result.brief.municipality_name,
                "triggering_event": result.brief.triggering_event,
            }
        )

    try:
        put_agent_run(result.run_id, record)
    except Exception:
        logger.exception(
            "agent_runs write failed — research run not logged",
            extra={
                "run_id": result.run_id,
                "research_type": result.research_type,
                "contact_id": result.contact_id,
            },
        )
