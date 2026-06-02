"""
FastAPI application entry point.

Run locally:
    cd backend
    uvicorn app.main:app --reload --port 8080

Run in Cloud Run: the Dockerfile invokes `uvicorn app.main:app` directly;
the image is built from this `backend/` directory.
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

# Load .env from repo root BEFORE anything else imports settings or
# instantiates SDK clients (e.g. anthropic.Anthropic() reads ANTHROPIC_API_KEY
# from os.environ at construct time). In Cloud Run there's no .env file and
# this is a no-op — env vars come from --set-env-vars / Secret Manager bindings.
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routes import agents, contacts, gmail, health, sync, webhooks
from core.logging import configure_logging
from core.settings import get_settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    configure_logging()
    settings = get_settings()
    logger.info(
        "chawq-api starting",
        extra={"env": settings.env, "gcp_project": settings.gcp_project_id},
    )
    yield
    # Shutdown
    logger.info("chawq-api shutting down")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="C-HAWQ API",
        description=(
            "FastAPI backend for the C-HAWQ AI System. "
            "Handles Drive ingestion webhooks, GHL bidirectional sync, "
            "and agent orchestration."
        ),
        version="0.1.0",
        lifespan=lifespan,
        # Docs only in non-prod; disable in prod to avoid leaking internals.
        docs_url="/docs" if settings.env != "prod" else None,
        redoc_url=None,
    )

    # CORS: tighten this when the frontend origin is nailed down.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.is_local else [],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routers
    app.include_router(health.router)
    app.include_router(webhooks.router, prefix="/webhooks", tags=["webhooks"])
    app.include_router(agents.router, prefix="/agents", tags=["agents"])
    app.include_router(sync.router, prefix="/sync", tags=["sync"])
    app.include_router(contacts.router, prefix="/contacts", tags=["contacts"])
    app.include_router(gmail.router, prefix="/gmail", tags=["gmail"])

    return app


app = create_app()
