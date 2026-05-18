"""
Pydantic models for the documents + chunks Firestore collections.

These models describe the shape of records BEFORE writing to Firestore.
At write time the embedding list[float] is wrapped in
`google.cloud.firestore_v1.vector.Vector(...)` so Firestore's `find_nearest`
can use it as a vector field. See client.put_chunks_bulk.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


# enum Adding values later is free; renaming/splitting
# existing ones forces reclassify + in-place chunk update.
#
# "other" is the default for files we ingest but can't classify
# confidently (e.g., PDFs attached to an email, lead docs whose name
# doesn't signal type). Filtering by document_type stays honest because
# "research_report" really means research report — uncategorized goes
# to "other" and can be reclassified in place when patterns clarify.
DocumentType = Literal[
    "email",
    "presentation",
    "meeting_notes",
    "research_report",
    "internal_policy",
    "project_plan",
    "letter",
    "image",
    "other",
]

IngestionStatus = Literal["pending", "processing", "completed", "failed"]

# Vertex text-embedding task types. Use RETRIEVAL_DOCUMENT at ingest,
# RETRIEVAL_QUERY at query time. See
# https://cloud.google.com/vertex-ai/generative-ai/docs/embeddings/task-types
EmbeddingTask = Literal[
    "RETRIEVAL_DOCUMENT",
    "RETRIEVAL_QUERY",
    "SEMANTIC_SIMILARITY",
    "CLASSIFICATION",
    "CLUSTERING",
]


class Document(BaseModel):
    """
    One row per Drive file. Tracks ingest state + canonical metadata.

    Document IDs equal Drive file IDs — stable, unique, and they make
    re-ingest idempotent (overwrite the same row, delete + rewrite chunks).
    """

    document_id: str
    drive_file_id: str
    drive_file_name: str
    drive_mime_type: str
    drive_modified_time: datetime
    drive_web_view_link: Optional[str] = None
    drive_parent_folder_id: Optional[str] = None

    # Ingestion lifecycle
    ingestion_status: IngestionStatus = "pending"
    parser_version: int = 1
    chunker_version: int = 1
    embedder_model: str = "text-embedding-005"
    embedder_version: str = "1"
    chunk_count: int = 0
    ingested_at: Optional[datetime] = None
    error: Optional[str] = None

    # Identity metadata. Copied down to each chunk at ingest time so the
    # chunks collection can be filtered without joins. Arrays even when
    # single-element so multi-entity docs (e.g., comparative reports) Just Work.
    document_type: DocumentType
    contact_id: list[str] = Field(default_factory=list)
    municipality: list[str] = Field(default_factory=list)
    project_name: list[str] = Field(default_factory=list)

    # Origin tag for the source folder. V1 values:
    #   plaud, leads, industry_context, email_data, iflytek
    # Used by retrieval to filter dev/prod data, by the ingester to pick
    # a per-source resolver, and by ops to count corpus size by source.
    data_source: Optional[str] = None


class Chunk(BaseModel):
    """
    One row per chunk. The retrieval surface for vector search.

    Chunk IDs are deterministic: f"{document_id}__{chunk_index:04d}".
    This makes re-ingest a clean delete-then-write cycle on the parent
    document's full chunk set.
    """

    chunk_id: str
    document_id: str
    chunk_index: int
    text: str

    # 768 dims (text-embedding-005). Stored as list[float] in this model;
    # client.put_chunks_bulk wraps it in firestore.Vector at write time.
    embedding: list[float]
    embedding_model: str = "text-embedding-005"
    embedding_task: EmbeddingTask = "RETRIEVAL_DOCUMENT"

    # Counts. token_count is an estimate from the chunker (V1 uses a
    # word-count heuristic); char_count is exact.
    token_count: int
    char_count: int

    # Identity metadata copied from the parent Document.
    document_type: DocumentType
    contact_id: list[str] = Field(default_factory=list)
    municipality: list[str] = Field(default_factory=list)
    project_name: list[str] = Field(default_factory=list)
    data_source: Optional[str] = None

    # Optional positional info. V1 leaves these empty; populate later when
    # the parser is smart enough to track headings or page numbers.
    heading_path: Optional[list[str]] = None
    page_number: Optional[int] = None
