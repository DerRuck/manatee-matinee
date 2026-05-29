"""Inbound auth middleware for the remote MCP server.

Validates the ``X-CHAWQ-MCP-Secret`` header on every request before the MCP
handler runs. Rejects with 401 if the header is missing or wrong.

Health-check paths (``/`` and ``/healthz``) stay open so Cloud Run's startup
and liveness probes don't need the secret.

The expected secret is captured at server startup and never re-read. To
rotate: update the Secret Manager value and redeploy. The Cloud Run service
will read the new value at next container start.
"""

from __future__ import annotations

import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


HEADER_NAME = "X-CHAWQ-MCP-Secret"
OPEN_PATHS = {"/", "/healthz"}


class McpSecretMiddleware(BaseHTTPMiddleware):
    """Reject any request that doesn't carry the shared secret in the header.

    Mounted in front of the FastMCP ASGI app via ``http_app(middleware=[...])``.
    Constant-time compare via ``hmac.compare_digest`` to avoid trivial timing
    side channels on the secret.
    """

    def __init__(self, app, expected_secret: str):
        super().__init__(app)
        self._expected = expected_secret

    async def dispatch(self, request: Request, call_next):
        if request.url.path in OPEN_PATHS:
            return await call_next(request)

        received = request.headers.get(HEADER_NAME, "")
        if not received or not hmac.compare_digest(received, self._expected):
            return JSONResponse(
                {"error": "Missing or invalid MCP shared secret"},
                status_code=401,
            )
        return await call_next(request)
