"""
Feedback agent.

Categorizes a human reviewer's reaction to an agent deliverable (an email
draft or a research brief). Loads the feedback_agent/v1.yaml prompt, builds
a user message from the reaction + note (+ optional diff), calls Claude,
parses the structured JSON response, and returns a typed FeedbackResult.

This agent is intentionally decoupled from Firestore and Drive — it only
does the model call. The orchestration pattern (mirroring email_drafter) is:

    agent = FeedbackAgent(version=1)
    result = agent.run_for_feedback(FeedbackInput(...))
    services.firestore.put_feedback(..., record={...})

so the categorizer stays reusable for a CLI smoke or an evaluation harness
that just wants to inspect the classification without writing records.

The runner (services/feedback_runner.py) resolves the original deliverable
text from Drive and computes the diff; this agent receives them ready-made.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal, Optional

from agents.base import BaseAgent

logger = logging.getLogger(__name__)


Reaction = Literal[
    "approved", "edits_requested", "rerun_requested", "rejected"
]
Sentiment = Literal["positive", "neutral", "negative"]

# Closed taxonomy. Mirrors the CATEGORY LIST in prompts/feedback_agent/v1.yaml.
# Keep the two in sync — the prompt is the model's contract, this is the
# validator's. A category here but not in the prompt would never be emitted;
# one in the prompt but not here would be rejected by the parser.
VALID_CATEGORIES = frozenset(
    {
        "tone",
        "length",
        "factual_accuracy",
        "formatting",
        "greeting_or_closing",
        "call_to_action",
        "personalization",
        "subject_line",
        "no_change",
    }
)


@dataclass
class FeedbackInput:
    """
    Inputs the agent needs to categorize one reviewer reaction.

    `original_run_id` and `contact_id` are not consumed by the model call
    itself — they're carried so the runner can write the schema link
    without re-threading them. `agent_type` tells the model what kind of
    deliverable was reviewed (an email subject_line category only makes
    sense for email_drafter).

    `original_text` and `diff` are resolved by the runner from Drive before
    this agent runs. Both are optional: a bare approval needs neither.
    """

    original_run_id: str
    reaction: Reaction
    note: str = ""

    contact_id: Optional[str] = None
    agent_type: Optional[str] = None  # "email_drafter" | "research"
    original_text: Optional[str] = None
    diff: Optional[str] = None


@dataclass
class FeedbackResult:
    """
    Structured classification the runner persists. Downstream callers read
    these fields directly — no JSON re-parsing required.
    """

    categories: list[str]
    sentiment: Sentiment
    summary: str
    actionable: bool

    # Run metadata for logging / debugging, same shape as EmailDraftResult.
    raw_model_output: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    prompt_version: int
    model: str


class FeedbackAgent(BaseAgent):
    """
    Subclass of BaseAgent that loads the feedback_agent prompt and adds a
    reaction-aware run_for_feedback() entry point. The plain run() inherited
    from BaseAgent stays available for testing the prompt in isolation.
    """

    def __init__(self, version: int = 1) -> None:
        super().__init__("feedback_agent", version)

    def run_for_feedback(self, input_: FeedbackInput) -> FeedbackResult:
        """Build the user message, call Claude, parse JSON, return typed result."""
        user_message = _build_user_message(input_)

        result = self.run(user_message)
        parsed = _parse_feedback_json(result.content)

        return FeedbackResult(
            categories=parsed["categories"],
            sentiment=parsed["sentiment"],
            summary=parsed["summary"],
            actionable=parsed["actionable"],
            raw_model_output=result.content,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_creation_tokens=result.cache_creation_tokens,
            cache_read_tokens=result.cache_read_tokens,
            prompt_version=self.config.version,
            model=result.model,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_user_message(input_: FeedbackInput) -> str:
    """Render agent type + reaction + note + optional diff for the model."""
    lines: list[str] = []
    lines.append(f"AGENT TYPE: {input_.agent_type or '(unknown)'}")
    lines.append(f"REACTION: {input_.reaction}")
    lines.append("")
    lines.append("REVIEWER NOTE")
    note = (input_.note or "").strip()
    lines.append(f"  {note}" if note else "  (no note — bare reaction)")

    if input_.diff:
        lines.append("")
        lines.append("DIFF (reviewer's edit vs. original — unified diff)")
        for diff_line in input_.diff.splitlines():
            lines.append(f"  {diff_line}")

    lines.append("")
    lines.append(
        "Categorize this reaction per the system prompt. Return JSON only — "
        "no markdown fences, no preamble, no trailing commentary."
    )
    return "\n".join(lines)


def _parse_feedback_json(raw: str) -> dict[str, Any]:
    """
    Parse the model's JSON output. Strips a code fence if Claude wrapped it
    despite the system prompt asking otherwise. Validates the expected
    fields are present and that categories/sentiment stay inside the closed
    sets.

    Raises ValueError with the raw output (truncated) attached on failure so
    the caller can log it for prompt-iteration purposes.
    """
    text = raw.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"feedback_agent output was not valid JSON: {exc}. "
            f"Raw[:500]: {raw[:500]}"
        ) from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"feedback_agent output was not a JSON object. Raw[:500]: {raw[:500]}"
        )

    required = {"categories", "sentiment", "summary", "actionable"}
    missing = required - data.keys()
    if missing:
        raise ValueError(
            f"feedback_agent output missing required fields: {sorted(missing)}. "
            f"Raw[:500]: {raw[:500]}"
        )

    if not isinstance(data["categories"], list):
        raise ValueError(
            f"feedback_agent categories must be a list. Raw[:500]: {raw[:500]}"
        )
    unknown = [c for c in data["categories"] if c not in VALID_CATEGORIES]
    if unknown:
        raise ValueError(
            f"feedback_agent categories outside the closed set: {unknown}. "
            f"Allowed: {sorted(VALID_CATEGORIES)}. Raw[:500]: {raw[:500]}"
        )

    valid_sentiment = {"positive", "neutral", "negative"}
    if data["sentiment"] not in valid_sentiment:
        raise ValueError(
            f"feedback_agent sentiment='{data['sentiment']}' not in "
            f"{sorted(valid_sentiment)}. Raw[:500]: {raw[:500]}"
        )

    if not isinstance(data["actionable"], bool):
        raise ValueError(
            f"feedback_agent actionable must be a bool. Raw[:500]: {raw[:500]}"
        )

    return data
