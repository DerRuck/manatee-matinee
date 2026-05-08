"""Upload validated ResearchBriefs to Google Drive.

Auth precedence: DRIVE_SA_EMAIL (impersonate via ADC) > DRIVE_SA_KEY (file) > ADC.

Usage:
    from services.research_agent.drive_sync import upload_brief
    results = upload_brief(brief, folder_id="1L-zcN4jA83EfsrRyei_ewbKOEKMKz-lC")
    # results = {"json": {...Drive file metadata...}, "markdown": {...}}
"""
from __future__ import annotations

import io
import os
import re
import sys

from services.research_agent.schema import ResearchBrief

DEFAULT_FOLDER_ID = "1L-zcN4jA83EfsrRyei_ewbKOEKMKz-lC"


def _slug(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "unknown").lower()).strip("_")


def filename_for(brief: ResearchBrief, ext: str) -> str:
    return (
        f"{_slug(brief.municipality_name)}_"
        f"{brief.research_type_id.lower().replace('-', '_')}_"
        f"v{brief.prompt_version}_{brief.run_id[:8]}.{ext}"
    )


def render_markdown(brief: ResearchBrief) -> str:
    lines = [f"# {brief.research_type_id} — {brief.municipality_name or 'Brief'}", ""]
    lines.append(f"**Generated:** {brief.generated_at.strftime('%B %d, %Y at %H:%M UTC')}")
    lines.append(f"**Run ID:** `{brief.run_id}`")
    lines.append(f"**Overall confidence:** {brief.overall_confidence:.2f}")
    lines.append("")

    f = brief.findings
    if f.research_type == "S6-1":
        lines.append("## Grants\n")
        for i, g in enumerate(f.grants, 1):
            lines.append(f"### {i}. {g.name}")
            lines.append(f"*{g.administering_agency}* (confidence {g.confidence:.2f})\n")
            if g.typical_award_usd_min and g.typical_award_usd_max:
                lines.append(f"- Award: ${g.typical_award_usd_min:,}–${g.typical_award_usd_max:,}")
            lines.append(f"- Deadline: {g.deadline_or_cycle}")
            lines.append(f"- P3: {g.p3_compatible}")
            lines.append(f"- Eligibility: {g.eligibility_summary}")
            lines.append(f"- Page: {g.program_url}\n")
        if f.risks_and_disqualifiers:
            lines.append("## Risks\n")
            for r in f.risks_and_disqualifiers:
                lines.append(f"- **[{r.severity.upper()}]** {r.description}")
                if r.mitigation:
                    lines.append(f"  - Mitigation: {r.mitigation}")

    elif f.research_type == "PW-3":
        if f.leadership:
            lines += ["## Leadership", ""]
            for p in f.leadership:
                lines.append(f"- **{p.name}** — {p.role}")
        if f.environmental_issues:
            lines += ["", "## Environmental Issues", ""]
            for issue in f.environmental_issues:
                lines.append(f"- [{issue.severity.upper()}] {issue.issue}")

    elif f.research_type in ("S3-PREP", "S3-3", "LOBBY-1", "PW-1",
                              "S1-2", "S1-4", "S4-DECK", "S8-1"):
        lines.append(f"## {f.research_type} Brief\n")
        lines.append(f"*See attached JSON for full structured output.*")

    if brief.notes:
        lines += ["", "## Agent Notes", "", brief.notes]
    if brief.sources_consulted:
        lines += ["", "## Sources Consulted", ""]
        for s in brief.sources_consulted:
            lines.append(f"- {s.url} (reliability {s.reliability_score:.2f})")

    return "\n".join(lines)


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
                pass  # static token — nothing to refresh

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


def _upload_or_replace(service, filename: str, content: str,
                        mime_type: str, folder_id: str) -> dict:
    from googleapiclient.http import MediaIoBaseUpload

    media = MediaIoBaseUpload(
        io.BytesIO(content.encode("utf-8")), mimetype=mime_type, resumable=False
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
    upload_json: bool = True,
    upload_markdown: bool = True,
) -> dict[str, dict]:
    """Upload brief as JSON and/or Markdown to Drive. Returns file metadata per format."""
    service = _get_drive_service()
    results: dict[str, dict] = {}

    if upload_json:
        results["json"] = _upload_or_replace(
            service, filename_for(brief, "json"),
            brief.model_dump_json(indent=2), "application/json", folder_id,
        )
    if upload_markdown:
        results["markdown"] = _upload_or_replace(
            service, filename_for(brief, "md"),
            render_markdown(brief), "text/markdown", folder_id,
        )

    return results
