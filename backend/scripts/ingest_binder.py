"""
Ingest the C-HAWQ Proven Process Binder into Firestore.

Writes chunks to the `vector_chunks` collection on chawq-manatee-matinee,
embedding each with Vertex AI text-embedding-004 (768-dim, RETRIEVAL_DOCUMENT).
Idempotent: reruns upsert cleanly via deterministic chunk_id doc IDs.

Run from backend/:
    # Dry run — validates input, prints plan, embeds nothing
    python scripts/ingest_binder.py --source binder_chunks.json --dry-run

    # Single-chunk smoke test
    python scripts/ingest_binder.py --source binder_chunks.json --limit 1

    # Full ingestion
    python scripts/ingest_binder.py --source binder_chunks.json

    # Verify retrieval after ingestion
    python scripts/ingest_binder.py --verify --query "qualifying criteria for boil leads"

Auth: ADC via `gcloud auth application-default login`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Iterable

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "chawq-manatee-matinee")
LOCATION = os.environ.get("VERTEX_LOCATION", "us-central1")
COLLECTION = "chunks"
SOURCE_DOC = "proven_process_binder"
EMBEDDING_MODEL = "text-embedding-004"
EMBEDDING_DIM = 768
TASK_TYPE = "RETRIEVAL_DOCUMENT"

EMBED_BATCH_SIZE = 5
FIRESTORE_BATCH_SIZE = 50


def _section_path(chunk: dict) -> list[str]:
    parts = []
    if chunk.get("section"):
        parts.append(chunk["section"])
    if chunk.get("subsection") and chunk["subsection"] != chunk.get("section"):
        parts.append(chunk["subsection"])
    if chunk.get("title") and chunk["title"] not in parts:
        parts.append(chunk["title"])
    return parts


def _batched(it: list, n: int) -> Iterable[list]:
    for i in range(0, len(it), n):
        yield it[i:i + n]


def _estimate_cost(chunks: list[dict]) -> tuple[int, float]:
    total_chars = sum(c.get("char_count", len(c.get("text", ""))) for c in chunks)
    cost = (total_chars / 1000) * 0.000025  # text-embedding-004 per 1k chars (approx)
    return total_chars, cost


def ingest(source: Path, version: int, limit: int | None, dry_run: bool) -> None:
    chunks = json.loads(source.read_text(encoding="utf-8"))
    if limit:
        chunks = chunks[:limit]

    total_chars, est_cost = _estimate_cost(chunks)
    print(f"Source:     {source}")
    print(f"Project:    {PROJECT}")
    print(f"Collection: {COLLECTION}")
    print(f"Chunks:     {len(chunks)}")
    print(f"Chars:      {total_chars:,}  (est. cost ${est_cost:.4f})")

    if dry_run:
        print("\nDRY RUN — no embeddings, no Firestore writes.")
        print("\nFirst 3 chunks:")
        for c in chunks[:3]:
            print(f"  - {c['chunk_id']}  [{c['chunk_type']}]  "
                  f"step={c.get('step')}  rid={c.get('research_type_id')}")
        return

    from vertexai import init as vertex_init
    from vertexai.language_models import TextEmbeddingInput, TextEmbeddingModel
    from google.cloud import firestore

    vertex_init(project=PROJECT, location=LOCATION)
    model = TextEmbeddingModel.from_pretrained(EMBEDDING_MODEL)
    db = firestore.Client(project=PROJECT)

    print(f"\nEmbedding {len(chunks)} chunks...")
    t0 = time.time()
    embedded = 0

    for batch in _batched(chunks, EMBED_BATCH_SIZE):
        inputs = [TextEmbeddingInput(c["text"], TASK_TYPE) for c in batch]
        results = model.get_embeddings(inputs)
        for c, emb in zip(batch, results):
            c["_embedding"] = list(emb.values)
            if len(c["_embedding"]) != EMBEDDING_DIM:
                raise RuntimeError(
                    f"Unexpected embedding dim {len(c['_embedding'])} for {c['chunk_id']}"
                )
        embedded += len(batch)
        if embedded % 25 == 0 or embedded == len(chunks):
            elapsed = time.time() - t0
            rate = embedded / elapsed if elapsed > 0 else 0
            print(f"  embedded {embedded}/{len(chunks)}  ({elapsed:.1f}s, {rate:.1f}/sec)")

    print(f"\nWriting to Firestore...")
    written = 0
    t1 = time.time()

    for batch in _batched(chunks, FIRESTORE_BATCH_SIZE):
        wb = db.batch()
        for c in batch:
            doc_ref = db.collection(COLLECTION).document(c["chunk_id"])
            wb.set(doc_ref, {
                "text":             c["text"],
                "source_doc":       SOURCE_DOC,
                "source_version":   c["source_version"],
                "version":          version,
                "embedding":        c["_embedding"],
                "embedding_model":  EMBEDDING_MODEL,
                "section_path":     _section_path(c),
                "chunk_id":         c["chunk_id"],
                "chunk_type":       c["chunk_type"],
                "section":          c["section"],
                "subsection":       c.get("subsection"),
                "title":            c.get("title"),
                "step":             c.get("step"),
                "phase":            c.get("phase"),
                "research_type_id": c.get("research_type_id"),
                "char_count":       c.get("char_count", len(c["text"])),
                "ingested_at":      firestore.SERVER_TIMESTAMP,
            })
        wb.commit()
        written += len(batch)
        print(f"  wrote {written}/{len(chunks)}")

    total_time = time.time() - t0
    print(f"\nDone. Ingested {written} chunks in {total_time:.1f}s.")


def verify(query: str, top_k: int = 5, research_type_id: str | None = None) -> None:
    """Cosine-score all binder chunks against a query to sanity-check retrieval."""
    from vertexai import init as vertex_init
    from vertexai.language_models import TextEmbeddingInput, TextEmbeddingModel
    from google.cloud import firestore
    from google.cloud.firestore_v1.base_query import FieldFilter

    vertex_init(project=PROJECT, location=LOCATION)
    model = TextEmbeddingModel.from_pretrained(EMBEDDING_MODEL)
    [q_emb] = model.get_embeddings([TextEmbeddingInput(query, "RETRIEVAL_QUERY")])
    qv = q_emb.values

    db = firestore.Client(project=PROJECT)
    q = db.collection(COLLECTION).where(filter=FieldFilter("source_doc", "==", SOURCE_DOC))
    if research_type_id:
        q = q.where(filter=FieldFilter("research_type_id", "==", research_type_id))

    docs = list(q.stream())
    print(f"Loaded {len(docs)} chunks (source_doc={SOURCE_DOC}"
          + (f", rid={research_type_id}" if research_type_id else "") + ")")

    def cos(a: list, b: list) -> float:
        return sum(x * y for x, y in zip(a, b))

    scored = [
        (cos(qv, d.to_dict()["embedding"]), d.to_dict())
        for d in docs
        if d.to_dict().get("embedding")
    ]
    scored.sort(key=lambda t: -t[0])

    print(f"\nQuery: {query!r}")
    for score, data in scored[:top_k]:
        title = data.get("title") or data.get("subsection") or "(untitled)"
        rid = data.get("research_type_id") or "-"
        snippet = data.get("text", "")[:140].replace("\n", " ")
        print(f"  [{score:.3f}] {data.get('chunk_type', '?'):18s} "
              f"step={data.get('step') or '-'}  rid={rid:12s}  {title[:50]}")
        print(f"           {snippet}...")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="binder_chunks.json")
    ap.add_argument("--version", type=int, default=1)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--query", default="qualifying criteria for boil leads")
    ap.add_argument("--research-type-id", default=None)
    ap.add_argument("--top-k", type=int, default=5)
    args = ap.parse_args()

    if args.verify:
        verify(args.query, top_k=args.top_k, research_type_id=args.research_type_id)
    else:
        ingest(Path(args.source), version=args.version,
               limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
