"""
Health and readiness endpoints.

- /health is a liveness probe: just confirms the process is up.
- /ready is a readiness probe: confirms dependencies (Firestore, etc.) are reachable.
  Left minimal for now; deepen as dependencies are wired in.
"""
from fastapi import APIRouter

from core.settings import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/ready")
def ready() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "env": settings.env,
        "app": settings.app_name,
        "version": "0.1.0",
    }
