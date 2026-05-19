from pathlib import Path
from typing import Any

from services.presentation_agent.runner import run
from services.presentation_agent.schema import PresentationOutline

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


class PresentationAgent:
    """Wraps the Presentation Agent runner.

    Consumes meeting context (audience, champion, project) plus optional
    upstream research briefs and produces a typed PresentationOutline —
    a slide-by-slide plan ready for a renderer (python-pptx, Canva, Figma).

    Usage:
        agent = PresentationAgent("PA-CURIOSITY")
        outline, meta = agent.run({
            "municipality_name": "Rookery Bay NERR",
            "audience": "Reserve Manager and field staff",
            "champion_name": "Dr. Sarah Chen",
            "champion_role": "Reserve Manager",
            "project_focus": "Tidal creek connectivity mapping",
            "meeting_date": "May 26, 2026",
        })
    """

    def __init__(self, outline_type: str, version: int = 1) -> None:
        self.outline_type = outline_type
        self.version = version
        self.yaml_path = (
            PROMPTS_DIR / "presentation_agent" / outline_type / f"v{version}.yaml"
        )

    def run(
        self,
        context: dict[str, Any],
        model: str | None = None,
        verbose: bool = False,
        no_web_search: bool = False,
    ) -> tuple[PresentationOutline, dict]:
        """Run the agent for one meeting context.

        Returns (PresentationOutline, metadata) where metadata contains token
        counts, elapsed time, and web tool usage.
        """
        return run(
            self.yaml_path,
            context,
            model=model,
            verbose=verbose,
            no_web_search=no_web_search,
        )
