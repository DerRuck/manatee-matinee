"""
Vertex text-embedding-005 wrapper.

Public API:
  embed_texts(list[str], task=...) -> list[list[float]]
  embed_query(str) -> list[float]    # convenience for query-time

Auth strategy (mirrors services/drive/client.py):
  - In Cloud Run (K_SERVICE env set): default ADC returns the runtime SA.
  - Local dev: default ADC returns user OAuth creds; we impersonate
    chawq-api-runtime so the call hits Vertex with the SA's permissions
    (aiplatform.user role granted 2026-05-04). User just needs
    serviceAccountTokenCreator on the runtime SA — admin@chawq.org has it.

Task types:
  - RETRIEVAL_DOCUMENT at ingest (the default here)
  - RETRIEVAL_QUERY at query time

Batching: text-embedding-005 accepts up to ~250 inputs per request. We cap
at 100 to keep individual requests responsive and well under quota limits.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import vertexai
from google.auth import default, impersonated_credentials
from vertexai.language_models import TextEmbeddingInput, TextEmbeddingModel

from core.settings import get_settings

logger = logging.getLogger(__name__)


SA_EMAIL = "chawq-api-runtime@chawq-manatee-matinee.iam.gserviceaccount.com"
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


_initialized = False
_model: Optional[TextEmbeddingModel] = None


def _build_credentials():
    """Same impersonation strategy as services/drive/client.py."""
    if os.environ.get("K_SERVICE"):
        creds, _ = default()
        return creds

    source_creds, _ = default()
    return impersonated_credentials.Credentials(
        source_credentials=source_creds,
        target_principal=SA_EMAIL,
        target_scopes=[CLOUD_PLATFORM_SCOPE],
        lifetime=3600,
    )


def _ensure_init() -> None:
    """Initialize the Vertex SDK once per process."""
    global _initialized, _model
    if _initialized:
        return
    settings = get_settings()
    creds = _build_credentials()
    vertexai.init(
        project=settings.gcp_project_id,
        location=settings.gcp_location,
        credentials=creds,
    )
    _model = TextEmbeddingModel.from_pretrained(settings.vertex_embedding_model)
    _initialized = True
    logger.info(
        "vertex embeddings initialized",
        extra={
            "project": settings.gcp_project_id,
            "location": settings.gcp_location,
            "model": settings.vertex_embedding_model,
        },
    )


def embed_texts(
    texts: list[str],
    task: str = "RETRIEVAL_DOCUMENT",
    max_tokens_per_batch: int = 12_000,
) -> list[list[float]]:
    """
    Embed a list of texts. Returns the embeddings in the same order.

    Vertex text-embedding-005 has a 20,000-token cap on the TOTAL input
    across one request. We pack each batch up to ~12,000 ESTIMATED tokens
    (using a words*1.3 heuristic) before flushing. This leaves real headroom
    against the 20K cap even when actual tokens-per-word runs ~1.5-1.6
    (e.g., meeting transcripts with names, timestamps, disfluencies).

    The single-input cap is 2,048 tokens, which our 600-word chunker
    (~780 tokens) stays well under. Out-of-spec chunks will get a clear
    error from Vertex; we don't pre-truncate here.

    Empty list -> empty list.
    """
    _ensure_init()
    assert _model is not None  # for type checkers

    if not texts:
        return []

    out: list[list[float]] = []
    batch_texts: list[str] = []
    batch_tokens = 0

    def _flush() -> None:
        if not batch_texts:
            return
        inputs = [TextEmbeddingInput(text=t, task_type=task) for t in batch_texts]
        results = _model.get_embeddings(inputs)
        out.extend(r.values for r in results)
        batch_texts.clear()

    for text in texts:
        # Token estimate: word count * 1.3 (matches the chunker's heuristic).
        est_tokens = max(1, int(len(text.split()) * 1.3))

        # If adding this text would overflow the batch, flush first.
        if batch_texts and (batch_tokens + est_tokens > max_tokens_per_batch):
            _flush()
            batch_tokens = 0

        batch_texts.append(text)
        batch_tokens += est_tokens

    _flush()
    return out


def embed_query(text: str) -> list[float]:
    """Embed a single query string with task=RETRIEVAL_QUERY."""
    embeddings = embed_texts([text], task="RETRIEVAL_QUERY")
    return embeddings[0]
