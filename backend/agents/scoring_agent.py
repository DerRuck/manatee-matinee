from pathlib import Path
from typing import Any

from services.scoring_agent.runner import run
from services.scoring_agent.schema import ScoringResult

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


class ScoringAgent:
    """Wraps the Scoring Agent runner.

    Consumes a contact context (built by services.scoring_agent.context_builder)
    and emits a typed ScoringResult — proven-process step placement, lead
    heat, signals, blockers, and recommended next actions.

    Usage:
        from services.scoring_agent.context_builder import build_scoring_context
        agent = ScoringAgent("PIPELINE-SCORE")
        ctx = build_scoring_context("0I21saCPXJVEbdncGXEW")
        result, meta = agent.run(ctx)
    """

    def __init__(self, score_type: str = "PIPELINE-SCORE", version: int = 1) -> None:
        self.score_type = score_type
        self.version = version
        self.yaml_path = (
            PROMPTS_DIR / "scoring_agent" / score_type / f"v{version}.yaml"
        )

    def run(
        self,
        context: dict[str, Any],
        model: str | None = None,
        verbose: bool = False,
    ) -> tuple[ScoringResult, dict]:
        """Run the agent for one contact context.

        Returns (ScoringResult, metadata) where metadata contains token
        counts, elapsed time, and model name.
        """
        return run(self.yaml_path, context, model=model, verbose=verbose)
