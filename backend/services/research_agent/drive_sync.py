"""Upload validated ResearchBriefs to Google Drive as Word documents.

Auth precedence: DRIVE_SA_EMAIL (impersonate via ADC) > DRIVE_SA_KEY (file) > ADC.

Usage:
    from services.research_agent.drive_sync import upload_brief
    result = upload_brief(brief, folder_id="1L-zcN4jA83EfsrRyei_ewbKOEKMKz-lC")
    # result = {"docx": {...Drive file metadata...}}
"""
from __future__ import annotations

import io
import os
import re
import sys
from typing import Any

from services.research_agent.schema import ResearchBrief

DEFAULT_FOLDER_ID = "1L-zcN4jA83EfsrRyei_ewbKOEKMKz-lC"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Fields that carry no human value and should never appear in the document.
_SKIP_ALWAYS = {"research_type"}

# Threshold (chars) above which a string gets its own heading + paragraph
# rather than being rendered as "Label: value" on one line.
_PROSE_THRESHOLD = 120


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def _slug(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "unknown").lower()).strip("_")


def filename_for(brief: ResearchBrief, ext: str) -> str:
    return (
        f"{_slug(brief.municipality_name)}_"
        f"{brief.research_type_id.lower().replace('-', '_')}_"
        f"v{brief.prompt_version}_{brief.run_id[:8]}.{ext}"
    )


# ---------------------------------------------------------------------------
# Word document renderer
# ---------------------------------------------------------------------------

def _label(field_name: str) -> str:
    """snake_case → Title Case for display."""
    return field_name.replace("_", " ").title()


def _item_heading(obj: Any, index: int) -> str:
    """Pick the most descriptive field from a list-item model for its heading."""
    for attr in (
        "name", "outlet_name", "contact_name", "firm_name",
        "section_title", "requirement", "criterion", "factor",
        "question", "objection", "pitfall", "standard_term",
        "term_or_section", "action", "task", "department",
        "slide_number", "week",
    ):
        val = getattr(obj, attr, None)
        if val is not None:
            return str(val)
    return f"Item {index}"


def _render_value(doc: Any, value: Any, level: int) -> None:
    """Recursively render a value into *doc* at heading *level*."""
    from pydantic import BaseModel

    if value is None or value == "" or value == []:
        return

    if isinstance(value, str):
        doc.add_paragraph(value)

    elif isinstance(value, (int, float, bool)):
        doc.add_paragraph(str(value))

    elif isinstance(value, list):
        if all(isinstance(v, str) for v in value):
            for item in value:
                doc.add_paragraph(item, style="List Bullet")
        else:
            for i, item in enumerate(value, 1):
                if isinstance(item, BaseModel):
                    heading_text = _item_heading(item, i)
                    doc.add_heading(heading_text, min(level, 9))
                    _render_model(doc, item, level + 1, skip_fields=set())
                else:
                    doc.add_paragraph(str(item), style="List Bullet")

    elif isinstance(value, BaseModel):
        _render_model(doc, value, level, skip_fields=set())

    else:
        doc.add_paragraph(str(value))


def _render_model(doc: Any, model: Any, level: int, skip_fields: set) -> None:
    """Render all fields of a Pydantic model into *doc*."""
    from pydantic import BaseModel

    for field_name in model.model_fields:
        if field_name in skip_fields:
            continue
        value = getattr(model, field_name)
        if value is None or value == "" or value == []:
            continue

        field_label = _label(field_name)

        if isinstance(value, str) and len(value) > _PROSE_THRESHOLD:
            doc.add_heading(field_label, min(level, 9))
            doc.add_paragraph(value)

        elif isinstance(value, str):
            p = doc.add_paragraph()
            p.add_run(f"{field_label}: ").bold = True
            p.add_run(value)

        elif isinstance(value, (int, float)):
            p = doc.add_paragraph()
            p.add_run(f"{field_label}: ").bold = True
            p.add_run(str(value))

        elif isinstance(value, bool):
            p = doc.add_paragraph()
            p.add_run(f"{field_label}: ").bold = True
            p.add_run("Yes" if value else "No")

        elif isinstance(value, list) and value:
            doc.add_heading(field_label, min(level, 9))
            _render_value(doc, value, level + 1)

        elif isinstance(value, BaseModel):
            doc.add_heading(field_label, min(level, 9))
            _render_model(doc, value, level + 1, skip_fields=set())


