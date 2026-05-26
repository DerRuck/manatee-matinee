"""Daily scoring sweep.

Iterates every "scoreable" contact and runs the Scoring Agent against each.
A single pass produces fresh `contact_scores` rows for the workbook UI to
read, with run_id-keyed audit trails in `agent_runs`.

The Cloud Scheduler hits POST /jobs/scoring/daily once a day, which calls
run_daily_sweep() from a FastAPI BackgroundTask. The same function powers
the CLI (scripts/run_daily_scoring.py) so cron-free runs stay on the same
code path.

Eligibility (default heuristic — tunable via kwargs):

  Include a contact when ALL of these hold:
    1. The contact_id row has at least one signal — a tag OR a prior
       agent_run. Bare-stub contacts produce thin cold scores; skipping
       them saves API budget for contacts the team can actually act on.
    2. The latest score (if any) is not `lost`. The Boil/Simmer/Stall
       framework treats `lost` as do-not-contact.
    3. The contact wasn't scored in the last `min_age_hours` hours.
       Default 18h gives the daily cron a 6h drift window without
       double-scoring.

Errors per contact are caught and recorded — one broken contact does not
abort the sweep. The function ALWAYS returns a SweepReport summarizing what
ran and what failed, even when every contact errored.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Report types
# ---------------------------------------------------------------------------

@dataclass
class ContactSweepOutcome:
    """One contact's outcome from a sweep — used for logging and the report.

    `status`:
      - 'scored'   : agent ran and the score was persisted
      - 'skipped'  : eligibility filter rejected the contact
      - 'failed'   : agent or persistence raised; error captured
    """
    contact_id: str
    status: str
    skipped_reason: str | None = None
    error: str | None = None
    lead_heat: str | None = None
    lead_heat_score: int | None = None
    elapsed_sec: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class SweepReport:
    sweep_id: str
    triggered_by: str
    started_at: datetime
    finished_at: datetime | None = None
    total_eligible: int = 0
    total_skipped: int = 0
    total_scored: int = 0
    total_failed: int = 0
    outcomes: list[ContactSweepOutcome] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Firestore-safe dict (datetimes preserved as datetime objects)."""
        return {
            "sweep_id":       self.sweep_id,
            "triggered_by":   self.triggered_by,
            "started_at":     self.started_at,
            "finished_at":    self.finished_at,
            "total_eligible": self.total_eligible,
            "total_skipped":  self.total_skipped,
            "total_scored":   self.total_scored,
            "total_failed":   self.total_failed,
            "outcomes": [
                {
                    "contact_id":      o.contact_id,
                    "status":          o.status,
                    "skipped_reason":  o.skipped_reason,
                    "error":           o.error,
                    "lead_heat":       o.lead_heat,
                    "lead_heat_score": o.lead_heat_score,
                    "elapsed_sec":     o.elapsed_sec,
                    "input_tokens":    o.input_tokens,
                    "output_tokens":   o.output_tokens,
                }
                for o in self.outcomes
            ],
        }


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

def _has_signal(contact: dict[str, Any]) -> bool:
    """A contact has signal when there's anything for the scorer to weigh.

    Mirrors the demo-readiness check in scripts/rank_contacts_for_scoring.py.
    Cheap inputs: tags, populated custom fields, a municipality field, or a
    full name. Prior agent_runs add signal too but those are checked separately
    so the heuristic doesn't need a join here.
    """
    if contact.get("tags"):
        return True
    if contact.get("city") or contact.get("companyName"):
        first = (contact.get("firstNameRaw") or contact.get("firstName") or "").strip()
        last = (contact.get("lastNameRaw") or contact.get("lastName") or "").strip()
        if first or last:
            return True
    for cf in contact.get("customFields") or []:
        if isinstance(cf, dict) and cf.get("value") not in (None, "", [], {}):
            return True
    return False


def _eligibility_check(
    contact: dict[str, Any],
    latest_score: dict[str, Any] | None,
    run_count: int,
    min_age_hours: int,
    skip_lost: bool,
) -> str | None:
    """Return None if eligible, or a short reason string if skipped."""
    if not _has_signal(contact) and run_count == 0:
        return "no_signal"

    if skip_lost and latest_score and latest_score.get("lead_heat") == "lost":
        return "lead_lost"

    if latest_score and min_age_hours > 0:
        scored_at = latest_score.get("scored_at")
        if isinstance(scored_at, datetime):
            scored_at = _to_utc(scored_at)
            cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=min_age_hours)
            if scored_at > cutoff:
                return "scored_recently"

    return None


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Sweep runner
# ---------------------------------------------------------------------------

