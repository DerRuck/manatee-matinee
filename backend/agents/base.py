from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anthropic
import yaml

PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"
DEFAULT_MODEL = "claude-sonnet-4-6"


@dataclass
class AgentConfig:
    agent: str
    version: int
    system_prompt: str
    max_tokens: int
    model: str
    temperature: float | None
    created: str
    author: str
    why: str


@dataclass
class AgentResult:
    content: str
    stop_reason: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int


class BaseAgent:
    def __init__(self, agent_name: str, version: int = 1):
        self.config = _load_config(agent_name, version)
        self._client = anthropic.Anthropic()

    def run(self, user_message: str, context: dict[str, Any] | None = None) -> AgentResult:
        create_kwargs: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": self.config.system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": _build_messages(user_message, context),
        }
        if self.config.temperature is not None:
            create_kwargs["temperature"] = self.config.temperature

        response = self._client.messages.create(**create_kwargs)
        text = next((b.text for b in response.content if b.type == "text"), "")

        return AgentResult(
            content=text,
            stop_reason=response.stop_reason,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_creation_tokens=response.usage.cache_creation_input_tokens or 0,
            cache_read_tokens=response.usage.cache_read_input_tokens or 0,
        )


def _load_config(agent_name: str, version: int) -> AgentConfig:
    yaml_path = PROMPTS_DIR / agent_name / f"v{version}.yaml"
    with open(yaml_path) as f:
        data = yaml.safe_load(f)
    cfg = data.get("config", {})
    return AgentConfig(
        agent=data["agent"],
        version=data["version"],
        system_prompt=data["system_prompt"],
        max_tokens=cfg.get("max_tokens", 1024),
        model=cfg.get("model", DEFAULT_MODEL),
        temperature=cfg.get("temperature"),
        created=str(data.get("created", "")),
        author=str(data.get("author", "")),
        why=str(data.get("why", "")),
    )


def _build_messages(user_message: str, context: dict[str, Any] | None) -> list[dict]:
    if not context:
        return [{"role": "user", "content": user_message}]
    context_str = "\n".join(f"{k}: {v}" for k, v in context.items())
    return [{"role": "user", "content": f"{context_str}\n\n{user_message}"}]
