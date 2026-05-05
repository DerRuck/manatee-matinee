"""
Smoke test for the chunks vector index.

Embeds a query string, runs find_nearest against the `chunks` collection,
and prints the top results with similarity context.

Run from backend/ dir:
    python -m scripts.test_vector_query
    python -m scripts.test_vector_query --query "what concerns came up about Army Corps dredging?"
    python -m scripts.test_vector_query --query "..." --top-k 3

Pre-reqs:
  - Vector index on chunks.embedding is in state: READY
    (created via `gcloud firestore indexes composite create` -- task #13)
  - Some chunks already ingested (run scripts.ingest_demo_corpus first)
  - Same auth setup as the ingestion run: ADC as a user with TokenCreator
    on chawq-api-runtime, since vertex.py impersonates that SA for embeddings
"""
from __future__ import annotations

import argparse
import logging
import sys

from google.cloud import firestore
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector

from core.settings import get_settings
from services.embeddings.vertex import embed_query


logger = logging.getLogger("test_vector_query")


DEFAULT_QUERY = "What did we discuss about Rookery Bay water quality and dredging?"


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _truncate(text: str, max_chars: int = 240) -> str:
    """Single-line preview for terminal output."""
    text = " ".join(text.split())
    return text if len(text) <= max_chars else text[:max_chars] + "..."


def main() -> int:
    _setup_logging()

    parser = argparse.ArgumentParser(description="Vector search smoke test.")
    parser.add_argument("--query", default=DEFAULT_QUERY, help="Query text.")
    parser.add_argument("--top-k", type=int, default=5, help="How many chunks to return.")
    args = parser.parse_args()

    settings = get_settings()
    print(f"Query:  {args.query!r}")
    print(f"Top K:  {args.top_k}")
    print()

    # 1. Embed the query (RETRIEVAL_QUERY task type, not RETRIEVAL_DOCUMENT).
    print("Embedding query via Vertex text-embedding-005...")
    query_vector = embed_query(args.query)
    print(f"  vector dim = {len(query_vector)}")
    print()

    # 2. Run find_nearest against the chunks collection.
    print(f"Running find_nearest against `{settings.firestore_chunks_collection}`...")
    client = firestore.Client(project=settings.gcp_project_id)
    collection = client.collection(settings.firestore_chunks_collection)

    vec_query = collection.find_nearest(
        vector_field="embedding",
        query_vector=Vector(query_vector),
        distance_measure=DistanceMeasure.COSINE,
        limit=args.top_k,
    )

    snapshots = list(vec_query.get())
    if not snapshots:
        print("  No results. Either the chunks collection is empty or the vector")
        print("  index isn't READY yet. Check Firestore console.")
        return 1

    print(f"  got {len(snapshots)} result(s).")
    print()

    # 3. Pretty-print each hit.
    for rank, snap in enumerate(snapshots, start=1):
        data = snap.to_dict() or {}
        print(f"--- Result {rank} ---")
        print(f"  chunk_id:      {snap.id}")
        print(f"  document_id:   {data.get('document_id')}")
        print(f"  document_type: {data.get('document_type')}")
        print(f"  municipality:  {data.get('municipality')}")
        print(f"  chunk_index:   {data.get('chunk_index')}")
        print(f"  token_count:   {data.get('token_count')}")
        print(f"  text preview:  {_truncate(data.get('text', ''))}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
