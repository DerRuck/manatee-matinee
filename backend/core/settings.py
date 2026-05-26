"""
Application settings.

Loaded from environment variables (or a local .env file in development).
In Cloud Run, secrets (API keys) are injected via Secret Manager.

Any change to required fields should be reflected in .env.example.
"""
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- App ---
    app_name: str = "chawq-api"
    env: Literal["local", "dev", "staging", "prod"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # --- Google Cloud ---
    gcp_project_id: str = "chawq-manatee-matinee"
    gcp_location: str = "us-central1"

    # --- Anthropic / Claude ---
    # Pulled from Secret Manager in Cloud Run; .env locally.
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-4-6"
    claude_max_tokens: int = 4096

    # --- GoHighLevel ---
    # Private Integration Token (location-scoped). Secret Manager in prod.
    ghl_pit: str = ""
    ghl_location_id: str = ""
    ghl_base_url: str = "https://services.leadconnectorhq.com"
    ghl_api_version_header: str = "2021-07-28"  # Required header on every v2 call.
    ghl_webhook_secret: str = ""  # For verifying inbound webhook signatures.

    # --- Firestore ---
    firestore_contacts_collection: str = "contacts"
    firestore_agent_runs_collection: str = "agent_runs"
    firestore_feedback_collection: str = "feedback"
    firestore_prompt_versions_collection: str = "prompt_versions"
    # Ingestion: documents (one row per Drive file) + chunks (retrieval surface).
    # Names locked 2026-04-23 schema decision; supersede the earlier
    # `vector_chunks` placeholder which was scaffolding from before the lock.
    firestore_documents_collection: str = "documents"
    firestore_chunks_collection: str = "chunks"
    # Per-contact rollup the workbook UI reads to render the lead-prioritization
    # list. One row per contact_id, overwritten on each scoring run.
    firestore_contact_scores_collection: str = "contact_scores"
    # Per-sweep audit doc written by the daily scoring sweep.
    firestore_scoring_sweeps_collection: str = "scoring_sweeps"

    # --- Vector embeddings (Vertex) ---
    vertex_embedding_model: str = "text-embedding-005"
    vertex_embedding_dimensions: int = 768

    # --- Google Drive ---
    # Service account JSON path for local dev; ADC used in Cloud Run.
    drive_service_account_file: str = ""
    drive_watch_folder_id: str = ""
    drive_output_root_folder_id: str = ""  # Where agent outputs get mirrored.

    # --- Server ---
    host: str = "0.0.0.0"
    port: int = 8080  # Cloud Run default.

    @property
    def is_local(self) -> bool:
        return self.env == "local"


@lru_cache
def get_settings() -> Settings:
    """
    Cached settings accessor. Use via FastAPI dependency injection:

        from fastapi import Depends
        from core.settings import get_settings, Settings

        @router.get("/thing")
        def read_thing(settings: Settings = Depends(get_settings)):
            ...
    """
    return Settings()
