"""
Ingestion library — single-file and bulk ingest of Drive content into Firestore.

Public API:
    ingest_one_drive_file(file_id, source_hint=None)  -- single-file entry point
    IngestStats, IngestDecision                       -- per-run + per-file shapes
    SOURCE_CONFIGS                                    -- registered source resolvers

Lower-level pieces (resolvers, header parsing) are also importable from
their submodules when needed.
"""
from ingestion.orchestrator import ingest_one_drive_file, IngestStats
from ingestion.resolvers import IngestDecision, SOURCE_CONFIGS, SourceConfig

__all__ = [
    "ingest_one_drive_file",
    "IngestStats",
    "IngestDecision",
    "SourceConfig",
    "SOURCE_CONFIGS",
]
