# Dockerfile for the C-HAWQ remote MCP server.
#
# Built and deployed by mcp/cloudbuild.dev.yaml (and mcp/cloudbuild.yaml once
# prod is wired). Runs on Cloud Run; listens on the PORT env var that Cloud
# Run injects (default 8080).

FROM python:3.12-slim

# Avoid creating .pyc files and force stdout/stderr to flush so logs surface
# in Cloud Logging without buffering.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Only ca-certificates is strictly required for outbound HTTPS to the agent
# API. Kept minimal so the image stays small and the cold-start is fast.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py auth.py ./

# Cloud Run sets PORT itself; this is the local-default fallback.
ENV PORT=8080
EXPOSE 8080

CMD ["python", "server.py"]
