"""Upload validated PresentationOutlines to Google Drive.

Auth and the low-level upload primitive are reused from
services/research_agent/drive_sync so both agents land in the same Drive,
under the same account, with identical idempotency semantics.

Layout produced (root = DEFAULT_FOLDER_ID):
    {root}/{contact folder}/Presentation Outlines/
        pa_curiosity_{run_id8}.json
        pa_curiosity_{run_id8}.docx

The .pptx renderer is deferred — the .docx outline already gives staff a
reviewable deck plan, and downstream tools (python-pptx, Canva, Figma) can
consume the .json directly.

Usage:
    from services.presentation_agent.drive_sync import upload_outline
    result = upload_outline(outline)
    # result = {"json": {...}, "docx": {...}}
"""

from __future__ import annotations

import re
from typing import Any

from services.presentation_agent.schema import PresentationOutline
# Reuse research-agent infra: same auth, same upload-or-replace semantics.
from services.research_agent.drive_sync import (
    DOCX_MIME,
    DEFAULT_FOLDER_ID,
    _get_drive_service,
    _upload_or_replace,
)


# ---------------------------------------------------------------------------
# Filenames
# ---------------------------------------------------------------------------

def _slug(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "unknown").lower()).strip("_")


def filename_for(outline: PresentationOutline, ext: str) -> str:
    return (
        f"{_slug(outline.municipality_name)}_"
        f"{outline.outline_type_id.lower().replace('-', '_')}_"
        f"v{outline.prompt_version}_{outline.run_id[:8]}.{ext}"
    )


# ---------------------------------------------------------------------------
# DOCX renderer — slide-by-slide cards
#
# Staff opens this to review the outline before any deck gets built. Each
# slide is one card with layout-appropriate content + speaker notes. Same
# python-docx pattern as research_agent renderers, simpler structure since
# the slide types are tighter.
# ---------------------------------------------------------------------------

_LAYOUT_LABELS = {
    "title":              "Title",
    "section_divider":    "Section Divider",
    "three_pillar":       "Three Pillars",
    "bullet":             "Bullet Slide",
    "team_bio":           "Team Bios",
    "data_point":         "Data Point",
    "comparable_project": "Comparable Project",
    "quote":              "Quote",
    "closing":            "Closing",
}


def _add_kv(doc: Any, label: str, value: Any) -> None:
    if value is None or value == "":
        return
    p = doc.add_paragraph()
    run = p.add_run(f"{label}: ")
    run.bold = True
    p.add_run(str(value))


def _render_slide_card(doc: Any, slide: Any) -> None:
    layout = slide.layout
    layout_label = _LAYOUT_LABELS.get(layout, layout)

    title = (
        getattr(slide, "title", None)
        or getattr(slide, "section_title", None)
        or getattr(slide, "headline", None)
        or getattr(slide, "project_name", None)
        or getattr(slide, "call_to_action", None)
        or getattr(slide, "quote", None)
        or layout_label
    )
    doc.add_heading(f"Slide {slide.slide_number}: {title}", level=2)
    _add_kv(doc, "Layout", layout_label)

    if layout == "title":
        _add_kv(doc, "Subtitle", slide.subtitle)
        _add_kv(doc, "Date", slide.date_text)
        _add_kv(doc, "Footer", slide.footer_text)

    elif layout == "section_divider":
        _add_kv(doc, "Section #", slide.section_number)
        _add_kv(doc, "Big idea", slide.big_idea)

    elif layout == "three_pillar":
        for i, pillar in enumerate(slide.pillars, 1):
            doc.add_heading(f"Pillar {i}: {pillar.heading}", level=3)
            doc.add_paragraph(pillar.body)

    elif layout == "bullet":
        _add_kv(doc, "Subtitle", slide.subtitle)
        for bullet in slide.bullets:
            doc.add_paragraph(bullet, style="List Bullet")
        for asset in slide.visual_assets:
            doc.add_paragraph(
                f"[visual: {asset.asset_type}] {asset.description}",
                style="Intense Quote",
            )

    elif layout == "team_bio":
        for member in slide.members:
            doc.add_heading(f"{member.name} — {member.role}", level=3)
            if member.bio_one_liner:
                doc.add_paragraph(member.bio_one_liner)
            _add_kv(doc, "Passion", member.passion)
            _add_kv(doc, "Fact", member.fact)

    elif layout == "data_point":
        _add_kv(doc, "Headline", slide.headline)
        _add_kv(doc, "Plain-English framing", slide.plain_english_framing)
        for src in slide.sources:
            _add_kv(doc, "Source", f"{src.title or src.url} (reliability {src.reliability_score})")

    elif layout == "comparable_project":
        _add_kv(doc, "Project", slide.project_name)
        _add_kv(doc, "Municipality", slide.municipality)
        _add_kv(doc, "Year", slide.year)
        if slide.cost_usd:
            _add_kv(doc, "Cost", f"${slide.cost_usd:,}")
        _add_kv(doc, "Outcome", slide.outcome)
        _add_kv(doc, "Why relevant", slide.why_relevant)

    elif layout == "quote":
        p = doc.add_paragraph()
        p.add_run(f"“{slide.quote}”").italic = True
        _add_kv(doc, "Attribution", slide.attribution)
        _add_kv(doc, "Context", slide.context)

    elif layout == "closing":
        _add_kv(doc, "Call to action", slide.call_to_action)
        _add_kv(doc, "Leave-behind", slide.leave_behind_summary)
        _add_kv(doc, "Contact line", slide.contact_line)

    if slide.speaker_notes:
        doc.add_heading("Speaker notes", level=3)
        p = doc.add_paragraph()
        p.add_run(slide.speaker_notes).italic = True


