"""Email Drafter agent package — re-exports the public surface."""
from .agent import (
    EmailDrafterAgent,
    EmailDrafterInput,
    EmailDraftResult,
    SuggestedSend,
)

__all__ = [
    "EmailDrafterAgent",
    "EmailDrafterInput",
    "EmailDraftResult",
    "SuggestedSend",
]
