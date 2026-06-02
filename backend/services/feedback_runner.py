"""
Feedback agent runner — orchestration layer.

Composes the side-effects of one feedback capture into a single safe-to-call
entry point, mirroring services/email_drafter_runner.py so the /agents/run
dispatcher can BackgroundTask-add it without restructuring the call site:

  1. resolve the original deliverable run (agent_runs[original_run_id])
  2. diff extraction — when the reviewer supplied an edit, read the original
     deliverable text from Drive and unified-diff it against the edit
  3. FeedbackAgent.run_for_feedback()  — the categorization model call
  4. put_feedback()                    — the `feedback` record (schema link)
  5. link_feedback_to_run()            — back-link feedback_ids on the run
  6. put_agent_run()                   — the feedback run's own agent_runs row

The feedback run is its own agent_runs row (own run_id). `inputs["run_id"]`
names the ORIGINAL deliverable run this feedback is about — that pointer,
stored on the feedback record as `original_run_id` and mirrored back onto the
deliverable as `feedback_ids`, is the schema link the loop is built around.

Failure model: returns a typed result with `status` in
{completed | partial | failed} so the caller decides what to surface without
parsing exceptions. The categorization is the core; the Drive read and the
two Firestore writes are each isolated so one failing degrades to "partial"
rather than losing the classification.
"""
from __future__ import annotations

import difflib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from agents.feedback_agent import FeedbackAgent, FeedbackInput, FeedbackResult
from services.drive.client import download_file_as_text, get_file_metadata
from services.firestore.client import (
    get_agent_run,
    link_feedback_to_run,
    put_agent_run,
    put_feedback,
)

logger = logging.getLogger(__name__)


RunStatus = Literal["completed", "partial", "failed"]


@dataclass
class FeedbackRunResult:
    """
    Result of one orchestrated feedback run. `status` is the easy flag for
    callers; the per-step fields tell you which side-effects actually landed.
    """

    run_id: str  # the feedback run's own id
    original_run_id: str
    status: RunStatus

    # The classification. Present whenever status != "failed".
    result: Optional[FeedbackResult] = None

    # Resolution of the deliverable being reviewed.
    contact_id: Optional[str] = None
    original_agent: Optional[str] = None
    drive_file_id: Optional[str] = None
    original_run_found: bool = False

    # Diff extraction.
    diff: Optional[str] = None
    diff_error: Optional[str] = None

    # Firestore side-effects.
    feedback_written: bool = False
    link_written: bool = False
    firestore_error: Optional[str] = None

    # Top-level error if the categorization model call itself failed.
    error: Optional[str] = None

    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    # Non-fatal notes surfaced to the caller (e.g. "original run had no
    # drive_file_id — diff skipped").
    warnings: list[str] = field(default_factory=list)


