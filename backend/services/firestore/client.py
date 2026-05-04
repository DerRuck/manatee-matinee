"""
Firestore client.

Collections (per settings.py):
  - contacts
  - agent_runs       (one document per agent invocation)
  - feedback
  - prompt_versions
  - vector_chunks    (native Firestore vector search, V1 strategy)

Sprint demo scope: just put_agent_run(). Other collections layer in as the
agents that need them ship.

Auth strategy: the default ADC creds work in both environments without
impersonation. Locally, your user has Owner-level project access; in Cloud
Run, the runtime SA has roles/datastore.user (per the SA inventory memory).
Drive is the special case that needs impersonation, not Firestore.
"""
from __future__ import annotations

import logging
from typing import Any

from google.cloud import firestore

from core.settings import Settings, get_settings

logger = logging.getLogger(__name__)


_client: firestore.Client | None = None


def _get_client() -> firestore.Client:
    """Lazy-build (and cache) the Firestore client for the configured project."""
    global _client
    if _client is None:
        settings = get_settings()
        _client = firestore.Client(project=settings.gcp_project_id)
    return _client


def put_agent_run(run_id: str, record: dict[str, Any]) -> None:
    """
    Upsert one row in the `agent_runs` collection, keyed by run_id.

    The record dict shape is intentionally not enforced here — the runner that
    composes it owns the schema. Document ID = run_id for direct lookup later.
    """
    client = _get_client()
    settings = get_settings()
    collection = settings.firestore_agent_runs_collection
    client.collection(collection).document(run_id).set(record)


class FirestoreRepo:
    """Stateful wrapper for places that prefer object-style access. Sprint 2+."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._db = None

    async def put_agent_run(self, run_id: str, record: dict) -> None:
        raise NotImplementedError("Use module-level put_agent_run() for now.")

    async def get_agent_run(self, run_id: str) -> dict | None:
        raise NotImplementedError("Wire when something actually reads agent_runs.")
