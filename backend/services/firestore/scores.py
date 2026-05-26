"""Read access for the per-contact rollup the workbook UI renders.

Writes are owned by services/scoring_agent/firestore_sync.py — this module
is read-only. Two query shapes:

  list_contact_scores(...)  -> the lead-prioritization list (sorted by
                               lead_heat_score DESC, with light filters)
  get_contact_score(id)     -> the per-contact detail view
  list_score_history(id)    -> every scoring agent_run for one contact

Isolated as a small module so tests can patch _get_client without bringing
google-cloud-firestore into the import path.
"""
from __future__ import annotations

import logging
from typing import Any

from core.settings import get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-contact rollup (contact_scores)
# ---------------------------------------------------------------------------

def list_contact_scores(
    limit: int = 50,
    lead_heat: list[str] | None = None,
    current_step: int | None = None,
    ready_to_advance: bool | None = None,
    min_score: int | None = None,
    start_after_score: int | None = None,
) -> list[dict[str, Any]]:
    """Return contact_scores ordered by lead_heat_score DESC.

    Filters are AND'd together. Filters mapped to Firestore predicates:
      - lead_heat        -> field 'lead_heat' IN [...]   (≤30 values)
      - current_step     -> field 'current_step' ==
      - ready_to_advance -> field 'ready_to_advance' ==
      - min_score        -> field 'lead_heat_score' >=

    `start_after_score` paginates: pass the lead_heat_score of the last row
    in the previous page to fetch the next page.

    Note on indexes: composite queries (e.g. lead_heat + lead_heat_score
    DESC) need a Firestore composite index. firestore.indexes.json carries
    the workbook MVP set; combining additional filters may surface an
    index-missing error pointing to a console URL to create one.
    """
    from services.firestore.client import _get_client

    client = _get_client()
    settings = get_settings()

    query: Any = client.collection(settings.firestore_contact_scores_collection)

    if lead_heat:
        if len(lead_heat) == 1:
            query = query.where("lead_heat", "==", lead_heat[0])
        else:
            query = query.where("lead_heat", "in", lead_heat[:30])
    if current_step is not None:
        query = query.where("current_step", "==", current_step)
    if ready_to_advance is not None:
        query = query.where("ready_to_advance", "==", ready_to_advance)
    if min_score is not None:
        query = query.where("lead_heat_score", ">=", min_score)

    from google.cloud.firestore_v1 import Query  # local import to keep tests light

    query = query.order_by("lead_heat_score", direction=Query.DESCENDING)

    if start_after_score is not None:
        query = query.start_after({"lead_heat_score": start_after_score})

    query = query.limit(min(max(limit, 1), 200))

    out: list[dict[str, Any]] = []
    for snap in query.stream():
        doc = snap.to_dict() or {}
        doc.setdefault("contact_id", snap.id)
        out.append(doc)
    return out


def get_contact_score(contact_id: str) -> dict[str, Any] | None:
    """Latest score for one contact, or None if never scored."""
    from services.firestore.client import _get_client

    client = _get_client()
    settings = get_settings()
    snap = (
        client.collection(settings.firestore_contact_scores_collection)
        .document(contact_id)
        .get()
    )
    if not snap.exists:
        return None
    doc = snap.to_dict() or {}
    doc.setdefault("contact_id", snap.id)
    return doc


# ---------------------------------------------------------------------------
# Per-contact score history (agent_runs filtered to scoring)
# ---------------------------------------------------------------------------

def list_score_history(contact_id: str, limit: int = 25) -> list[dict[str, Any]]:
    """Recent scoring runs for one contact, newest first.

    Reads from agent_runs (where scoring runs are stored alongside research +
    presentation), filters in memory to agent='scoring'. The contact_id +
    finished_at composite index already exists for build_scoring_context;
    we reuse it here so a separate index isn't needed just for history.
    """
    from services.firestore.client import _get_client
    from google.cloud.firestore_v1 import Query

    client = _get_client()
    settings = get_settings()

    query = (
        client.collection(settings.firestore_agent_runs_collection)
        .where("contact_id", "==", contact_id)
        .order_by("finished_at", direction=Query.DESCENDING)
        .limit(min(max(limit, 1), 100) * 3)  # over-fetch, then filter
    )

    out: list[dict[str, Any]] = []
    for snap in query.stream():
        doc = snap.to_dict() or {}
        if doc.get("agent") != "scoring":
            continue
        doc.setdefault("run_id", snap.id)
        out.append(doc)
        if len(out) >= limit:
            break
    return out