def run_feedback_for_lead(
    inputs: dict[str, Any],
    *,
    run_id: str | None = None,
) -> FeedbackRunResult:
    """
    Run the feedback agent end-to-end for one reviewer reaction.

    Args:
        inputs: the agent inputs from POST /agents/run. Expected keys:
            - run_id (str):       the ORIGINAL deliverable run this feedback
                                  is about. Accepts `original_run_id` too.
            - reaction (str):     approved | edits_requested |
                                  rerun_requested | rejected.
            - note (str):         the reviewer's free-text comment (optional).
            - revised_text (str): the reviewer's edited version, when they
                                  supplied one — triggers diff extraction.
            - contact_id (str):   optional; falls back to the original run's.
            - agent_type (str):   optional; falls back to the original run's
                                  `agent` field.
        run_id: the feedback run's own id, passed by the dispatcher behind
            POST /agents/run so the terminal write merges into the pending
            stub. When None (direct/CLI call), a fresh UUID is generated.

    Returns:
        FeedbackRunResult — never raises. Failures are captured on the result
        object so a BackgroundTask runner can log + move on.
    """
    feedback_run_id = run_id or str(uuid.uuid4())
    started_at = datetime.now(tz=timezone.utc)

    original_run_id = (
        inputs.get("run_id") or inputs.get("original_run_id") or ""
    )
    reaction = inputs.get("reaction") or "edits_requested"
    note = inputs.get("note") or ""
    revised_text = inputs.get("revised_text")

    log_extra = {
        "run_id": feedback_run_id,
        "original_run_id": original_run_id,
        "agent": "feedback",
    }

    warnings: list[str] = []

    # 1. Resolve the original deliverable run.
    original_run: dict[str, Any] | None = None
    if original_run_id:
        try:
            original_run = get_agent_run(original_run_id)
        except Exception:
            logger.exception("feedback: failed to read original run", extra=log_extra)
    else:
        warnings.append("no original run_id supplied — feedback not linked to a deliverable")

    original_run_found = original_run is not None
    contact_id = inputs.get("contact_id") or (
        original_run.get("contact_id") if original_run else None
    )
    agent_type = inputs.get("agent_type") or (
        original_run.get("agent") if original_run else None
    )
    drive_file_id = original_run.get("drive_file_id") if original_run else None
    if original_run_id and not original_run_found:
        warnings.append(f"original run {original_run_id} not found in agent_runs")

    # 2. Diff extraction — only when the reviewer supplied an edit.
    diff: Optional[str] = None
    original_text: Optional[str] = None
    diff_error: Optional[str] = None
    if revised_text:
        if not drive_file_id:
            diff_error = "original run has no drive_file_id — cannot read original to diff"
            warnings.append(diff_error)
        else:
            try:
                meta = get_file_metadata(drive_file_id)
                if not meta:
                    diff_error = f"drive file {drive_file_id} not found — diff skipped"
                    warnings.append(diff_error)
                else:
                    original_text = download_file_as_text(
                        drive_file_id, meta.get("mimeType", ""), meta.get("name")
                    )
                    if original_text is None:
                        diff_error = (
                            f"drive file {drive_file_id} mime "
                            f"{meta.get('mimeType')} not text-extractable — diff skipped"
                        )
                        warnings.append(diff_error)
                    else:
                        diff = _unified_diff(original_text, revised_text)
            except Exception as exc:
                diff_error = f"{type(exc).__name__}: {exc}"
                logger.exception("feedback: diff extraction failed", extra=log_extra)

    # 3. Categorization model call. This is the core — if it fails the run
    # failed; everything above degrades to warnings.
    try:
        agent = FeedbackAgent(version=1)
        result = agent.run_for_feedback(
            FeedbackInput(
                original_run_id=original_run_id,
                reaction=reaction,
                note=note,
                contact_id=contact_id,
                agent_type=agent_type,
                original_text=original_text,
                diff=diff,
            )
        )
    except Exception as exc:
        logger.exception("feedback: categorization failed", extra=log_extra)
        finished_at = datetime.now(tz=timezone.utc)
        run_result = FeedbackRunResult(
            run_id=feedback_run_id,
            original_run_id=original_run_id,
            status="failed",
            contact_id=contact_id,
            original_agent=agent_type,
            drive_file_id=drive_file_id,
            original_run_found=original_run_found,
            diff=diff,
            diff_error=diff_error,
            error=f"{type(exc).__name__}: {exc}",
            started_at=started_at,
            finished_at=finished_at,
            warnings=warnings,
        )
        _safe_put_agent_run(run_result, reaction=reaction, note=note)
        return run_result

    logger.info(
        "feedback categorization complete",
        extra={
            **log_extra,
            "categories": result.categories,
            "sentiment": result.sentiment,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
        },
    )

    # 4 + 5. Persist the feedback record (schema link) and back-link it onto
    # the deliverable run. Isolated so a write failure degrades to "partial"
    # rather than losing the classification.
    feedback_written = False
    link_written = False
    firestore_error: Optional[str] = None
    feedback_record = _build_feedback_record(
        feedback_run_id=feedback_run_id,
        original_run_id=original_run_id,
        contact_id=contact_id,
        agent_type=agent_type,
        drive_file_id=drive_file_id,
        reaction=reaction,
        note=note,
        diff=diff,
        diff_error=diff_error,
        result=result,
        created_at=started_at,
    )
    try:
        put_feedback(feedback_run_id, feedback_record)
        feedback_written = True
    except Exception as exc:
        firestore_error = f"put_feedback: {type(exc).__name__}: {exc}"
        logger.exception("feedback: put_feedback failed", extra=log_extra)

    if original_run_found:
        try:
            link_feedback_to_run(original_run_id, feedback_run_id)
            link_written = True
        except Exception as exc:
            firestore_error = (
                f"{firestore_error + '; ' if firestore_error else ''}"
                f"link_feedback_to_run: {type(exc).__name__}: {exc}"
            )
            logger.exception("feedback: link_feedback_to_run failed", extra=log_extra)

    finished_at = datetime.now(tz=timezone.utc)

    # 6. Compute status. Model call succeeded by here. "partial" if any
    # expected side-effect didn't land: a caller-named original run that
    # didn't resolve (no schema link possible), a requested diff that
    # couldn't be produced, a failed feedback write, or a failed back-link.
    degraded = (
        (original_run_id and not original_run_found)
        or (revised_text and diff is None)
        or not feedback_written
        or (original_run_found and not link_written)
    )
    status: RunStatus = "partial" if degraded else "completed"

    run_result = FeedbackRunResult(
        run_id=feedback_run_id,
        original_run_id=original_run_id,
        status=status,
        result=result,
        contact_id=contact_id,
        original_agent=agent_type,
        drive_file_id=drive_file_id,
        original_run_found=original_run_found,
        diff=diff,
        diff_error=diff_error,
        feedback_written=feedback_written,
        link_written=link_written,
        firestore_error=firestore_error,
        started_at=started_at,
        finished_at=finished_at,
        warnings=warnings,
    )
    _safe_put_agent_run(run_result, reaction=reaction, note=note)

    logger.info(
        "feedback run complete",
        extra={**log_extra, "status": status, "feedback_written": feedback_written},
    )
    return run_result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unified_diff(original: str, revised: str) -> str:
    """
    Unified diff of original → revised, line-oriented. Empty string when the
    two are identical (reviewer edited nothing substantive).
    """
    return "\n".join(
        difflib.unified_diff(
            original.splitlines(),
            revised.splitlines(),
            fromfile="original",
            tofile="revised",
            lineterm="",
        )
    )


