"""Smoke test for the remote C-HAWQ MCP server.

Connects via Streamable HTTP using the FastMCP client, lists the exposed
tools, fires a small research run, polls until completion. Run this after
``gcloud run deploy`` finishes and before pointing Claude at the URL — it
proves the inbound auth, MCP transport, and outbound API call all work.

Usage:

    export CHAWQ_MCP_URL=https://chawq-mcp-dev-XXXX.us-central1.run.app/mcp/
    export CHAWQ_MCP_SHARED_SECRET=<the inbound secret>
    python smoke_remote.py

Trailing slash on the URL matters — FastMCP mounts the endpoint at /mcp/, not
/mcp.

Expected outcome:
    Tools exposed: ['chawq_agent_run', 'chawq_agent_status']
    Firing chawq_agent_run (research / PW-3, Rookery Bay)...
      -> run_id = <uuid>
      poll 1: status = pending
      poll 2: status = running
      ...
      poll N: status = completed
    Final response: {...with drive_web_link...}
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from fastmcp import Client


URL = os.environ.get("CHAWQ_MCP_URL")
SECRET = os.environ.get("CHAWQ_MCP_SHARED_SECRET")

if not URL or not SECRET:
    print(
        "ERROR: set CHAWQ_MCP_URL and CHAWQ_MCP_SHARED_SECRET before running.",
        file=sys.stderr,
    )
    sys.exit(2)


# Research run that exercises the cheapest path:
# PW-3 takes municipality_name + county and skips heavy web search when
# no_web_search=true, so it finishes in ~10-30s on the dev backend.
SMOKE_INPUTS = {
    "research_type": "PW-3",
    "municipality_name": "Rookery Bay",
    "county": "Collier",
    "no_web_search": True,
}
POLL_INTERVAL_SECONDS = 2
POLL_MAX_ATTEMPTS = 45  # ~90s ceiling


async def main() -> int:
    headers = {"X-CHAWQ-MCP-Secret": SECRET}

    async with Client(URL, headers=headers) as client:
        # 1. List tools — confirms transport + inbound auth both work.
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}
        print(f"Tools exposed: {sorted(tool_names)}")
        expected = {"chawq_agent_run", "chawq_agent_status"}
        missing = expected - tool_names
        if missing:
            print(f"FAIL: expected tools missing: {sorted(missing)}", file=sys.stderr)
            return 1

        # 2. Fire a research run.
        print("Firing chawq_agent_run (research / PW-3, Rookery Bay)...")
        result = await client.call_tool(
            "chawq_agent_run",
            {"agent": "research", "inputs": SMOKE_INPUTS},
        )
        run_id = result.data.get("run_id")
        if not run_id:
            print(f"FAIL: no run_id in response: {result.data}", file=sys.stderr)
            return 1
        print(f"  -> run_id = {run_id}")

        # 3. Poll until terminal.
        for attempt in range(1, POLL_MAX_ATTEMPTS + 1):
            time.sleep(POLL_INTERVAL_SECONDS)
            status_result = await client.call_tool(
                "chawq_agent_status", {"run_id": run_id}
            )
            data = status_result.data
            status = data.get("status", "?")
            print(f"  poll {attempt}: status = {status}")
            if status in {"completed", "partial", "failed"}:
                print("Final response:")
                print(data)
                return 0 if status == "completed" else 1

        print(
            f"FAIL: timed out after {POLL_MAX_ATTEMPTS * POLL_INTERVAL_SECONDS}s "
            "of polling.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
