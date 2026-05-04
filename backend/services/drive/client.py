"""
Google Drive client.

V1 responsibilities:
  - Watch a folder (push notification channel -> /webhooks/drive).
  - Read files in place (never moved, per Guardrail #1).
  - Mirror agent outputs to /CHAWQ/<city>/<project>/<stage>/... folders.

Sprint demo: just upload_text_file() so the Hello World agent's output can
land in the watched Drive folder. Watch-channel + read paths come in later Sprint.

Auth strategy:
  - In Cloud Run (K_SERVICE env set): default() returns the runtime SA's
    creds via the metadata server. We just request the Drive scope.
  - Local dev: default() returns user OAuth creds, which can't directly hit
    Drive (this is the "This app is blocked" wall we hit in the watch spike).
    So we impersonate the runtime SA — same pattern as scripts/drive_watch.py.
"""
from __future__ import annotations

import io
import logging
import os
from typing import Any

from google.auth import default, impersonated_credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from core.settings import Settings, get_settings

logger = logging.getLogger(__name__)


SA_EMAIL = "chawq-api-runtime@chawq-manatee-matinee.iam.gserviceaccount.com"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"


_drive_service: Any | None = None


def _get_drive_service():
    """Lazy-build (and cache) the Drive v3 client."""
    global _drive_service
    if _drive_service is not None:
        return _drive_service

    if os.environ.get("K_SERVICE"):
        creds, _ = default(scopes=[DRIVE_SCOPE])
    else:
        source_creds, _ = default()
        creds = impersonated_credentials.Credentials(
            source_credentials=source_creds,
            target_principal=SA_EMAIL,
            target_scopes=[DRIVE_SCOPE],
            lifetime=3600,
        )

    _drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _drive_service


def upload_text_file(
    folder_id: str,
    filename: str,
    content: str,
    mime_type: str = "text/markdown",
) -> dict[str, str]:
    """
    Upload `content` as a new file named `filename` inside the Drive folder
    `folder_id`. Returns the response dict containing at least:
      - id           : the Drive file ID
      - name         : the final filename
      - webViewLink  : a clickable link to the file in Drive

    Caller is responsible for ensuring the runtime SA has Editor access on
    the target folder. Without it this will fail with insufficientPermissions.
    """
    service = _get_drive_service()
    media = MediaIoBaseUpload(
        io.BytesIO(content.encode("utf-8")),
        mimetype=mime_type,
        resumable=False,
    )
    body = {"name": filename, "parents": [folder_id]}

    response = (
        service.files()
        .create(
            body=body,
            media_body=media,
            fields="id, name, webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )
    return response


class DriveClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._service = None

    async def register_watch(self, folder_id: str, webhook_url: str) -> dict:
        """POST /files/{fileId}/watch — register a push notification channel."""
        raise NotImplementedError("Drive watch registration — Sprint 2 task.")

    async def mirror_file(
        self,
        source_path: str,
        target_folder_path: str,
        filename: str,
    ) -> str:
        """Upload a local file to the target Drive folder. Returns Drive file ID."""
        raise NotImplementedError("Drive mirror write — Sprint 3 one-way sync task.")
