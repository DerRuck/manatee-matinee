"""Gmail service — DWD-backed draft creation for the Email Drafter agent."""
from .client import create_draft, resolve_from_user

__all__ = ["create_draft", "resolve_from_user"]
