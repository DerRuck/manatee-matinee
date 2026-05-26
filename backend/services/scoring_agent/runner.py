"""
Core Scoring Agent runner.

Public API:
    run(yaml_path, context, model, verbose) -> (ScoringResult, meta)
    load_prompt(yaml_path) -> dict
    resolve_inputs(cfg, context) -> dict

The runner:
  1. Loads a prompt YAML from prompts/scoring_agent/<type>/v<n>.yaml
  2. Resolves typed inputs from a context dict (built by context_builder)
  3. Builds system + user prompts with the ScoringResult JSON schema injected
  4. Calls Claude (no web tools — scoring uses internal signals only)
  5. Parses and validates into a typed ScoringResult

Mirrors services/presentation_agent/runner.py — same operational pattern,
no web_search/web_fetch (scoring reads our own data; the binder retrieval
layer remains available for future use).
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Template

from services.scoring_agent.schema import (
    ScoringResult,
    json_schema_for_type,
)


# ---------------------------------------------------------------------------
# Prompt loading + input resolution
# ---------------------------------------------------------------------------

def load_prompt(yaml_path: Path) -> dict:
    return yaml.safe_load(yaml_path.read_text(encoding="utf-8"))


def resolve_inputs(cfg: dict, context: dict) -> dict:
    """Map required/optional YAML input specs to values from a context dict.

    Same pattern as research_agent + presentation_agent: missing required
    inputs raise; optional inputs are passed through when present.
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

def build_system_prompt(cfg: dict, schema: dict) -> str:
    """Compose:
       (1) YAML system prompt — agent role + rubric
       (2) ScoringResult JSON schema — exact response shape
    """
    return (
        cfg["system"].rstrip()
        + "\n\n## Output JSON schema (REQUIRED)\n\n"
        "Your response MUST be a single JSON object validating against this "
        "schema. No prose, no markdown fences, no preamble. Start with `{` "
        "and end with `}`.\n\n"
        "Notes:\n"
        "- Omit `run_id`, `prompt_version`, `score_type_id`, and "
        "`generated_at` — injected by the runner.\n"
        f"- `findings.score_type` MUST be the literal value for this prompt: \"{cfg['id']}\".\n"
        f"- `score_type_id` will be set to \"{cfg['id']}\" by the runner.\n\n"
        f"```json\n{json.dumps(schema, indent=2)}\n```"
    )


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
) -> tuple[ScoringResult, dict]:
    """Run the Scoring Agent for one contact context.

    Returns (validated ScoringResult, metadata dict with token counts + timing).
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
    system = build_system_prompt(cfg, schema)
    user_template = Template(cfg["user"])
    user = user_template.render(**_jinja_safe(inputs))

    if verbose:
        print(f"[3/4] Built prompts: system={len(system):,} chars, user={len(user):,} chars")

    client = Anthropic()
    model_name = model or cfg["model"]["name"]

    if verbose:
        temp = cfg["model"].get("temperature", 0.2)
        print(f"[4/4] Calling {model_name} (T={temp}) [no web tools — internal-signal scoring]")

    t0 = time.time()
    raw_chunks: list[str] = []
    last_progress = t0

    stream_kwargs: dict[str, Any] = {
        "model": model_name,
        "max_tokens": cfg["model"].get("max_tokens", 6000),
        "temperature": cfg["model"].get("temperature", 0.2),
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }

    def update_progress() -> None:
        if not verbose:
            return
        approx_tokens = sum(len(c) for c in raw_chunks) // 4
        elapsed = time.time() - t0
        rate = approx_tokens / elapsed if elapsed > 0 else 0
        sys.stdout.write(
            f"\r        [scoring] ~{approx_tokens:,} tok, {rate:.0f}/s, {elapsed:.0f}s   "
        )
        sys.stdout.flush()

    with client.messages.stream(**stream_kwargs) as stream:
        for event in stream:
            etype = getattr(event, "type", None)
            if etype == "content_block_delta":
                delta = getattr(event, "delta", None)
                if delta is not None and getattr(delta, "type", None) == "text_delta":
                    raw_chunks.append(delta.text)
                    if time.time() - last_progress > 0.4:
                        update_progress()
                        last_progress = time.time()
        final_message = stream.get_final_message()

    elapsed = time.time() - t0
    if verbose:
        sys.stdout.write("\r" + " " * 80 + "\r")
        sys.stdout.flush()

    raw_text = "".join(raw_chunks)

    try:
        raw_json = extract_json(raw_text)
        parsed = json.loads(raw_json)
    except (ValueError, json.JSONDecodeError) as exc:
        max_t = cfg["model"].get("max_tokens", 6000)
        truncated = final_message.usage.output_tokens >= max_t - 50
        raise ValueError(
            f"Claude's response is not valid JSON: {exc}\n"
            f"Output tokens: {final_message.usage.output_tokens}"
            + (f"\nHINT: max_tokens={max_t} was reached — response truncated." if truncated else "")
        ) from exc

    parsed["run_id"] = str(uuid.uuid4())
    parsed["prompt_version"] = cfg["version"]
    parsed["score_type_id"] = cfg["id"]
    parsed["contact_id"] = parsed.get("contact_id") or context.get("contact_id")
    parsed["municipality_name"] = (
        parsed.get("municipality_name") or context.get("municipality_name")
    )
    # Stash a human-readable label so file names and doc headers don't
    # fall back to the opaque GHL contact_id. Source of truth is the
    # flattened contact_record built by services.firestore.contact_context.
    contact_record = context.get("contact_record") or {}
    parsed["contact_name"] = (
        parsed.get("contact_name")
        or contact_record.get("contact_name")
        or _join_name(contact_record.get("first_name"), contact_record.get("last_name"))
    )
    parsed["contact_email"] = (
        parsed.get("contact_email")
        or contact_record.get("email")
    )
    parsed["generated_at"] = datetime.now(timezone.utc).isoformat()
    parsed.setdefault("triggered_by", context.get("triggered_by", "manual"))

    result = ScoringResult.model_validate(parsed)

    meta = {
        "elapsed_sec": round(elapsed, 2),
        "input_tokens": final_message.usage.input_tokens,
        "output_tokens": final_message.usage.output_tokens,
        "model": model_name,
    }

    if verbose:
        print(
            f"Done in {elapsed:.1f}s. "
            f"Tokens: in={meta['input_tokens']:,} out={meta['output_tokens']:,}. "
            f"Step={result.findings.current_step} "
            f"heat={result.findings.lead_heat} "
            f"score={result.findings.lead_heat_score}"
        )

    return result, meta


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _join_name(first: Any, last: Any) -> str | None:
    parts = [str(p).strip() for p in (first, last) if p]
    joined = " ".join(parts).strip()
    return joined or None


def _jinja_safe(inputs: dict[str, Any]) -> dict[str, Any]:
    """Make complex values (dicts, lists) render cleanly inside the user template.

    The user template uses {{ contact_record }} and {{ agent_runs_summary }}
    which are typically dict/list values. JSON-dumping them gives Claude a
    clean fenced view; passing the raw Python repr would render unhelpfully.
    """
    out: dict[str, Any] = {}
    for k, v in inputs.items():
        if isinstance(v, (dict, list)):
            out[k] = json.dumps(v, indent=2, default=str)
        else:
            out[k] = v
    return out
