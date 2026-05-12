"""
Core Research Agent runner.

Public API:
    run(yaml_path, contact, model, verbose, no_web_search) -> (ResearchBrief, meta)
    load_prompt(yaml_path) -> dict
    resolve_inputs(cfg, contact) -> dict

The runner:
  1. Loads a prompt YAML from prompts/research_agent/<type>/v<n>.yaml
  2. Resolves typed inputs from a contact dict
  3. Retrieves the canonical binder prompt chunk from Firestore (if configured)
  4. Builds system + user prompts with the ResearchBrief JSON schema injected
  5. Calls Claude with web_search + web_fetch server tools
  6. Parses and validates the response into a typed ResearchBrief

Binder chunks are stored in the `vector_chunks` Firestore collection by
scripts/ingest_binder.py — separate from the `chunks` collection used for
contact/document ingestion.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Template

from services.research_agent.schema import ResearchBrief, json_schema_for_type

BINDER_COLLECTION = "chunks"


# ---------------------------------------------------------------------------
# Prompt loading + input resolution
# ---------------------------------------------------------------------------

def load_prompt(yaml_path: Path) -> dict:
    return yaml.safe_load(yaml_path.read_text(encoding="utf-8"))


def resolve_inputs(cfg: dict, contact: dict) -> dict:
    """Map required/optional YAML input specs to values from a contact dict.

    In production the Context Assembler walks `source: ghl.contact.X` paths.
    Here we use the flat contact dict directly.
    """
    resolved: dict[str, Any] = {}
    for spec in cfg["inputs"]["required"]:
        name = spec["name"]
        if name not in contact:
            raise ValueError(
                f"Missing required input '{name}' for {cfg['id']}. "
                f"Contact has: {sorted(contact.keys())}"
            )
        resolved[name] = contact[name]
    for spec in cfg["inputs"].get("optional", []):
        name = spec["name"]
        if name in contact and contact[name] is not None:
            resolved[name] = contact[name]
    return resolved


# ---------------------------------------------------------------------------
# Binder context retrieval
# ---------------------------------------------------------------------------

def retrieve_binder_context(cfg: dict) -> str:
    """Pull the canonical binder prompt chunk by structured filter.

    No semantic search needed — research_type_id is a direct lookup.
    Returns empty string if Firestore is unavailable or no match found.
    """
    try:
        from google.cloud import firestore
        from google.cloud.firestore_v1.base_query import FieldFilter
    except ImportError:
        return ""

    primary = cfg.get("retrieval", {}).get("binder")
    if not primary:
        return ""

    try:
        from core.settings import get_settings
        project = get_settings().gcp_project_id
    except Exception:
        project = os.environ.get("GOOGLE_CLOUD_PROJECT", "chawq-manatee-matinee")

    try:
        db = firestore.Client(project=project)
        q = db.collection(BINDER_COLLECTION).where(
            filter=FieldFilter("source_doc", "==", primary["source_doc"])
        )
        if "research_type_id" in primary:
            q = q.where(filter=FieldFilter("research_type_id", "==", primary["research_type_id"]))
        if "chunk_type" in primary:
            q = q.where(filter=FieldFilter("chunk_type", "==", primary["chunk_type"]))
        docs = list(q.limit(primary.get("limit", 5)).stream())
        texts = [d.to_dict()["text"] for d in docs if d.to_dict().get("text")]
        return "\n\n---\n\n".join(texts)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def build_system_prompt(
    cfg: dict,
    binder_canonical: str,
    schema: dict,
    no_web_search: bool = False,
) -> str:
    """Compose:
       (1) YAML system prompt — agent role + output rules
       (2) Binder canonical prompt — ground-truth framing (if retrieved)
       (3) ResearchBrief JSON schema — exact response shape
       (4) Optional brevity note when web search is disabled (test/dev runs)
    """
    parts = [cfg["system"].rstrip()]

    if no_web_search:
        parts.append(
            "## No-web-search mode (pipeline test)\n\n"
            "Web search and web fetch are disabled for this run. Use your "
            "training-data knowledge only. To keep the output small and valid:\n"
            "- Limit every list to at most 2 items.\n"
            "- Keep all text fields to 1-2 sentences.\n"
            "- Set overall_confidence to 0.4 (training-data knowledge only).\n"
            "The JSON must still validate against the schema — just keep it minimal."
        )

    if binder_canonical:
        parts.append(
            "## Reference: canonical playbook framing\n\n"
            "Below is the framing the C-HAWQ Proven Process Binder gives to "
            "this research task. Treat the question structure as authoritative; "
            "the bracketed placeholders have been replaced with the real values "
            "in the user message below.\n\n"
            f"```\n{binder_canonical}\n```"
        )

    parts.append(
        "## Output JSON schema (REQUIRED)\n\n"
        "Your response MUST be a single JSON object validating against this "
        "schema. No prose, no markdown fences, no preamble. Start with `{` "
        "and end with `}`.\n\n"
        "Notes:\n"
        "- Omit `run_id`, `prompt_version`, `research_type_id`, `contact_id`, "
        "`municipality_name`, and `generated_at` — injected by the runner.\n"
        "- `findings.research_type` MUST be the literal value for this prompt: "
        f"\"{cfg['id']}\".\n\n"
        f"```json\n{json.dumps(schema, indent=2)}\n```"
    )

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------

def extract_json(raw_text: str) -> str:
    """Pull a JSON object from Claude's response, tolerating markdown fences."""
    text = raw_text.strip()

    if text.startswith("```"):
        text = text[3:]
        if text.lower().startswith("json"):
            text = text[4:]
        end = text.rfind("```")
        if end >= 0:
            text = text[:end]
        text = text.strip()

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(
            f"No JSON object found in response. First 200 chars: {text[:200]!r}"
        )
    return text[start:end + 1]


