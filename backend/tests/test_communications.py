"""Tests for the communications layer.

Covers the schema + helpers, the context_builder integration, and the
GHL webhook ingest branch. Firestore writes are patched out so the suite
runs without google-cloud-firestore installed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from services.firestore.communications import (
    Communication,
    make_comm_id,
)


# ---------------------------------------------------------------------------
# Schema + helpers
# ---------------------------------------------------------------------------

def test_make_comm_id_prefers_source_ref():
    assert make_comm_id("ghl", "msg_abc", "body") == "ghl_msg_abc"


def test_make_comm_id_falls_back_to_body_hash():
    a = make_comm_id("manual", None, "hello world")
    b = make_comm_id("manual", None, "hello world")
    c = make_comm_id("manual", None, "different body")
    assert a == b              # same body -> same id (idempotent)
    assert a != c              # different body -> different id
    assert a.startswith("manual_")


def test_make_comm_id_replaces_spaces_in_ref():
    # GHL ids never have spaces but Drive titles might — make sure we
    # produce a Firestore-safe doc id either way.
    assert make_comm_id("drive", "my file id", "x") == "drive_my_file_id"


def test_communication_validates_minimum_fields():
    c = Communication(
        comm_id="ghl_msg_1",
        contact_id="ghl_real",
        channel="email",
        direction="inbound",
        timestamp=datetime.now(tz=timezone.utc),
        body="Hi",
        source="ghl",
    )
    assert c.channel == "email"
    assert c.author is None


def test_communication_rejects_unknown_channel():
    with pytest.raises(Exception):
        Communication(
            comm_id="x",
            contact_id="y",
            channel="carrier_pigeon",  # type: ignore[arg-type]
            direction="inbound",
            timestamp=datetime.now(tz=timezone.utc),
            body="",
            source="manual",
        )


# ---------------------------------------------------------------------------
# context_builder integration
# ---------------------------------------------------------------------------

def test_fetch_communications_truncates_long_bodies():
    from services.scoring_agent import context_builder

    long_body = "x" * 5000
    rows = [{
        "comm_id":    "ghl_msg_1",
        "channel":    "email",
        "direction":  "inbound",
        "timestamp":  datetime.now(tz=timezone.utc),
        "subject":    "hi",
        "author":     "jamie@x.com",
        "body":       long_body,
        "source":     "ghl",
    }]
    with patch(
        "services.firestore.communications.list_communications",
        return_value=rows,
    ):
        out = context_builder._fetch_communications("c1", limit=10)

    assert len(out) == 1
    assert len(out[0]["body"]) < len(long_body)
    assert out[0]["body"].endswith("…[truncated]")
    assert out[0]["channel"] == "email"


def test_fetch_communications_returns_empty_on_failure():
    from services.scoring_agent import context_builder

    with patch(
        "services.firestore.communications.list_communications",
        side_effect=RuntimeError("firestore unreachable"),
    ):
        out = context_builder._fetch_communications("c1", limit=10)
    assert out == []


def test_days_since_last_signal_uses_communications():
    """Communications timestamps should beat older agent_runs."""
    from services.scoring_agent.context_builder import _days_since_last_signal

    now = datetime.now(tz=timezone.utc)
    comms = [{"timestamp": now - timedelta(days=1)}]
    runs  = [{"finished_at": now - timedelta(days=10)}]
    assert _days_since_last_signal(comms, runs) == 1


# ---------------------------------------------------------------------------
# GHL webhook → communications ingest
# ---------------------------------------------------------------------------

def test_dispatch_routes_comm_ingest():
    from services.webhook_router import dispatch_ghl_payload

    with patch("services.webhook_router._ingest_communication") as ingest:
        dispatch_ghl_payload({
            "agent_type": "comm:ingest",
            "contact_id": "ghl_1",
            "messageType": "TYPE_EMAIL",
            "body": "Looking forward to next week.",
            "messageId": "msg_abc",
        })
    ingest.assert_called_once()
    payload = ingest.call_args[0][0]
    assert payload["contact_id"] == "ghl_1"


def test_ingest_communication_maps_ghl_aliases():
    from services.webhook_router import _ingest_communication

    with patch("services.firestore.communications.put_communication") as put:
        _ingest_communication({
            "contact_id":      "ghl_real",
            "messageType":     "TYPE_EMAIL",
            "messageDirection": "inbound",
            "dateAdded":       "2026-05-24T18:30:00Z",
            "emailSubject":    "Re: site walk",
            "emailBody":       "Confirmed for the 12th. Bringing two commissioners.",
            "fromEmail":       "jamie@floridaenet.com",
            "messageId":       "ghl_msg_42",
        })

    put.assert_called_once()
    comm = put.call_args[0][0]
    assert comm.contact_id == "ghl_real"
    assert comm.channel == "email"
    assert comm.direction == "inbound"
    assert comm.subject == "Re: site walk"
    assert comm.author == "jamie@floridaenet.com"
    assert "Confirmed" in comm.body
    assert comm.timestamp.year == 2026
    assert comm.timestamp.month == 5
    # comm_id prefixes the source onto the GHL ref so it's namespaced
    # alongside other sources in the same collection.
    assert comm.comm_id == "ghl_ghl_msg_42"


def test_ingest_communication_skips_when_no_contact_id():
    from services.webhook_router import _ingest_communication

    with patch("services.firestore.communications.put_communication") as put:
        _ingest_communication({
            "messageType": "TYPE_EMAIL",
            "body":        "stranded message — no contact",
        })
    put.assert_not_called()


def test_ingest_communication_skips_empty_body_and_subject():
    from services.webhook_router import _ingest_communication

    with patch("services.firestore.communications.put_communication") as put:
        _ingest_communication({
            "contact_id": "ghl_x",
            "messageType": "TYPE_EMAIL",
            "body": "",
        })
    put.assert_not_called()


def test_ingest_communication_falls_back_to_now_for_bad_timestamp():
    from services.webhook_router import _ingest_communication

    with patch("services.firestore.communications.put_communication") as put:
        _ingest_communication({
            "contact_id": "ghl_x",
            "messageType": "TYPE_SMS",
            "body": "ok",
            "dateAdded": "not-a-date",
        })
    put.assert_called_once()
    comm = put.call_args[0][0]
    # Should default to ~now (within the last minute)
    delta = datetime.now(tz=timezone.utc) - comm.timestamp
    assert delta.total_seconds() < 60


def test_ingest_communication_normalizes_unknown_channel():
    from services.webhook_router import _ingest_communication

    with patch("services.firestore.communications.put_communication") as put:
        _ingest_communication({
            "contact_id": "ghl_x",
            "channel":    "mystery_channel",
            "body":       "hello",
        })
    comm = put.call_args[0][0]
    assert comm.channel == "note"  # safe fallback
