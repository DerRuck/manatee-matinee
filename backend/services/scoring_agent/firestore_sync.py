"""Persist ScoringResults to Firestore.

The workbook UI reads scores in two patterns:

  1. The lead-prioritization list reads ONE document per contact —
     the latest score — sorted by lead_heat_score DESC. This needs to
     be a single-read fetch, so we maintain a `contact_scores`
     collection keyed by contact_id with the most recent ScoringResult.

  2. The contact detail view reads the full score history for one
     contact — written to `agent_runs` keyed by run_id (same convention
     as research + presentation agents).

Both writes are idempotent. Re-running the scoring agent for the same
contact overwrites contact_scores[contact_id] and adds a new agent_runs
row.

A small isolated helper so tests can patch the Firestore client without
hitting the network.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from services.scoring_agent.schema import ScoringResult

logger = logging.getLogger(__name__)


def persist_score(result: ScoringResult, meta: dict[str, Any]) -> None:
    """Write the score to BOTH agent_runs and contact_scores.

    Never raises — failures are logged so a downstream consumer (workbook
    UI, dispatcher) can decide what to do without losing the score result.
    """
    record = _build_agent_run_record(result, meta)

    try:
        from services.firestore.client import put_agent_run
        put_agent_run(result.run_id, record)
    except Exception:
        logger.exception(
            "scoring firestore_sync: put_agent_run failed",
            extra={"run_id": result.run_id, "contact_id": result.contact_id},
        )

    try:
        _upsert_contact_score(result, meta)
    except Exception:
        logger.exception(
            "scoring firestore_sync: contact_scores upsert failed",
            extra={"contact_id": result.contact_id},
        )


def _build_agent_run_record(
    result: ScoringResult,
    meta: dict[str, Any],
) -> dict[str, Any]:
    findings = result.findings
    return {
        "run_id":            result.run_id,
        "agent":             "scoring",
        "score_type_id":     result.score_type_id,
        "prompt_version":    result.prompt_version,
        "contact_id":        result.contact_id,
        "municipality_name": result.municipality_name,
        "generated_at":      result.generated_at,
        "finished_at":       datetime.now(tz=timezone.utc),
        "triggered_by":      result.triggered_by,
        "status":            "succeeded",
        "model":             meta.get("model"),
        "input_tokens":      meta.get("input_tokens"),
        "output_tokens":     meta.get("output_tokens"),
        "elapsed_sec":       meta.get("elapsed_sec"),
        # Flatten the workbook-UI surface so it shows up in agent_runs
        # without consumers having to navigate findings/score_type/etc.
        "current_step":      findings.current_step,
        "current_step_name": findings.current_step_name,
        "lead_heat":         findings.lead_heat,
        "lead_heat_score":   findings.lead_heat_score,
        "step_confidence":   findings.step_confidence,
        "ready_to_advance":  findings.ready_to_advance,
        "summary_one_line":  findings.summary_one_line,
        # Full result for forensic / audit reads
        "findings":          findings.model_dump(mode="json"),
        "notes":             result.notes,
    }


def _upsert_contact_score(result: ScoringResult, meta: dict[str, Any]) -> None:
    """Upsert the per-contact rollup the workbook UI reads.

    Doc id = contact_id, so there's exactly one row per contact at any time.
    """
    from services.firestore.client import _get_client
    from core.settings import get_settings

    client = _get_client()
    settings = get_settings()

    findings = result.findings
    payload = {
        "contact_id":        result.contact_id,
        "municipality_name": result.municipality_name,
        "score_type_id":     result.score_type_id,
        "prompt_version":    result.prompt_version,
        "latest_run_id":     result.run_id,
        "scored_at":         result.generated_at,
        "triggered_by":      result.triggered_by,
        "current_step":      findings.current_step,
        "current_step_name": findings.current_step_name,
        "current_phase":     findings.current_phase,
        "step_confidence":   findings.step_confidence,
        "ready_to_advance":  findings.ready_to_advance,
        "lead_heat":         findings.lead_heat,
        "lead_heat_score":   findings.lead_heat_score,
        "summary_one_line":  findings.summary_one_line,
        "blocker_count":     len(findings.next_step_blockers),
        "recommended_action_count": len(findings.recommended_actions),
        "days_since_last_signal":   findings.days_since_last_signal,
        # Keep the full findings here too so the workbook UI doesn't have to
        # do a second read into agent_runs for the detail view.
        "findings":          findings.model_dump(mode="json"),
        "model":             meta.get("model"),
    }
    # contact_scores is a new collection added for the workbook UI. Use a
    # fixed name (not in settings yet) — when we wire it in settings.py,
    # only this line changes.
    client.collection("contact_scores").document(result.contact_id).set(payload)