def run_daily_sweep(
    max_contacts: int = 100,
    triggered_by: str = "daily",
    min_age_hours: int = 18,
    skip_lost: bool = True,
    score_type: str = "PIPELINE-SCORE",
    score_version: int = 1,
    dry_run: bool = False,
    persist_report: bool = True,
    on_outcome: Any = None,
    sweep_id: str | None = None,
) -> SweepReport:
    """Score every eligible contact once.

    Args:
        max_contacts: Hard cap on contacts scanned in one sweep. Cloud Run
            with default config can serve a few hundred sequentially; pick
            a value matched to your per-contact latency budget.
        triggered_by: Stamped onto every score and the sweep audit doc.
            'daily' for the cron, 'manual' from the CLI, 'webhook' if
            ever wired from a workflow.
        min_age_hours: Skip a contact when its latest score is younger than
            this. Default 18h leaves headroom under a 24h cron without
            double-scoring.
        skip_lost: Skip contacts whose latest heat is 'lost' (do-not-contact).
        score_type / score_version: Forwarded to ScoringAgent — change only
            if you have a non-PIPELINE-SCORE prompt to test in a sweep.
        dry_run: Build context, but skip the Claude call and the persist
            writeback. Eligibility filtering is exercised end-to-end so the
            report tells you what WOULD have been scored.
        persist_report: Write the SweepReport to the scoring_sweeps Firestore
            collection. Disable when running tests or one-off CLI dry-runs.
        on_outcome: Optional callable invoked with each ContactSweepOutcome
            as it lands — used by the HTTP background task to stream
            progress into the sweep doc without waiting for completion.

    Returns:
        The completed SweepReport, regardless of how many contacts errored.
    """
    sweep_id = sweep_id or str(uuid.uuid4())
    started_at = datetime.now(tz=timezone.utc)
    report = SweepReport(
        sweep_id=sweep_id,
        triggered_by=triggered_by,
        started_at=started_at,
    )

    logger.info(
        "scoring sweep starting",
        extra={
            "sweep_id": sweep_id,
            "max_contacts": max_contacts,
            "triggered_by": triggered_by,
            "dry_run": dry_run,
        },
    )

    if persist_report and not dry_run:
        _persist_sweep(report, status="started")

    try:
        contacts = _list_contacts(limit=max_contacts)
    except Exception as exc:
        logger.exception("scoring sweep contact listing failed")
        report.finished_at = datetime.now(tz=timezone.utc)
        if persist_report and not dry_run:
            _persist_sweep(report, status="failed", error=str(exc))
        raise

    report.total_eligible = len(contacts)

    for contact in contacts:
        contact_id = contact.get("id") or ""
        if not contact_id:
            continue

        outcome = _score_one(
            contact=contact,
            triggered_by=triggered_by,
            min_age_hours=min_age_hours,
            skip_lost=skip_lost,
            score_type=score_type,
            score_version=score_version,
            dry_run=dry_run,
        )
        report.outcomes.append(outcome)
        if outcome.status == "scored":
            report.total_scored += 1
        elif outcome.status == "skipped":
            report.total_skipped += 1
        elif outcome.status == "failed":
            report.total_failed += 1

        if on_outcome is not None:
            try:
                on_outcome(outcome)
            except Exception:
                logger.exception("on_outcome callback failed")

        if persist_report and not dry_run:
            _persist_sweep(report, status="in_progress")

    report.finished_at = datetime.now(tz=timezone.utc)

    if persist_report and not dry_run:
        _persist_sweep(report, status="completed")

    logger.info(
        "scoring sweep done",
        extra={
            "sweep_id": sweep_id,
            "scored": report.total_scored,
            "skipped": report.total_skipped,
            "failed": report.total_failed,
            "elapsed_sec": (report.finished_at - report.started_at).total_seconds(),
        },
    )
    return report


