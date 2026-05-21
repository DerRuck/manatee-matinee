"""Routing tests for the GHL webhook dispatcher.

These tests stub each branch's runner so no Claude calls or Drive uploads
happen. They verify the dispatcher reads agent_type correctly and routes:
  - missing / 'hello_world' -> hello_world runner
  - 'research:<TYPE>'       -> ResearchAgent + upload_brief
  - 'presentation:<TYPE>'   -> PresentationAgent + upload_outline
  - unknown                 -> falls back to hello_world (with a warning)
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from services.webhook_router import (
    _payload_to_context,
    dispatch_ghl_payload,
)
from services.firestore.contact_context import build_context_from_contact


# ---------------------------------------------------------------------------
# Context stripper
# ---------------------------------------------------------------------------

def test_payload_to_context_strips_routing_keys():
    with patch(
        "services.webhook_router._hydrate_contact_from_firestore",
        return_value=None,
    ):
        ctx = _payload_to_context({
            "agent_type": "presentation:PA-STEP4",
            "contact_id": "ghl_abc",
            "municipality_name": "Sample City",
        })
    assert "agent_type" not in ctx
    assert ctx["contact_id"] == "ghl_abc"
    assert ctx["municipality_name"] == "Sample City"


def test_payload_to_context_normalizes_contact_id_from_id():
    with patch(
        "services.webhook_router._hydrate_contact_from_firestore",
        return_value=None,
    ):
        ctx = _payload_to_context({"id": "ghl_xyz", "first_name": "Test"})
    assert ctx["contact_id"] == "ghl_xyz"
    assert ctx["first_name"] == "Test"


def test_payload_to_context_hydrates_from_firestore():
    """When contact_id is in the payload, the dispatcher merges the
    Firestore contact doc with the webhook-supplied fields. Webhook fields win."""
    firestore_doc = {
        "id": "ghl_real",
        "firstNameRaw": "Jamie",
        "lastNameRaw": "Sheehan",
        "email": "jamie@floridaenet.com",
        "city": "Tallahassee",
        "companyName": "Florida Environmental Network",
        "customFields": [{"fieldKey": "contact.contact_notes", "value": "Strong intake"}],
    }
    flattened = build_context_from_contact(firestore_doc)

    with patch(
        "services.webhook_router._hydrate_contact_from_firestore",
        return_value=flattened,
    ) as fetch:
        ctx = _payload_to_context({
            "agent_type": "presentation:PA-CURIOSITY",
            "contact_id": "ghl_real",
            "audience": "Reserve Manager + field staff",
            # Webhook-supplied override — wins over the contact-doc city
            "municipality_name": "Override City",
        })

    fetch.assert_called_once_with("ghl_real")
    assert ctx["contact_id"] == "ghl_real"
    assert ctx["first_name"] == "Jamie"
    assert ctx["email"] == "jamie@floridaenet.com"
    assert ctx["contact_notes"] == "Strong intake"
    assert ctx["audience"] == "Reserve Manager + field staff"
    # Webhook payload field beats the contact's city
    assert ctx["municipality_name"] == "Override City"


def test_payload_to_context_swallows_firestore_errors():
    with patch(
        "services.webhook_router._hydrate_contact_from_firestore",
        side_effect=RuntimeError("firestore unavailable"),
    ):
        ctx = _payload_to_context({"contact_id": "ghl_abc", "audience": "team"})
    # Hydration failed but the webhook-supplied fields still come through
    assert ctx["contact_id"] == "ghl_abc"
    assert ctx["audience"] == "team"


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def test_dispatch_no_agent_type_runs_hello_world():
    with patch("services.webhook_router._run_hello_world") as hello, \
         patch("services.webhook_router._run_research") as research, \
         patch("services.webhook_router._run_presentation") as presentation:
        dispatch_ghl_payload({"contact_id": "ghl_1"})
        hello.assert_called_once()
        research.assert_not_called()
        presentation.assert_not_called()


def test_dispatch_hello_world_explicit():
    with patch("services.webhook_router._run_hello_world") as hello:
        dispatch_ghl_payload({"agent_type": "hello_world", "contact_id": "ghl_1"})
        hello.assert_called_once()


def test_dispatch_research_routes_with_normalized_type():
    with patch("services.webhook_router._run_research") as research, \
         patch("services.webhook_router._run_hello_world") as hello:
        dispatch_ghl_payload({
            "agent_type": "research:lobby-1",
            "contact_id": "ghl_2",
            "jurisdiction_name": "Sample County",
        })
        research.assert_called_once()
        type_arg, payload_arg = research.call_args[0]
        assert type_arg == "LOBBY-1"
        # Payload still routed to the branch, but raw shape passes through
        # (the branch itself calls _payload_to_context to hydrate).
        assert payload_arg["jurisdiction_name"] == "Sample County"
        hello.assert_not_called()


def test_dispatch_presentation_routes_with_normalized_type():
    with patch("services.webhook_router._run_presentation") as presentation:
        dispatch_ghl_payload({
            "agent_type": "presentation:pa-step4",
            "contact_id": "ghl_3",
            "municipality_name": "Sample City",
        })
        presentation.assert_called_once()
        type_arg, payload_arg = presentation.call_args[0]
        assert type_arg == "PA-STEP4"
        assert payload_arg["municipality_name"] == "Sample City"


def test_dispatch_scoring_default_score_type():
    with patch("services.webhook_router._run_scoring") as scoring:
        dispatch_ghl_payload({"agent_type": "scoring", "contact_id": "ghl_9"})
        scoring.assert_called_once()
        type_arg, payload_arg = scoring.call_args[0]
        assert type_arg == "PIPELINE-SCORE"
        assert payload_arg["contact_id"] == "ghl_9"


def test_dispatch_scoring_with_explicit_score_type():
    with patch("services.webhook_router._run_scoring") as scoring:
        dispatch_ghl_payload({
            "agent_type": "scoring:pipeline-score",
            "contact_id": "ghl_10",
            "triggered_by": "new_data",
        })
        scoring.assert_called_once()
        type_arg, _ = scoring.call_args[0]
        assert type_arg == "PIPELINE-SCORE"


def test_dispatch_unknown_agent_type_falls_back_to_hello_world():
    with patch("services.webhook_router._run_hello_world") as hello, \
         patch("services.webhook_router._run_research") as research:
        dispatch_ghl_payload({
            "agent_type": "outreach:something",
            "contact_id": "ghl_4",
        })
        hello.assert_called_once()
        research.assert_not_called()


def test_dispatch_never_raises_when_branch_fails():
    with patch(
        "services.webhook_router._run_research",
        side_effect=RuntimeError("simulated agent failure"),
    ):
        # Must not raise — failure is logged and swallowed so the worker
        # thread stays healthy.
        dispatch_ghl_payload({
            "agent_type": "research:LOBBY-1",
            "contact_id": "ghl_5",
        })


def test_dispatch_handles_whitespace_in_agent_type():
    with patch("services.webhook_router._run_presentation") as presentation:
        dispatch_ghl_payload({
            "agent_type": "  presentation: PA-KICKOFF  ",
            "contact_id": "ghl_6",
        })
        presentation.assert_called_once()
        type_arg, _ = presentation.call_args[0]
        assert type_arg == "PA-KICKOFF"