def render_docx(brief: ResearchBrief) -> bytes:
    """Render a ResearchBrief as a Word document and return the raw bytes."""
    from docx import Document

    doc = Document()

    # ---- Title and metadata ------------------------------------------------
    doc.add_heading(
        f"{brief.research_type_id} — {brief.municipality_name or 'Brief'}", 0
    )

    meta = doc.add_paragraph()
    meta.add_run("Generated: ").bold = True
    meta.add_run(brief.generated_at.strftime("%B %d, %Y at %H:%M UTC"))
    meta.add_run("     Confidence: ").bold = True
    meta.add_run(f"{brief.overall_confidence:.2f}")
    meta.add_run("     Run: ").bold = True
    meta.add_run(brief.run_id[:8])

    # ---- Findings ----------------------------------------------------------
    doc.add_heading("Findings", 1)
    _render_model(doc, brief.findings, level=2, skip_fields=_SKIP_ALWAYS)

    # ---- Agent notes -------------------------------------------------------
    if brief.notes:
        doc.add_heading("Notes", 1)
        doc.add_paragraph(brief.notes)

    # ---- Sources -----------------------------------------------------------
    if brief.sources_consulted:
        doc.add_heading("Sources Consulted", 1)
        for s in brief.sources_consulted:
            doc.add_paragraph(
                f"{s.url}  (reliability {s.reliability_score:.2f})",
                style="List Bullet",
            )

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Drive upload
# ---------------------------------------------------------------------------

def _get_drive_service():
    try:
        from googleapiclient.discovery import build
    except ImportError:
        print("ERROR: pip install google-api-python-client google-auth")
        sys.exit(1)

    access_token = os.environ.get("DRIVE_ACCESS_TOKEN")
    sa_email = os.environ.get("DRIVE_SA_EMAIL")
    sa_key = os.environ.get("DRIVE_SA_KEY")

    if access_token:
        import google.auth.credentials

        class _StaticToken(google.auth.credentials.Credentials):
            def __init__(self, token):
                super().__init__()
                self.token = token

            @property
            def valid(self):
                return True

            @property
            def expired(self):
                return False

            def refresh(self, request):
                pass

            def before_request(self, request, method, url, headers):
                self.apply(headers)

        credentials = _StaticToken(access_token)
    elif sa_email:
        from google.auth import default, impersonated_credentials
        source, _ = default()
        credentials = impersonated_credentials.Credentials(
            source_credentials=source,
            target_principal=sa_email,
            target_scopes=["https://www.googleapis.com/auth/drive"],
            lifetime=3600,
        )
    elif sa_key:
        from google.oauth2 import service_account
        credentials = service_account.Credentials.from_service_account_file(
            sa_key, scopes=["https://www.googleapis.com/auth/drive"]
        )
    else:
        from google.auth import default
        credentials, _ = default(scopes=["https://www.googleapis.com/auth/drive"])

    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _upload_or_replace(
    service, filename: str, content: bytes, mime_type: str, folder_id: str
) -> dict:
    from googleapiclient.http import MediaIoBaseUpload

    media = MediaIoBaseUpload(
        io.BytesIO(content), mimetype=mime_type, resumable=False
    )
    existing = service.files().list(
        q=f"name = '{filename}' and '{folder_id}' in parents and trashed = false",
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute().get("files", [])

    if existing:
        return service.files().update(
            fileId=existing[0]["id"],
            media_body=media,
            fields="id, name, webViewLink, modifiedTime",
            supportsAllDrives=True,
        ).execute()

    return service.files().create(
        body={"name": filename, "parents": [folder_id]},
        media_body=media,
        fields="id, name, webViewLink, createdTime",
        supportsAllDrives=True,
    ).execute()


def upload_brief(
    brief: ResearchBrief,
    folder_id: str = DEFAULT_FOLDER_ID,
) -> dict[str, dict]:
    """Upload brief as JSON + Word doc to Drive. Returns file metadata per format."""
    service = _get_drive_service()

    json_bytes = brief.model_dump_json(indent=2).encode("utf-8")
    docx_bytes = render_docx(brief)

    return {
        "json": _upload_or_replace(
            service, filename_for(brief, "json"),
            json_bytes, "application/json", folder_id,
        ),
        "docx": _upload_or_replace(
            service, filename_for(brief, "docx"),
            docx_bytes, DOCX_MIME, folder_id,
        ),
    }
