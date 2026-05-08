from pathlib import Path
from typing import Any

from services.research_agent.runner import run
from services.research_agent.schema import ResearchBrief

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


class ResearchAgent:
    """Wraps the Research Agent runner.

    Unlike BaseAgent, research prompts use a richer YAML schema (Jinja2 user
    templates, typed inputs, web_search/web_fetch tools, binder retrieval) and
    return a validated ResearchBrief rather than plain text.

    Usage:
        agent = ResearchAgent("PW-3")
        brief, meta = agent.run({"municipality_name": "Stuart", "county": "Martin"})
    """

    def __init__(self, research_type: str, version: int = 1) -> None:
        self.research_type = research_type
        self.version = version
        self.yaml_path = (
            PROMPTS_DIR / "research_agent" / research_type / f"v{version}.yaml"
        )

    def run(
        self,
        contact: dict[str, Any],
        model: str | None = None,
        verbose: bool = False,
        no_web_search: bool = False,
    ) -> tuple[ResearchBrief, dict]:
        """Run the agent for one contact dict.

        Returns (ResearchBrief, metadata) where metadata contains token counts,
        elapsed time, and web tool usage.
        """
        return run(
            self.yaml_path,
            contact,
            model=model,
            verbose=verbose,
            no_web_search=no_web_search,
        )