def _score_one(
    contact: dict[str, Any],
    triggered_by: str,
    min_age_hours: int,
    skip_lost: bool,
    score_type: str,
    score_version: int,
    dry_run: bool,
) -> ContactSweepOutcome:
    """Score one contact end-to-end. Catches everything to keep the sweep alive."""
    contact_id = contact.get("id") or ""
    t0 = time.time()

    try:
        latest_score = _get_latest_score(contact_id)
        run_count = _count_agent_runs(contact_id)
    except Exception as exc:
        logger.exception(
            "sweep eligibility lookup failed — treating as eligible",
            extra={"contact_id": contact_id},
        )
        latest_score = None
        run_count = 0

    skip_reason = _eligibility_check(
        contact, latest_score, run_count, min_age_hours, skip_lost,
    )
    if skip_reason:
        return ContactSweepOutcome(
            contact_id=contact_id, status="skipped", skipped_reason=skip_reason,
        )

    if dry_run:
        return ContactSweepOutcome(contact_id=contact_id, status="skipped",
                                   skipped_reason="dry_run")

    try:
        from services.scoring_agent.context_builder import build_scoring_context
        from agents.scoring_agent import ScoringAgent
        from services.scoring_agent.firestore_sync import persist_score

        context = build_scoring_context(contact_id, triggered_by=triggered_by)
        agent = ScoringAgent(score_type, version=score_version)
        result, meta = agent.run(context)
        persist_score(result, meta)
    except Exception as exc:
        logger.exception(
            "sweep contact scoring failed",
            extra={"contact_id": contact_id},
        )
        return ContactSweepOutcome(
            contact_id=contact_id, status="failed", error=str(exc),
            elapsed_sec=round(time.time() - t0, 2),
        )

    return ContactSweepOutcome(
        contact_id=contact_id,
        status="scored",
        lead_heat=result.findings.lead_heat,
        lead_heat_score=result.findings.lead_heat_score,
        elapsed_sec=round(time.time() - t0, 2),
        input_tokens=meta.get("input_tokens"),
        output_tokens=meta.get("output_tokens"),
    )


# ---------------------------------------------------------------------------
# Firestore wrappers — small enough that tests can patch them directly
# ---------------------------------------------------------------------------

def _list_contacts(limit: int) -> list[dict[str, Any]]:
    """All contacts up to `limit`. Sorted is not required — the sweep order
    is intentionally undefined."""
    from services.firestore.client import list_contacts as _list
    return _list(limit=limit)


def _get_latest_score(contact_id: str) -> dict[str, Any] | None:
    from services.firestore.scores import get_contact_score
    return get_contact_score(contact_id)


def _count_agent_runs(contact_id: str) -> int:
    """Cheap presence check — used only to keep no-signal contacts alive."""
    from services.firestore.client import _get_client
    from core.settings import get_settings

    client = _get_client()
    settings = get_settings()
    query = (
        client.collection(settings.firestore_agent_runs_collection)
        .where("contact_id", "==", contact_id)
        .limit(1)
    )
    return sum(1 for _ in query.stream())


def get_sweep_doc(sweep_id: str) -> dict[str, Any] | None:
    """Read one sweep audit doc from Firestore. None if missing.

    Module-level so the /jobs/scoring/sweeps/{id} route can call it
    without doing its own Firestore import — keeps the route easy to test
    by patching this function.
    """
    from services.firestore.client import _get_client
    from core.settings import get_settings

    client = _get_client()
    settings = get_settings()
    snap = (
        client.collection(settings.firestore_scoring_sweeps_collection)
        .document(sweep_id)
        .get()
    )
    if not snap.exists:
        return None
    doc = snap.to_dict() or {}
    doc.setdefault("sweep_id", snap.id)
    return doc


def _persist_sweep(report: SweepReport, status: str, error: str | None = None) -> None:
    """Upsert the sweep audit doc. Best-effort: failures are logged, not raised."""
    try:
        from services.firestore.client import _get_client
        from core.settings import get_settings

        client = _get_client()
        settings = get_settings()
        payload = report.as_dict()
        payload["status"] = status
        if error:
            payload["error"] = error
        client.collection(
            settings.firestore_scoring_sweeps_collection
        ).document(report.sweep_id).set(payload)
    except Exception:
        logger.exception(
            "sweep audit doc persist failed",
            extra={"sweep_id": report.sweep_id, "status": status},
        )
