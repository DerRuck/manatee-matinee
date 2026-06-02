"""Gmail service — DWD-backed draft creation for the Email Drafter agent."""
from .client import (
    create_draft,
    get_signature,
    get_thread,
    resolve_from_user,
    search_contact_threads,
)

__all__ = [
    "create_draft",
    "get_signature",
    "get_thread",
    "resolve_from_user",
    "search_contact_threads",
]
