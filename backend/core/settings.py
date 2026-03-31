from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Google Cloud ---
    gcp_project_id: str
    gcp_location: str = "us-central1"

    # --- Vertex AI ---
    vertex_llm_model: str = "gemini-2.5-pro"
    vertex_embedding_model: str = "text-embedding-004"

    # --- Firestore ---
    firestore_leads_collection: str = "leads"
    firestore_tasks_collection: str = "tasks"
    firestore_transcripts_collection: str = "transcripts"
    firestore_artifacts_collection: str = "artifacts"
    firestore_feedback_collection: str = "feedback"

    # --- Google Drive / Gmail ---
    service_account_email: str
    shared_drive_folder_id: str
    target_inboxes: list[str] = [
        "emily@chawq.org",
        "admin@chawq.org",
        "tyler@chawq.org",
    ]

    # --- App ---
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
