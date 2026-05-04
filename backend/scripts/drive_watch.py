"""
Drive Sprint 1 spike: create a Drive push-notification channel.

Watches the folder id in DRIVE_WATCH_FOLDER_ID (from .env) and points pushes at
the deployed /webhooks/drive endpoint. Uses service account impersonation for
auth (user credentials → runtime SA with Drive scope). No JSON key needed.

Run from backend/ dir:
    python -m scripts.drive_watch

Prereqs (one-time):
  - `gcloud auth application-default login` with your user account
  - User granted roles/iam.serviceAccountTokenCreator on chawq-api-runtime
  - Drive folder shared with chawq-api-runtime@... as Viewer
  - Drive API enabled on chawq-manatee-matinee
  - DRIVE_WATCH_FOLDER_ID set in backend/.env

Output: channel id, resource id, expiration. Save these — you need them to
stop the watch later via drive.channels().stop(). Channels expire after max
~7 days; re-run this script to renew. Sprint 2 adds a proper renewal cron.
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone

from google.auth import default, impersonated_credentials
from googleapiclient.discovery import build

from core.settings import get_settings


SA_EMAIL = "chawq-api-runtime@chawq-manatee-matinee.iam.gserviceaccount.com"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
DEPLOYED_BASE = "https://chawq-api-783495307551.us-central1.run.app"
WEBHOOK_PATH = "/webhooks/drive"


def main() -> int:
    s = get_settings()

    if not s.drive_watch_folder_id:
        print("ERROR: DRIVE_WATCH_FOLDER_ID not set in .env", file=sys.stderr)
        return 1

    webhook_url = DEPLOYED_BASE + WEBHOOK_PATH
    print(f"Drive watch setup")
    print(f"  folder id      = {s.drive_watch_folder_id}")
    print(f"  webhook url    = {webhook_url}")
    print(f"  impersonate SA = {SA_EMAIL}")

    # --- Auth: user creds -> impersonated runtime SA with Drive scope --------
    source_creds, _ = default()
    target_creds = impersonated_credentials.Credentials(
        source_credentials=source_creds,
        target_principal=SA_EMAIL,
        target_scopes=[DRIVE_SCOPE],
        lifetime=3600,
    )

    drive = build("drive", "v3", credentials=target_creds, cache_discovery=False)

    # --- Verify we can actually see the folder -------------------------------
    print(f"\nVerifying folder access (as the runtime SA)...")
    try:
        meta = (
            drive.files()
            .get(
                fileId=s.drive_watch_folder_id,
                fields="id, name, mimeType",
                supportsAllDrives=True,
            )
            .execute()
        )
    except Exception as e:
        print(f"  FAILED: {e}", file=sys.stderr)
        print(
            "\n  Common causes:\n"
            "    - Folder not shared with the runtime SA email\n"
            "    - Drive API not enabled on the project\n"
            "    - User missing roles/iam.serviceAccountTokenCreator on the SA",
            file=sys.stderr,
        )
        return 1

    print(f"  OK: name={meta.get('name')!r}, mime={meta.get('mimeType')}")
    if meta.get("mimeType") != "application/vnd.google-apps.folder":
        print(f"  WARN: target is not a folder (mimeType={meta.get('mimeType')})")

    # --- Create the watch channel --------------------------------------------
    channel_id = f"chawq-drive-spike-{uuid.uuid4()}"
    body = {
        "id": channel_id,
        "type": "web_hook",
        "address": webhook_url,
        # "token" is an optional arbitrary string Drive will echo back in the
        # X-Goog-Channel-Token header on every push — useful as a shared secret.
        "token": "chawq-drive-spike-v1",
    }
    print(f"\nCreating watch channel...")
    print(f"  channel_id = {channel_id}")

    try:
        result = (
            drive.files()
            .watch(fileId=s.drive_watch_folder_id, body=body, supportsAllDrives=True)
            .execute()
        )
    except Exception as e:
        print(f"  FAILED: {e}", file=sys.stderr)
        return 1

    print(f"\nWatch created:")
    print(f"  channel_id   = {result.get('id')}")
    print(f"  resource_id  = {result.get('resourceId')}")
    print(f"  resource_uri = {result.get('resourceUri')}")
    exp = result.get("expiration")
    if exp:
        exp_dt = datetime.fromtimestamp(int(exp) / 1000, tz=timezone.utc)
        print(f"  expiration   = {exp_dt.isoformat()}  (raw={exp})")
    else:
        print(f"  expiration   = not set (Drive default ~7 days)")

    print("\nSAVE the channel_id + resource_id above — needed to stop the watch.")
    print("An initial 'sync' event should arrive at /webhooks/drive immediately.")
    print("Then edit or add a file in the watched folder to fire a real push.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