# ---------------------------------------------------------------------------
# Core run
# ---------------------------------------------------------------------------

def run(
    yaml_path: Path,
    contact: dict[str, Any],
    model: str | None = None,
    verbose: bool = False,
    no_web_search: bool = False,
) -> tuple[ResearchBrief, dict]:
    """Run the Research Agent for one contact.

    Returns (validated ResearchBrief, metadata dict with token counts + timing).
    """
    try:
        from anthropic import Anthropic
    except ImportError:
        raise RuntimeError("anthropic SDK not installed.")

    cfg = load_prompt(yaml_path)
    inputs = resolve_inputs(cfg, contact)

    if verbose:
        print(f"[1/5] Loaded prompt {cfg['id']} v{cfg['version']}: {cfg['name']}")
        print(f"[2/5] Resolved inputs: {sorted(inputs.keys())}")

    binder_canonical = retrieve_binder_context(cfg)
    if verbose:
        print(f"[3/5] Retrieved binder context: {len(binder_canonical):,} chars")

    schema = json_schema_for_type(cfg["id"])
    system = build_system_prompt(cfg, binder_canonical, schema, no_web_search=no_web_search)
    user = Template(cfg["user"]).render(**inputs)

    if verbose:
        print(f"[4/5] Built prompts: system={len(system):,} chars, user={len(user):,} chars")

    client = Anthropic()
    model_name = model or cfg["model"]["name"]

    # Build web_search + web_fetch server tools from the retrieval config.
    # Domain allowlist strips wildcards; the API auto-includes subdomains.
    tools: list[dict] = []
    extra_headers: dict[str, str] = {}
    if not no_web_search:
        external = cfg.get("retrieval", {}).get("external", {})
        domains = [
            d.removeprefix("*.").removeprefix("https://").removeprefix("http://")
            for d in external.get("enabled_sources", [])
            if d
        ]
        domains = [d for d in domains if d]

        web_search_tool: dict[str, Any] = {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 5,
        }
        if domains:
            web_search_tool["allowed_domains"] = domains

        tools = [
            web_search_tool,
            {
                "type": "web_fetch_20250910",
                "name": "web_fetch",
                "max_uses": 8,
                "citations": {"enabled": True},
            },
        ]
        extra_headers["anthropic-beta"] = "web-fetch-2025-09-10"

    if verbose:
        temp = cfg["model"].get("temperature", 0.3)
        if tools:
            ds = ", ".join(domains) if domains else "ALL DOMAINS"
            print(f"[5/5] Calling {model_name} (T={temp}) + web_search + web_fetch")
            print(f"        allowed_domains: {ds}")
        else:
            print(f"[5/5] Calling {model_name} (T={temp}) [web search disabled]")

    # Stream with event-level handling to capture tool_use progress.
    t0 = time.time()
    raw_chunks: list[str] = []
    last_progress = t0
    status = "thinking"
    tool_call_counts = {"web_search": 0, "web_fetch": 0}

    stream_kwargs: dict[str, Any] = {
        "model": model_name,
        "max_tokens": cfg["model"].get("max_tokens", 4000),
        "temperature": cfg["model"].get("temperature", 0.3),
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    if tools:
        stream_kwargs["tools"] = tools
    if extra_headers:
        stream_kwargs["extra_headers"] = extra_headers

    def update_progress() -> None:
        if not verbose:
            return
        approx_tokens = sum(len(c) for c in raw_chunks) // 4
        elapsed = time.time() - t0
        rate = approx_tokens / elapsed if elapsed > 0 else 0
        line = (
            f"\r        [{status:13}] ~{approx_tokens:,} tok, {rate:.0f}/s, "
            f"{elapsed:.0f}s, search={tool_call_counts['web_search']} "
            f"fetch={tool_call_counts['web_fetch']}        "
        )
        sys.stdout.write(line)
        sys.stdout.flush()

    with client.messages.stream(**stream_kwargs) as stream:
        for event in stream:
            etype = getattr(event, "type", None)

            if etype == "content_block_start":
                cb = getattr(event, "content_block", None)
                if cb is not None:
                    cb_type = getattr(cb, "type", None)
                    if cb_type == "server_tool_use":
                        name = getattr(cb, "name", "")
                        if name == "web_search":
                            status = "searching"
                            tool_call_counts["web_search"] += 1
                        elif name == "web_fetch":
                            status = "fetching"
                            tool_call_counts["web_fetch"] += 1
                    elif cb_type in ("web_search_tool_result", "web_fetch_tool_result"):
                        status = "thinking"
                    elif cb_type == "text":
                        status = "writing"
                update_progress()

            elif etype == "content_block_delta":
                delta = getattr(event, "delta", None)
                if delta is not None and getattr(delta, "type", None) == "text_delta":
                    raw_chunks.append(delta.text)
                    if time.time() - last_progress > 0.4:
                        update_progress()
                        last_progress = time.time()

        final_message = stream.get_final_message()

    elapsed = time.time() - t0
    if verbose:
        sys.stdout.write("\r" + " " * 100 + "\r")
        sys.stdout.flush()

    raw_text = "".join(raw_chunks)

    # Parse JSON
    try:
        raw_json = extract_json(raw_text)
        parsed = json.loads(raw_json)
    except (ValueError, json.JSONDecodeError) as exc:
        max_t = cfg["model"].get("max_tokens", 4000)
        truncated = final_message.usage.output_tokens >= max_t - 50
        raise ValueError(
            f"Claude's response is not valid JSON: {exc}\n"
            f"Output tokens: {final_message.usage.output_tokens}"
            + (f"\nHINT: max_tokens={max_t} was reached — response truncated." if truncated else "")
        ) from exc

    # Inject identity fields the model doesn't generate
    parsed["run_id"] = str(uuid.uuid4())
    parsed["prompt_version"] = cfg["version"]
    parsed["research_type_id"] = cfg["id"]
    parsed["contact_id"] = contact.get("contact_id")
    parsed["municipality_name"] = contact.get("municipality_name")
    parsed["generated_at"] = datetime.now(timezone.utc).isoformat()
    parsed["triggering_event"] = contact.get("triggering_event")

    brief = ResearchBrief.model_validate(parsed)

    server_tool_use = getattr(final_message.usage, "server_tool_use", None)
    actual_searches = (
        getattr(server_tool_use, "web_search_requests", None)
        if server_tool_use else None
    )

    meta = {
        "elapsed_sec": round(elapsed, 2),
        "input_tokens": final_message.usage.input_tokens,
        "output_tokens": final_message.usage.output_tokens,
        "web_searches": actual_searches if actual_searches is not None else tool_call_counts["web_search"],
        "web_fetches": tool_call_counts["web_fetch"],
        "model": model_name,
    }

    if verbose:
        print(
            f"Done in {elapsed:.1f}s. "
            f"Tokens: in={meta['input_tokens']:,} out={meta['output_tokens']:,}, "
            f"web_search={meta['web_searches']}, web_fetch={meta['web_fetches']}"
        )

    return brief, meta