def render_docx(outline: PresentationOutline) -> bytes:
    """Build a .docx outline doc for human review."""
    import io
    from docx import Document

    from services.branding.docx_styles import (
        add_brand_header, add_meta_line, apply_brand_styles,
    )

    doc = Document()
    apply_brand_styles(doc)
    f = outline.findings

    add_brand_header(
        doc,
        title=f.deck_title,
        subtitle=f.deck_subtitle or f"{outline.outline_type_id} · presentation outline",
    )

    add_meta_line(
        doc,
        generated=outline.generated_at.strftime("%B %d, %Y at %H:%M UTC"),
        confidence=outline.overall_confidence,
        run=outline.run_id[:8],
        slides=len(f.slides),
    )

    doc.add_heading("Meeting context", level=1)
    _add_kv(doc, "Outline type", outline.outline_type_id)
    _add_kv(doc, "Audience", f.audience)
    _add_kv(doc, "Objective", f.meeting_objective)
    _add_kv(doc, "Champion", f.champion_name)
    _add_kv(doc, "Municipality", outline.municipality_name)

    if outline.upstream_briefs:
        doc.add_heading("Upstream research briefs", level=1)
        for ref in outline.upstream_briefs:
            line = f"{ref.research_type_id}"
            if ref.run_id:
                line += f" (run {ref.run_id[:8]})"
            if ref.summary:
                line += f" — {ref.summary}"
            doc.add_paragraph(line, style="List Bullet")

    doc.add_heading("Slides", level=1)
    for slide in f.slides:
        _render_slide_card(doc, slide)

    doc.add_heading("Suggested next step", level=1)
    doc.add_paragraph(f.suggested_next_step)

    if outline.notes:
        doc.add_heading("Notes", level=1)
        doc.add_paragraph(outline.notes)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def upload_outline(
    outline: PresentationOutline,
    folder_id: str = DEFAULT_FOLDER_ID,
) -> dict[str, dict]:
    """Upload outline as JSON + Word doc to Drive. Returns file metadata per format.

    Files land in {folder_id}/{contact folder}/Presentation Outlines/. Both
    the contact folder and the "Presentation Outlines" subfolder are
    created on first use and reused on re-runs.
    """
    from services.drive.folders import ensure_subfolder, resolve_contact_folder_name

    service = _get_drive_service()

    contact_folder_id = ensure_subfolder(
        service, folder_id, resolve_contact_folder_name(outline),
    )
    target_folder_id = ensure_subfolder(
        service, contact_folder_id, "Presentation Outlines",
    )

    json_bytes = outline.model_dump_json(indent=2).encode("utf-8")
    docx_bytes = render_docx(outline)

    return {
        "json": _upload_or_replace(
            service, filename_for(outline, "json"),
            json_bytes, "application/json", target_folder_id,
        ),
        "docx": _upload_or_replace(
            service, filename_for(outline, "docx"),
            docx_bytes, DOCX_MIME, target_folder_id,
        ),
    }
