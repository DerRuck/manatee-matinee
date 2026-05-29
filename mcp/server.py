"""C-HAWQ MCP server — remote / HTTPS edition.

Wraps the C-HAWQ agent API as two MCP tools and exposes them over Streamable
HTTP so Claude (org-managed custom connector) can call them directly. No
per-user install, no per-user shared secret on disk.

Two auth layers:

* Inbound (Claude → this server). Validated by
  ``auth.McpSecretMiddleware`` on every request to the MCP path. The
  ``X-CHAWQ-MCP-Secret`` header must match ``CHAWQ_MCP_SHARED_SECRET``.

* Outbound (this server → ``chawq-api``). Sent on every HTTP call to the
  agent API as the existing ``X-CHAWQ-Secret`` header, value loaded from
  ``CHAWQ_SHARED_SECRET``. Same secret the local stdio MCP uses today.

Both secrets are loaded from env vars at startup. The server refuses to start
if either is missing — fail fast in CI, never start a half-authenticated
server in production.
"""

from __future__ import annotations

import os
import sys

import requests
import uvicorn
from fastmcp import FastMCP
from starlette.middleware import Middleware

from auth import McpSecretMiddleware


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_BASE = os.environ.get(
    "CHAWQ_API_BASE",
    # Dev Cloud Run is the default until prod catches up to the
    # /agents/run + /agents/runs/{id} schema. See memory entry
    # `project_chawq_prod_dev_drift`.
    "https://chawq-api-dev-783495307551.us-central1.run.app",
)
API_SECRET = os.environ.get("CHAWQ_SHARED_SECRET")
MCP_SECRET = os.environ.get("CHAWQ_MCP_SHARED_SECRET")
PORT = int(os.environ.get("PORT", "8080"))


def _die(message: str) -> None:
    """Print to stderr and exit non-zero. Cloud Run logs the message."""
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


if not API_SECRET:
    _die(
        "CHAWQ_SHARED_SECRET env var is not set. The MCP cannot reach the "
        "C-HAWQ agent API without it."
    )

if not MCP_SECRET:
    _die(
        "CHAWQ_MCP_SHARED_SECRET env var is not set. The MCP cannot validate "
        "inbound requests from Claude without it."
    )


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

mcp = FastMCP("chawq-mcp")


def _api_headers(content_type: bool = False) -> dict[str, str]:
    headers = {"X-CHAWQ-Secret": API_SECRET}
    if content_type:
        headers["Content-Type"] = "application/json"
    return headers


def _check(response: requests.Response, url: str) -> dict:
    """Return parsed JSON on 2xx, raise verbose HTTPError otherwise."""
    if response.ok:
        return response.json()

    try:
        detail = response.json()
    except Exception:
        detail = response.text or "(empty response body)"

    raise requests.HTTPError(
        f"HTTP {response.status_code} from {url}. Response: {detail}"
    )


@mcp.tool()
def chawq_agent_run(agent: str, inputs: dict) -> dict:
    """Fire a C-HAWQ agent run.

    Args:
        agent: Agent identifier. Currently supported: ``"email_drafter"``,
            ``"research"``.
        inputs: Agent-specific input dict.

            * ``email_drafter`` expects: ``contact_id``, ``contact_first_name``,
              ``contact_last_name``, ``contact_organization``,
              ``contact_municipality``, ``contact_email``, ``from_user``,
              ``triggering_event``, optional ``triggering_event_summary``.
            * ``research`` expects: ``research_type`` (e.g. ``"PW-3"``,
              ``"S1-4"``, ``"LOBBY-1"``) plus the inputs that prompt requires.

    Returns:
        Dict with ``run_id`` (uuid string) and ``status`` (typically
        ``"pending"``). The agent runs asynchronously on the server — call
        ``chawq_agent_status`` with the ``run_id`` to poll for completion.
    """
    url = f"{API_BASE}/agents/run"
    # Dev backend expects nested ``{"agent": ..., "inputs": {...}}``. Flat
    # bodies return 200 but run with empty inputs (no validation error on the
    # wrong shape) — never send flat.
    body = {"agent": agent, "inputs": inputs}
    response = requests.post(
        url,
        json=body,
        headers=_api_headers(content_type=True),
        timeout=30,
    )
    return _check(response, url)


@mcp.tool()
def chawq_agent_status(run_id: str) -> dict:
    """Poll the status of a previously fired C-HAWQ agent run.

    Args:
        run_id: The run_id returned by ``chawq_agent_run``.

    Returns:
        Flat dict at the top level. Always includes ``status`` (one of
        ``"pending"``, ``"running"``, ``"completed"``, ``"partial"``,
        ``"failed"``). When ``"completed"``, also includes agent-specific
        output (``subject``, ``gmail_web_link``, ``drive_file_id``,
        ``drive_web_link``, etc.). When ``"partial"`` or ``"failed"``,
        includes error fields (``gmail_error``, ``drive_error``, ``error``).
    """
    url = f"{API_BASE}/agents/runs/{run_id}"
    response = requests.get(url, headers=_api_headers(), timeout=30)
    return _check(response, url)


# ---------------------------------------------------------------------------
# ASGI app — MCP handler with inbound auth middleware in front.
# ---------------------------------------------------------------------------

# FastMCP's Streamable HTTP transport mounts the MCP endpoint at ``/mcp/``.
# Claude's custom-connector URL must include that path.
app = mcp.http_app(
    middleware=[Middleware(McpSecretMiddleware, expected_secret=MCP_SECRET)]
)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
