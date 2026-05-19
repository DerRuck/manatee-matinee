"""
Unit tests for services/drive/folders.

Tests run without real Drive credentials by mocking the service client. The
goal is to verify the find-or-create logic, the cache, the query escaping,
and the contact-folder fallback chain — not the Drive API itself.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from services.drive.folders import (
    clear_folder_cache,
    ensure_subfolder,
    normalize_folder_name,
    resolve_contact_folder_name,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    clear_folder_cache()
    yield
    clear_folder_cache()


def _make_service(*, list_result: list[dict] | None = None, create_id: str = "new_id"):
    """Build a mock Drive service whose .files().list().execute() returns
    {"files": list_result} and whose .files().create().execute() returns
    {"id": create_id, "name": "..."}.
    """
    service = MagicMock()
    files = service.files.return_value

    files.list.return_value.execute.return_value = {"files": list_result or []}
    files.create.return_value.execute.return_value = {"id": create_id, "name": "x"}

    return service


# ---------------------------------------------------------------------------
# normalize_folder_name
# ---------------------------------------------------------------------------

def test_normalize_strips_path_separators():
    assert normalize_folder_name("Foo/Bar\\Baz") == "Foo Bar Baz"


def test_normalize_collapses_whitespace_and_trims():
    assert normalize_folder_name("  Rookery   Bay\n NERR  ") == "Rookery Bay NERR"


def test_normalize_empty_becomes_misc():
    assert normalize_folder_name("") == "Misc"
    assert normalize_folder_name("   ") == "Misc"


# ---------------------------------------------------------------------------
# ensure_subfolder
# ---------------------------------------------------------------------------

def test_ensure_subfolder_creates_when_missing():
    service = _make_service(list_result=[], create_id="created_42")
    fid = ensure_subfolder(service, "parent_1", "Rookery Bay")
    assert fid == "created_42"

    service.files().create.assert_called_once()
    create_args = service.files().create.call_args.kwargs
    assert create_args["body"]["name"] == "Rookery Bay"
    assert create_args["body"]["parents"] == ["parent_1"]
    assert create_args["body"]["mimeType"] == "application/vnd.google-apps.folder"


def test_ensure_subfolder_returns_existing():
    service = _make_service(list_result=[{"id": "existing_99", "name": "Rookery Bay"}])
    fid = ensure_subfolder(service, "parent_1", "Rookery Bay")
    assert fid == "existing_99"
    service.files().create.assert_not_called()


def test_ensure_subfolder_caches_within_process():
    service = _make_service(list_result=[], create_id="created_once")
    a = ensure_subfolder(service, "parent_1", "Rookery Bay")
    b = ensure_subfolder(service, "parent_1", "Rookery Bay")
    assert a == b == "created_once"
    assert service.files().list.call_count == 1
    assert service.files().create.call_count == 1


def test_ensure_subfolder_escapes_single_quotes():
    service = _make_service(list_result=[], create_id="ok")
    ensure_subfolder(service, "parent_1", "O'Hara Bay")
    q_arg = service.files().list.call_args.kwargs["q"]
    # The literal must contain the escaped quote — otherwise the query is malformed
    # and could be exploited by a crafted contact name.
    assert "O\\'Hara Bay" in q_arg


def test_ensure_subfolder_normalizes_name_before_create():
    service = _make_service(list_result=[], create_id="ok")
    ensure_subfolder(service, "parent_1", "  Rookery   Bay  ")
    body = service.files().create.call_args.kwargs["body"]
    assert body["name"] == "Rookery Bay"


# ---------------------------------------------------------------------------
# resolve_contact_folder_name fallback chain
# ---------------------------------------------------------------------------

class _Obj:
    """Minimal stand-in for a brief/outline envelope."""
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_resolve_prefers_municipality_name():
    obj = _Obj(municipality_name="Sarasota", contact_id="ghl_001")
    assert resolve_contact_folder_name(obj) == "Sarasota"


def test_resolve_falls_back_through_chain():
    # PW-1 conferences: municipality_name is None, conference_name on findings
    findings = _Obj(conference_name="FSBPA 2026 Annual Conference")
    obj = _Obj(municipality_name=None, contact_id="ghl_002", findings=findings)
    assert resolve_contact_folder_name(obj) == "FSBPA 2026 Annual Conference"


def test_resolve_uses_jurisdiction_when_no_municipality():
    findings = _Obj(jurisdiction_name="Alachua County")
    obj = _Obj(municipality_name=None, findings=findings)
    assert resolve_contact_folder_name(obj) == "Alachua County"


def test_resolve_final_fallback_to_contact_id():
    obj = _Obj(municipality_name=None, contact_id="ghl_999")
    assert resolve_contact_folder_name(obj) == "ghl_999"


def test_resolve_returns_misc_when_nothing_usable():
    obj = _Obj(municipality_name=None, contact_id=None)
    assert resolve_contact_folder_name(obj) == "Misc"