def _build_feedback_record(
    *,
    feedback_run_id: str,
    original_run_id: str,
    contact_id: Optional[str],
    agent_type: Optional[str],
    drive_file_id: Optional[str],
    reaction: str,
    note: str,
    diff: Optional[str],
    diff_error: Optional[str],
    result: FeedbackResult,
    created_at: datetime,
) -> dict[str, Any]:
    """
    The `feedback` collection row. `original_run_id`, `contact_id`, and
    `drive_file_id` are the schema link — they tie this record to the exact
    deliverable that was reviewed. The classification fields are flat at the
    top level so a query like "all negative tone feedback" needs no nesting.
    """
    return {
        "feedback_id": feedback_run_id,
        "original_run_id": original_run_id,
        "contact_id": contact_id,
        "original_agent": agent_type,
        "drive_file_id": drive_file_id,
        "reaction": reaction,
        "note": note,
        "categories": result.categories,
        "sentiment": result.sentiment,
        "summary": result.summary,
        "actionable": result.actionable,
        "has_diff": bool(diff),
        "diff": diff,
        "diff_error": diff_error,
        "model": result.model,
        "prompt_version": result.prompt_version,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "created_at": created_at,
    }


def _safe_put_agent_run(
    run_result: FeedbackRunResult, *, reaction: str, note: str
) -> None:
    """
    Best-effort terminal write to agent_runs for the feedback run itself.
    Merges onto the pending stub written by POST /agents/run. Logging-only
    on failure — the `feedback` record is the source of truth.
    """
    duration_seconds: Optional[float] = None
    if run_result.started_at and run_result.finished_at:
        duration_seconds = (
            run_result.finished_at - run_result.started_at
        ).total_seconds()

    record: dict[str, Any] = {
        "run_id": run_result.run_id,
        "agent": "feedback",
        "agent_version": 1,
        "contact_id": run_result.contact_id,
        "status": run_result.status,
        "original_run_id": run_result.original_run_id,
        "original_agent": run_result.original_agent,
        "drive_file_id": run_result.drive_file_id,
        "reaction": reaction,
        "note": note,
        "feedback_id": run_result.run_id,
        "feedback_written": run_result.feedback_written,
        "link_written": run_result.link_written,
        "diff_error": run_result.diff_error,
        "firestore_error": run_result.firestore_error,
        "error": run_result.error,
        "warnings": run_result.warnings,
        "started_at": run_result.started_at,
        "finished_at": run_result.finished_at,
        "duration_seconds": duration_seconds,
    }
    if run_result.result:
        record.update(
            {
                "categories": run_result.result.categories,
                "sentiment": run_result.result.sentiment,
                "summary": run_result.result.summary,
                "actionable": run_result.result.actionable,
                "model": run_result.result.model,
                "prompt_version": run_result.result.prompt_version,
                "input_tokens": run_result.result.input_tokens,
                "output_tokens": run_result.result.output_tokens,
            }
        )
    try:
        put_agent_run(run_result.run_id, record)
    except Exception:
        logger.exception(
            "agent_runs write failed — feedback run not logged",
            extra={"run_id": run_result.run_id},
        )
