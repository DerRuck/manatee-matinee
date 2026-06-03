"""
Feedback agent + runner tests.

Three layers, all offline (no Anthropic, Firestore, or Drive calls):
  - _parse_feedback_json: the model-output contract (closed category set,
    sentiment enum, bool actionable, fence stripping).
  - _unified_diff: diff extraction is real difflib, just confirm shape.
  - run_feedback_for_lead: orchestration — resolves the original run, extracts
    a diff, categorizes (mocked), writes the feedback record + back-link.

Run from `backend/`:  pytest -q
"""
from unittest.mock import MagicMock, patch

import pytest

from agents.feedback_agent.agent import (
    FeedbackResult,
    _parse_feedback_json,
)
from services.feedback_runner import _unified_diff, run_feedback_for_lead


# --- _parse_feedback_json ---------------------------------------------------

def test_parse_valid():
    raw = (
        '{"categories": ["tone", "length"], "sentiment": "negative", '
        '"summary": "too formal and too long", "actionable": true}'
    )
    data = _parse_feedback_json(raw)
    assert data["categories"] == ["tone", "length"]
    assert data["sentiment"] == "negative"
    assert data["actionable"] is True


def test_parse_strips_code_fence():
    raw = (
        '```json\n{"categories": ["no_change"], "sentiment": "positive", '
        '"summary": "approved as-is", "actionable": false}\n```'
    )
    data = _parse_feedback_json(raw)
    assert data["categories"] == ["no_change"]
    assert data["actionable"] is False


def test_parse_rejects_unknown_category():
    raw = (
        '{"categories": ["vibes"], "sentiment": "neutral", '
        '"summary": "x", "actionable": true}'
    )
    with pytest.raises(ValueError, match="closed set"):
        _parse_feedback_json(raw)


def test_parse_rejects_bad_sentiment():
    raw = (
        '{"categories": [], "sentiment": "angry", '
        '"summary": "x", "actionable": true}'
    )
    with pytest.raises(ValueError, match="sentiment"):
        _parse_feedback_json(raw)


def test_parse_rejects_missing_field():
    raw = '{"categories": [], "sentiment": "neutral", "summary": "x"}'
    with pytest.raises(ValueError, match="missing required fields"):
        _parse_feedback_json(raw)


def test_parse_rejects_non_bool_actionable():
    raw = (
        '{"categories": [], "sentiment": "neutral", '
        '"summary": "x", "actionable": "yes"}'
    )
    with pytest.raises(ValueError, match="actionable"):
        _parse_feedback_json(raw)


def test_parse_rejects_garbage():
    with pytest.raises(ValueError, match="not valid JSON"):
        _parse_feedback_json("not json at all")


# --- _unified_diff ----------------------------------------------------------

def test_unified_diff_shows_change():
    diff = _unified_diff("Hi Nick,\nLong opener.\n", "Hi Nick,\nShort.\n")
    assert "-Long opener." in diff
    assert "+Short." in diff


def test_unified_diff_identical_is_empty():
    assert _unified_diff("same\n", "same\n") == ""


# --- run_feedback_for_lead (orchestration) ----------------------------------

def _fake_result():
    return FeedbackResult(
        categories=["tone"],
        sentiment="negative",
        summary="too formal",
        actionable=True,
        raw_model_output="{}",
        input_tokens=10,
        output_tokens=5,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        prompt_version=1,
        model="claude-sonnet-4-6",
    )


@patch("services.feedback_runner.put_agent_run")
@patch("services.feedback_runner.link_feedback_to_run")
@patch("services.feedback_runner.put_feedback")
@patch("services.feedback_runner.download_file_as_text", return_value="Hi Nick,\nLong opener.\n")
@patch("services.feedback_runner.get_file_metadata", return_value={"mimeType": "text/markdown", "name": "draft.md"})
@patch("services.feedback_runner.get_agent_run")
@patch("services.feedback_runner.FeedbackAgent")
def test_run_with_diff_links_and_writes(
    mock_agent_cls,
    mock_get_run,
    _mock_meta,
    _mock_download,
    mock_put_feedback,
    mock_link,
    _mock_put_run,
):
    mock_get_run.return_value = {
        "run_id": "orig-1",
        "agent": "email_drafter",
        "contact_id": "contact-9",
        "drive_file_id": "drive-abc",
    }
    mock_agent_cls.return_value.run_for_feedback.return_value = _fake_result()

    res = run_feedback_for_lead(
        {
            "run_id": "orig-1",
            "reaction": "edits_requested",
            "note": "too formal, shorten the opener",
            "revised_text": "Hi Nick,\nShort.\n",
        },
        run_id="fb-1",
    )

    assert res.status == "completed"
    assert res.original_run_found is True
    assert res.contact_id == "contact-9"
    assert res.original_agent == "email_drafter"
    assert res.diff and "+Short." in res.diff

    # Schema link: feedback record carries original_run_id; back-link fired.
    mock_put_feedback.assert_called_once()
    fb_id, record = mock_put_feedback.call_args.args
    assert fb_id == "fb-1"
    assert record["original_run_id"] == "orig-1"
    assert record["contact_id"] == "contact-9"
    assert record["categories"] == ["tone"]
    assert record["has_diff"] is True
    mock_link.assert_called_once_with("orig-1", "fb-1")


@patch("services.feedback_runner.put_agent_run")
@patch("services.feedback_runner.link_feedback_to_run")
@patch("services.feedback_runner.put_feedback")
@patch("services.feedback_runner.get_agent_run", return_value=None)
@patch("services.feedback_runner.FeedbackAgent")
def test_run_missing_original_still_categorizes(
    mock_agent_cls, _mock_get_run, mock_put_feedback, mock_link, _mock_put_run
):
    mock_agent_cls.return_value.run_for_feedback.return_value = _fake_result()

    res = run_feedback_for_lead(
        {"run_id": "ghost", "reaction": "rejected", "note": "off-brand"},
        run_id="fb-2",
    )

    # No diff requested, but the original run wasn't found → partial, and we
    # never try to back-link a run that doesn't exist.
    assert res.status == "partial"
    assert res.original_run_found is False
    assert any("not found" in w for w in res.warnings)
    mock_put_feedback.assert_called_once()
    mock_link.assert_not_called()


@patch("services.feedback_runner.put_agent_run")
@patch("services.feedback_runner.put_feedback")
@patch("services.feedback_runner.get_agent_run", return_value={"agent": "research", "contact_id": "c1"})
@patch("services.feedback_runner.FeedbackAgent")
def test_run_failed_model_call_records_failed(
    mock_agent_cls, _mock_get_run, _mock_put_feedback, _mock_put_run
):
    mock_agent_cls.return_value.run_for_feedback.side_effect = RuntimeError("boom")

    res = run_feedback_for_lead(
        {"run_id": "orig-x", "reaction": "approved"}, run_id="fb-3"
    )
    assert res.status == "failed"
    assert "RuntimeError" in res.error
