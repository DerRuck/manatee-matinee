# syntax=docker/dockerfile:1.6
#
# C-HAWQ backend image.
# Target: Cloud Run (listens on $PORT, defaulting to 8080).
# Build from the repo root:
#   docker build -f backend/Dockerfile -t chawq-api:local backend/
# Run locally:
#   docker run --rm -p 8080:8080 --env-file backend/.env chawq-api:local

FROM python:3.12-slim AS base

# Prevent Python from writing .pyc files; unbuffered stdout for Cloud Logging.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System deps. Kept minimal; add only when a Python wheel is missing.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Install Python deps first to leverage Docker layer caching.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy source.
COPY . .

# Cloud Run injects PORT; default to 8080 for local docker run.
ENV PORT=8080
EXPOSE 8080

# Run without --reload in the image; local dev uses `uvicorn --reload` outside Docker.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
