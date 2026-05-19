"""
Core Presentation Agent runner.

Public API:
    run(yaml_path, context, model, verbose, no_web_search) -> (PresentationOutline, meta)
    load_prompt(yaml_path) -> dict
    resolve_inputs(cfg, context) -> dict

The runner:
  1. Loads a prompt YAML from prompts/presentation_agent/<type>/v<n>.yaml
  2. Resolves typed inputs from a context dict
  3. Builds system + user prompts with the PresentationOutline JSON schema injected
  4. Calls Claude (optionally with web_search + web_fetch server tools)
  5. Parses and validates the response into a typed PresentationOutline

Mirrors services/research_agent/runner.py so the operational pattern is
identical for staff already familiar with running research agents.
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

from services.presentation_agent.schema import (
    PresentationOutline,
    json_schema_for_type,
)


# ---------------------------------------------------------------------------
# Prompt loading + input resolution
# ---------------------------------------------------------------------------

def load_prompt(yaml_path: Path) -> dict:
    return yaml.safe_load(yaml_path.read_text(encoding="utf-8"))


def resolve_inputs(cfg: dict, context: dict) -> dict:
    """Map required/optional YAML input specs to values from a context dict.

    For research agents the dict is a GHL contact. For presentation agents
    it's a richer context that can include upstream brief JSON blobs.
    """
    resolved: dict[str, Any] = {}
    for spec in cfg["inputs"]["required"]:
        name = spec["name"]
        if name not in context:
            raise ValueError(
                f"Missing required input '{name}' for {cfg['id']}. "
                f"Context has: {sorted(context.keys())}"
            )
        resolved[name] = context[name]
    for spec in cfg["inputs"].get("optional", []):
        name = spec["name"]
        if name in context and context[name] is not None:
            resolved[name] = context[name]
    return resolved


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def build_system_prompt(
    cfg: dict,
    schema: dict,
    no_web_search: bool = False,
) -> str:
    """Compose:
       (1) YAML system prompt — agent role + output rules
       (2) PresentationOutline JSON schema — exact response shape
       (3) Optional brevity note when web search is disabled
    """
    parts = [cfg["system"].rstrip()]

    if no_web_search:
        parts.append(
            "## No-web-search mode (pipeline test)\n\n"
            "Web search and web fetch are disabled for this run. Use your "
            "training-data knowledge and the provided context only. To keep "
            "the output small and valid:\n"
            "- Aim for the minimum slide count (3 slides).\n"
            "- Keep all text fields to 1-2 sentences.\n"
            "- Set overall_confidence to 0.4.\n"
            "The JSON must still validate against the schema."
        )

    parts.append(
        "## Output JSON schema (REQUIRED)\n\n"
        "Your response MUST be a single JSON object validating against this "
        "schema. No prose, no markdown fences, no preamble. Start with `{` "
        "and end with `}`.\n\n"
        "Notes:\n"
        "- Omit `run_id`, `prompt_version`, `outline_type_id`, `contact_id`, "
        "`municipality_name`, and `generated_at` — injected by the runner.\n"
        "- `findings.outline_type` MUST be the literal value for this "
        f"prompt: \"{cfg['id']}\".\n"
        "- `slide_number` must be 1..N sequential with no gaps.\n\n"
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
    context: dict[str, Any],
    model: str | None = None,
    verbose: bool = False,
    no_web_search: bool = False,
) -> tuple[PresentationOutline, dict]:
    """Run the Presentation Agent for one meeting context.

    Returns (validated PresentationOutline, metadata dict with token counts + timing).
    """
    try:
        from anthropic import Anthropic
    except ImportError:
        raise RuntimeError("anthropic SDK not installed.")

    cfg = load_prompt(yaml_path)
    inputs = resolve_inputs(cfg, context)

    if verbose:
        print(f"[1/4] Loaded prompt {cfg['id']} v{cfg['version']}: {cfg['name']}")
        print(f"[2/4] Resolved inputs: {sorted(inputs.keys())}")

    schema = json_schema_for_type(cfg["id"])
    system = build_system_prompt(cfg, schema, no_web_search=no_web_search)
    user = Template(cfg["user"]).render(**inputs)

    if verbose:
        print(f"[3/4] Built prompts: system={len(system):,} chars, user={len(user):,} chars")

    client = Anthropic()
    model_name = model or cfg["model"]["name"]

    tools: list[dict] = []
    extra_headers: dict[str, str] = {}
    domains: list[str] = []
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
            "max_uses": 3,
        }
        if domains:
            web_search_tool["allowed_domains"] = domains

        tools = [
            web_search_tool,
            {
                "type": "web_fetch_20250910",
                "name": "web_fetch",
                "max_uses": 5,
                "citations": {"enabled": True},
            },
        ]
        extra_headers["anthropic-beta"] = "web-fetch-2025-09-10"

    if verbose:
        temp = cfg["model"].get("temperature", 0.4)
        if tools:
            ds = ", ".join(domains) if domains else "ALL DOMAINS"
            print(f"[4/4] Calling {model_name} (T={temp}) + web_search + web_fetch")
            print(f"        allowed_domains: {ds}")
        else:
            print(f"[4/4] Calling {model_name} (T={temp}) [web search disabled]")

    t0 = time.time()
    raw_chunks: list[str] = []
    last_progress = t0
    status = "thinking"
    tool_call_counts = {"web_search": 0, "web_fetch": 0}

    stream_kwargs: dict[str, Any] = {
        "model": model_name,
        "max_tokens": cfg["model"].get("max_tokens", 8000),
        "temperature": cfg["model"].get("temperature", 0.4),
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

    try:
        raw_json = extract_json(raw_text)
        parsed = json.loads(raw_json)
    except (ValueError, json.JSONDecodeError) as exc:
        max_t = cfg["model"].get("max_tokens", 8000)
        truncated = final_message.usage.output_tokens >= max_t - 50
        raise ValueError(
            f"Claude's response is not valid JSON: {exc}\n"
            f"Output tokens: {final_message.usage.output_tokens}"
            + (f"\nHINT: max_tokens={max_t} was reached — response truncated." if truncated else "")
        ) from exc

    parsed["run_id"] = str(uuid.uuid4())
    parsed["prompt_version"] = cfg["version"]
    parsed["outline_type_id"] = cfg["id"]
    parsed["contact_id"] = context.get("contact_id")
    parsed["municipality_name"] = context.get("municipality_name")
    parsed["generated_at"] = datetime.now(timezone.utc).isoformat()
    parsed["triggering_event"] = context.get("triggering_event")

    outline = PresentationOutline.model_validate(parsed)

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

    return outline, meta
