"""Tests for the GHL contact → agent context flattener.

The fixture mirrors the real Firestore document shape (the GHL contacts
backfill from 2026-05): firstName/firstNameRaw, customFields[], tags[],
nullable city, etc.
"""
from __future__ import annotations

from services.firestore.contact_context import build_context_from_contact


_SAMPLE_DOC = {
    "id": "0I21saCPXJVEbdncGXEW",
    "firstName": "jamie",
    "firstNameRaw": "Jamie",
    "lastName": "sheehan",
    "lastNameRaw": "Sheehan",
    "contactName": "jamie sheehan",
    "email": "jamie@floridaenet.com",
    "phone": "+18504435937",
    "companyName": "Florida Environmental Network",
    "city": None,
    "state": None,
    "country": "US",
    "postalCode": None,
    "tags": ["intake-done", "boil"],
    "type": "lead",
    "locationId": "As8Nc8kEs6J86YgDIi9Q",
    "customFields": [
        {"id": "u7nkCuvWJdcfe4mZLqjR", "fieldKey": "contact.contact_notes",
         "value": "Strong intake on 2/11 — wants water-quality monitoring help."},
        {"id": "abc123", "fieldKey": "contact.job_title", "value": "Executive Director"},
        # No fieldKey — falls back to id as the key
        {"id": "unkeyed_field", "value": "kept by id"},
        # Null value — dropped
        {"id": "empty", "fieldKey": "contact.unused", "value": None},
    ],
}


def test_basic_field_mapping():
    ctx = build_context_from_contact(_SAMPLE_DOC)
    assert ctx["contact_id"] == "0I21saCPXJVEbdncGXEW"
    assert ctx["first_name"] == "Jamie"  # firstNameRaw wins
    assert ctx["last_name"] == "Sheehan"
    assert ctx["contact_name"] == "jamie sheehan"  # contactName wins
    assert ctx["email"] == "jamie@floridaenet.com"
    assert ctx["phone"] == "+18504435937"
    assert ctx["company_name"] == "Florida Environmental Network"
    assert ctx["country"] == "US"
    assert ctx["type"] == "lead"


def test_municipality_falls_back_to_company_when_city_is_null():
    """Org-level contacts (a nonprofit, an agency) have null city. Their
    municipality_name should fall back to the company name so research
    prompts that key off it don't get a None."""
    ctx = build_context_from_contact(_SAMPLE_DOC)
    assert ctx["municipality_name"] == "Florida Environmental Network"


def test_municipality_uses_city_when_present():
    doc = {**_SAMPLE_DOC, "city": "Tallahassee"}
    ctx = build_context_from_contact(doc)
    assert ctx["municipality_name"] == "Tallahassee"
    assert ctx["city"] == "Tallahassee"


def test_null_fields_are_stripped():
    ctx = build_context_from_contact(_SAMPLE_DOC)
    # None values omitted entirely so the prompt's input resolver doesn't
    # see them as "provided but empty".
    assert "postal_code" not in ctx
    assert "city" not in ctx  # was null in the source


def test_custom_fields_promoted_by_field_key():
    """custom_fields with a fieldKey like 'contact.contact_notes' get
    promoted to top-level as 'contact_notes' so prompts can reference them
    directly."""
    ctx = build_context_from_contact(_SAMPLE_DOC)
    assert ctx["contact_notes"].startswith("Strong intake")
    assert ctx["job_title"] == "Executive Director"


def test_custom_fields_keyed_dict_preserved():
    ctx = build_context_from_contact(_SAMPLE_DOC)
    cf = ctx["custom_fields"]
    assert cf["contact_notes"].startswith("Strong intake")
    assert cf["job_title"] == "Executive Director"
    # Unkeyed field falls back to its id
    assert cf["unkeyed_field"] == "kept by id"
    # Null-value entries are dropped
    assert "unused" not in cf


def test_overrides_win():
    """Overrides are how the CLI / webhook layer meeting-specific fields
    (audience, meeting_date) on top of the contact baseline."""
    ctx = build_context_from_contact(
        _SAMPLE_DOC,
        overrides={
            "audience": "Florida Environmental Network board",
            "meeting_date": "June 12, 2026",
            "municipality_name": "Override City",  # beats the company fallback
        },
    )
    assert ctx["audience"] == "Florida Environmental Network board"
    assert ctx["meeting_date"] == "June 12, 2026"
    assert ctx["municipality_name"] == "Override City"


def test_tags_passed_through_as_list():
    ctx = build_context_from_contact(_SAMPLE_DOC)
    assert ctx["tags"] == ["intake-done", "boil"]


def test_minimum_viable_contact():
    """A nearly-empty contact still produces a usable context dict."""
    ctx = build_context_from_contact({"id": "abc", "email": "test@example.com"})
    assert ctx["contact_id"] == "abc"
    assert ctx["email"] == "test@example.com"
    assert ctx["contact_name"] == "test@example.com"
    assert "first_name" not in ctx  # null/empty stripped


def test_handles_missing_custom_fields():
    ctx = build_context_from_contact({"id": "x", "firstName": "Test"})
    assert ctx["custom_fields"] == {}
