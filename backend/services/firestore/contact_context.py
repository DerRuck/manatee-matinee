"""Flatten a GHL contact document into the context dict agents consume.

The Firestore `contacts` collection holds backfilled GHL contacts in their
native shape — firstName, lastName, customFields[], tags[], etc. Research
and presentation agent prompts expect a flat snake_case context (e.g.
municipality_name, contact_id, email, first_name).

build_context_from_contact() does that mapping in one place so both the
CLI (--contact-id flag) and the webhook dispatcher can use it.

Custom fields are GHL's user-defined attributes. They arrive as a list of
{id, value} or {fieldKey, value} entries. We expose them in two ways:
  - context["custom_fields"]  : dict keyed by fieldKey (when present), else id
  - the same keys promoted to the top level so prompts can reference them
    directly when the GHL fieldKey matches a prompt input (e.g.
    contact.contact_notes → contact_notes).
"""
from __future__ import annotations

from typing import Any


def build_context_from_contact(
    contact: dict[str, Any],
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a flat agent-input context built from a GHL contact doc.

    Args:
      contact:   The raw Firestore document (GHL contact shape).
      overrides: Per-run fields layered on top (audience, meeting_date, etc.).
                 Overrides win — they're how callers add meeting-specific
                 context that isn't stored on the contact.

    Returns:
      A flat dict ready to hand to ResearchAgent.run() or PresentationAgent.run().
    """
    first = (contact.get("firstNameRaw") or contact.get("firstName") or "").strip()
    last = (contact.get("lastNameRaw") or contact.get("lastName") or "").strip()
    contact_name = (
        contact.get("contactName")
        or f"{first} {last}".strip()
        or contact.get("email")
        or contact.get("id", "")
    )

    custom_fields = _custom_fields_to_dict(contact.get("customFields") or [])

    context: dict[str, Any] = {
        "contact_id":     contact.get("id"),
        "contact_name":   contact_name,
        "first_name":     first or None,
        "last_name":      last or None,
        "email":          contact.get("email"),
        "phone":          contact.get("phone"),
        "company_name":   contact.get("companyName"),
        # Most prompts ask for `municipality_name` — fall back to city, then
        # the company name (org-level contacts like Florida Environmental
        # Network don't have a city).
        "municipality_name": contact.get("city") or contact.get("companyName"),
        "city":           contact.get("city"),
        "state":          contact.get("state"),
        "country":        contact.get("country"),
        "postal_code":    contact.get("postalCode"),
        "tags":           list(contact.get("tags") or []),
        "type":           contact.get("type"),
        "custom_fields":  custom_fields,
    }

    # Promote each custom field to the top level so a prompt input named
    # contact_notes / job_title / champion_role can be resolved directly.
    for key, value in custom_fields.items():
        context.setdefault(key, value)

    if overrides:
        context.update(overrides)

    return {k: v for k, v in context.items() if v is not None}


def _custom_fields_to_dict(raw: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce GHL's array of {id, value} (or {fieldKey, value}) to a flat map.

    Prefers fieldKey over id when both are present, and strips GHL's
    `contact.` prefix on fieldKey so prompts can reference the field by its
    bare name (e.g. `contact.contact_notes` → `contact_notes`).
    """
    out: dict[str, Any] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        key = entry.get("fieldKey") or entry.get("key") or entry.get("id")
        if not key:
            continue
        if isinstance(key, str) and key.startswith("contact."):
            key = key[len("contact."):]
        value = entry.get("value")
        if value is None:
            continue
        out[key] = value
    return out
