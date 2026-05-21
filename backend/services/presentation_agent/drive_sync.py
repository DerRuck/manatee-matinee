"""Upload validated PresentationOutlines to Google Drive.

Auth and the low-level upload primitive are reused from
services/research_agent/drive_sync so both agents land in the same Drive,
under the same account, with identical idempotency semantics.

Layout produced (root = DEFAULT_FOLDER_ID):
    {root}/{contact folder}/Presentation Outlines/
        pa_curiosity_{run_id8}.json
        pa_curiosity_{run_id8}.docx
        pa_curiosity_{run_id8}.html

Three formats land per run:
  - .json  — canonical machine-readable outline (consumed by Canva/Figma/python-pptx)
  - .docx  — reviewable outline for staff (slide-by-slide cards)
  - .html  — self-contained brand-styled deck (one file, opens in any browser,
             prints to PDF, can be screenshared as-is)

Usage:
    from services.presentation_agent.drive_sync import upload_outline
    result = upload_outline(outline)
    # result = {"json": {...}, "docx": {...}, "html": {...}}
"""

from __future__ import annotations

import html as html_lib
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

HTML_MIME = "text/html"


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
    _add_kv(doc, "Audience", getattr(f, "audience", None))
    _add_kv(doc, "Objective", getattr(f, "meeting_objective", None))
    _add_kv(doc, "Champion", getattr(f, "champion_name", None))
    _add_kv(doc, "Municipality", outline.municipality_name)
    _add_kv(doc, "Project", getattr(f, "project_name", None))
    _add_kv(doc, "Problem-area focus", getattr(f, "problem_area_focus", None))

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

    cadence = getattr(f, "communication_cadence", None)
    if cadence:
        doc.add_heading("Communication cadence", level=1)
        doc.add_paragraph(cadence)

    risks = getattr(f, "top_risks", None)
    if risks:
        doc.add_heading("Top risks", level=1)
        for risk in risks:
            doc.add_paragraph(risk, style="List Bullet")

    next_step = getattr(f, "suggested_next_step", None)
    if next_step:
        doc.add_heading("Suggested next step", level=1)
        doc.add_paragraph(next_step)

    if outline.notes:
        doc.add_heading("Notes", level=1)
        doc.add_paragraph(outline.notes)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# HTML renderer — self-contained brand-styled deck
#
# Each slide becomes a 16:9 styled section. CSS lives inline in a single
# <style> block. Google Fonts are loaded via <link> with web-safe fallbacks
# so the file still looks reasonable offline. Open in any browser, print to
# PDF, or screenshare as-is.
#
# Brand palette + typography mirror services/branding/docx_styles.py so the
# Word outline and the HTML deck share one visual language.
# ---------------------------------------------------------------------------

_HTML_LAYOUT_LABELS = {
    "title":              "Cover",
    "section_divider":    "Section",
    "three_pillar":       "Three Pillars",
    "bullet":             "Bullet",
    "team_bio":           "Team",
    "data_point":         "Data Point",
    "comparable_project": "Florida Precedent",
    "quote":              "Quote",
    "closing":            "Closing",
}


def _esc(value: Any) -> str:
    return html_lib.escape("" if value is None else str(value), quote=True)


def _slide_html(slide: Any) -> str:
    layout = slide.layout
    label = _HTML_LAYOUT_LABELS.get(layout, layout)
    parts: list[str] = [
        f'<section class="slide slide--{_esc(layout)}">',
        f'  <div class="slide-chrome"><span class="slide-num">{slide.slide_number}</span>'
        f'<span class="slide-tag">{_esc(label)}</span></div>',
    ]

    if layout == "title":
        parts.append(f'  <h1 class="title-line">{_esc(slide.title)}</h1>')
        if slide.subtitle:
            parts.append(f'  <p class="title-sub">{_esc(slide.subtitle)}</p>')
        if slide.date_text:
            parts.append(f'  <p class="title-date">{_esc(slide.date_text)}</p>')
        parts.append(f'  <p class="title-footer">{_esc(slide.footer_text)}</p>')

    elif layout == "section_divider":
        if slide.section_number is not None:
            parts.append(f'  <p class="section-num">Section {slide.section_number}</p>')
        parts.append(f'  <h2 class="section-title">{_esc(slide.section_title)}</h2>')
        if slide.big_idea:
            parts.append(f'  <p class="section-idea">{_esc(slide.big_idea)}</p>')

    elif layout == "three_pillar":
        parts.append(f'  <h2 class="slide-title">{_esc(slide.title)}</h2>')
        parts.append('  <div class="pillars">')
        for pillar in slide.pillars:
            parts.append(
                '    <div class="pillar">'
                f'<h3>{_esc(pillar.heading)}</h3>'
                f'<p>{_esc(pillar.body)}</p>'
                '</div>'
            )
        parts.append('  </div>')

    elif layout == "bullet":
        parts.append(f'  <h2 class="slide-title">{_esc(slide.title)}</h2>')
        if slide.subtitle:
            parts.append(f'  <p class="slide-sub">{_esc(slide.subtitle)}</p>')
        parts.append('  <ul class="bullets">')
        for bullet in slide.bullets:
            parts.append(f'    <li>{_esc(bullet)}</li>')
        parts.append('  </ul>')
        for asset in slide.visual_assets:
            parts.append(
                '  <p class="visual-hint">'
                f'<span class="visual-tag">[{_esc(asset.asset_type)}]</span> '
                f'{_esc(asset.description)}'
                '</p>'
            )

    elif layout == "team_bio":
        parts.append(f'  <h2 class="slide-title">{_esc(slide.title)}</h2>')
        parts.append('  <div class="team">')
        for member in slide.members:
            parts.append('    <div class="member">')
            parts.append(f'      <h3>{_esc(member.name)}</h3>')
            parts.append(f'      <p class="member-role">{_esc(member.role)}</p>')
            if member.bio_one_liner:
                parts.append(f'      <p>{_esc(member.bio_one_liner)}</p>')
            if member.passion:
                parts.append(
                    f'      <p class="member-kv"><span>Passion:</span> {_esc(member.passion)}</p>'
                )
            if member.fact:
                parts.append(
                    f'      <p class="member-kv"><span>Fact:</span> {_esc(member.fact)}</p>'
                )
            parts.append('    </div>')
        parts.append('  </div>')

    elif layout == "data_point":
        parts.append(f'  <p class="data-headline">{_esc(slide.headline)}</p>')
        parts.append(
            f'  <p class="data-framing">{_esc(slide.plain_english_framing)}</p>'
        )
        parts.append('  <ul class="sources">')
        for src in slide.sources:
            label_text = src.title or str(src.url)
            parts.append(
                '    <li>'
                f'<a href="{_esc(str(src.url))}">{_esc(label_text)}</a>'
                f' <span class="reliability">reliability {src.reliability_score:.2f}</span>'
                '</li>'
            )
        parts.append('  </ul>')

    elif layout == "comparable_project":
        parts.append(f'  <h2 class="slide-title">{_esc(slide.project_name)}</h2>')
        meta = f"{_esc(slide.municipality)} · {slide.year}"
        if slide.cost_usd:
            meta += f" · ${slide.cost_usd:,}"
        parts.append(f'  <p class="comparable-meta">{meta}</p>')
        parts.append(
            f'  <p class="comparable-outcome"><strong>Outcome:</strong> {_esc(slide.outcome)}</p>'
        )
        parts.append(
            f'  <p class="comparable-why"><strong>Why relevant:</strong> {_esc(slide.why_relevant)}</p>'
        )

    elif layout == "quote":
        parts.append(f'  <blockquote class="pull-quote">{_esc(slide.quote)}</blockquote>')
        parts.append(f'  <p class="attribution">— {_esc(slide.attribution)}</p>')
        if slide.context:
            parts.append(f'  <p class="quote-context">{_esc(slide.context)}</p>')

    elif layout == "closing":
        parts.append(f'  <p class="closing-cta">{_esc(slide.call_to_action)}</p>')
        if slide.leave_behind_summary:
            parts.append(f'  <p class="closing-leave">{_esc(slide.leave_behind_summary)}</p>')
        parts.append(f'  <p class="closing-contact">{_esc(slide.contact_line)}</p>')

    if slide.speaker_notes:
        parts.append(
            '  <details class="speaker-notes">'
            '<summary>Speaker notes</summary>'
            f'<p>{_esc(slide.speaker_notes)}</p>'
            '</details>'
        )

    parts.append('</section>')
    return "\n".join(parts)


_HTML_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700&family=Open+Sans:wght@400;600&family=Roboto+Mono:wght@400;500&display=swap');

:root {
  --main-blue: #1f396d;
  --green: #3f886c;
  --sky: #48c5e3;
  --ink: #141f36;
  --subtle: #556688;
  --warning: #c84a3a;
  --bg: #f4f6fa;
  --paper: #ffffff;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  padding: 32px 16px;
  background: var(--bg);
  font-family: 'Open Sans', Arial, sans-serif;
  color: var(--ink);
  line-height: 1.5;
}

.deck-header {
  max-width: 960px;
  margin: 0 auto 24px;
}

.deck-brand-bar {
  background: var(--main-blue);
  color: #fff;
  font-family: 'Poppins', Arial, sans-serif;
  font-weight: 600;
  font-size: 12px;
  padding: 8px 16px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.deck-title {
  font-family: 'Poppins', Arial, sans-serif;
  color: var(--main-blue);
  font-size: 32px;
  margin: 16px 0 4px;
}

.deck-subtitle {
  color: var(--subtle);
  font-style: italic;
  margin: 0 0 12px;
}

.deck-meta {
  font-family: 'Roboto Mono', monospace;
  font-size: 12px;
  color: var(--subtle);
  margin: 0 0 24px;
}

.deck-meta strong {
  font-family: 'Poppins', Arial, sans-serif;
  color: var(--main-blue);
  font-weight: 600;
}

.slide {
  position: relative;
  max-width: 960px;
  margin: 0 auto 24px;
  aspect-ratio: 16 / 9;
  background: var(--paper);
  border-radius: 6px;
  box-shadow: 0 2px 10px rgba(20, 31, 54, 0.08);
  padding: 56px 64px 64px;
  overflow: hidden;
  page-break-after: always;
}

.slide-chrome {
  position: absolute;
  top: 16px;
  right: 24px;
  font-family: 'Roboto Mono', monospace;
  font-size: 11px;
  color: var(--subtle);
  display: flex;
  gap: 8px;
  align-items: center;
}

.slide-num {
  background: var(--main-blue);
  color: #fff;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-weight: 600;
}

.slide-tag {
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.slide-title {
  font-family: 'Poppins', Arial, sans-serif;
  color: var(--main-blue);
  font-size: 32px;
  margin: 0 0 24px;
  line-height: 1.2;
}

.slide-sub {
  color: var(--subtle);
  font-style: italic;
  margin: -16px 0 24px;
}

/* Title slide */
.slide--title {
  background: linear-gradient(135deg, var(--main-blue) 0%, #2a4f8d 100%);
  color: #fff;
  display: flex;
  flex-direction: column;
  justify-content: center;
}
.slide--title .slide-chrome { color: rgba(255,255,255,0.7); }
.slide--title .slide-num { background: rgba(255,255,255,0.2); }
.title-line {
  font-family: 'Poppins', Arial, sans-serif;
  font-size: 48px;
  font-weight: 700;
  margin: 0 0 16px;
  line-height: 1.1;
}
.title-sub { font-size: 20px; opacity: 0.9; margin: 0 0 24px; }
.title-date {
  font-family: 'Roboto Mono', monospace;
  font-size: 14px;
  opacity: 0.8;
  margin: 0;
}
.title-footer {
  position: absolute;
  bottom: 32px;
  left: 64px;
  right: 64px;
  font-size: 11px;
  font-family: 'Roboto Mono', monospace;
  opacity: 0.6;
  margin: 0;
}

/* Section divider */
.slide--section_divider {
  background: var(--main-blue);
  color: #fff;
  display: flex;
  flex-direction: column;
  justify-content: center;
}
.slide--section_divider .slide-chrome { color: rgba(255,255,255,0.7); }
.slide--section_divider .slide-num { background: rgba(255,255,255,0.2); }
.section-num {
  font-family: 'Roboto Mono', monospace;
  font-size: 14px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  opacity: 0.7;
  margin: 0 0 12px;
}
.section-title {
  font-family: 'Poppins', Arial, sans-serif;
  font-size: 42px;
  font-weight: 700;
  margin: 0 0 16px;
  color: #fff;
}
.section-idea {
  font-size: 18px;
  max-width: 640px;
  opacity: 0.9;
  margin: 0;
}

/* Three pillars */
.pillars {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 24px;
  margin-top: 12px;
}
.pillar {
  background: #f7f9fc;
  border-top: 4px solid var(--sky);
  border-radius: 4px;
  padding: 20px;
}
.pillar h3 {
  font-family: 'Poppins', Arial, sans-serif;
  color: var(--main-blue);
  font-size: 16px;
  margin: 0 0 12px;
}
.pillar p { font-size: 14px; margin: 0; color: var(--ink); }

/* Bullets */
.bullets {
  margin: 0;
  padding-left: 24px;
  font-size: 18px;
}
.bullets li { margin-bottom: 10px; }
.visual-hint {
  font-size: 12px;
  color: var(--subtle);
  font-style: italic;
  margin-top: 16px;
}
.visual-tag {
  font-family: 'Roboto Mono', monospace;
  color: var(--sky);
  font-style: normal;
}

/* Team */
.team {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 20px;
}
.member {
  background: #f7f9fc;
  border-radius: 4px;
  padding: 16px;
}
.member h3 {
  font-family: 'Poppins', Arial, sans-serif;
  color: var(--main-blue);
  font-size: 16px;
  margin: 0 0 4px;
}
.member-role {
  color: var(--subtle);
  font-size: 13px;
  margin: 0 0 10px;
}
.member-kv { font-size: 13px; margin: 4px 0; }
.member-kv span {
  font-family: 'Poppins', Arial, sans-serif;
  color: var(--main-blue);
  font-weight: 600;
}

/* Data point */
.data-headline {
  font-family: 'Poppins', Arial, sans-serif;
  color: var(--main-blue);
  font-size: 40px;
  font-weight: 700;
  margin: 24px 0 16px;
  line-height: 1.15;
}
.data-framing {
  font-size: 18px;
  margin: 0 0 24px;
  max-width: 720px;
}
.sources {
  margin-top: 24px;
  padding-left: 20px;
  font-size: 12px;
  color: var(--subtle);
}
.sources a { color: var(--main-blue); text-decoration: none; }
.sources a:hover { text-decoration: underline; }
.reliability {
  font-family: 'Roboto Mono', monospace;
  margin-left: 6px;
}

/* Comparable project */
.comparable-meta {
  font-family: 'Roboto Mono', monospace;
  color: var(--subtle);
  margin: 0 0 20px;
}
.comparable-outcome, .comparable-why {
  font-size: 16px;
  margin: 0 0 12px;
  max-width: 720px;
}

/* Quote */
.slide--quote { display: flex; flex-direction: column; justify-content: center; }
.pull-quote {
  font-family: 'Poppins', Arial, sans-serif;
  font-size: 28px;
  font-style: italic;
  color: var(--main-blue);
  border-left: 4px solid var(--sky);
  padding-left: 20px;
  margin: 0 0 16px;
  line-height: 1.3;
}
.attribution {
  font-size: 14px;
  color: var(--subtle);
  margin: 0 0 4px;
}
.quote-context {
  font-size: 12px;
  color: var(--subtle);
  font-style: italic;
  margin: 0;
}

/* Closing */
.slide--closing {
  background: linear-gradient(135deg, #3f886c 0%, #2f6c54 100%);
  color: #fff;
  display: flex;
  flex-direction: column;
  justify-content: center;
}
.slide--closing .slide-chrome { color: rgba(255,255,255,0.7); }
.slide--closing .slide-num { background: rgba(255,255,255,0.2); }
.closing-cta {
  font-family: 'Poppins', Arial, sans-serif;
  font-size: 36px;
  font-weight: 700;
  margin: 0 0 24px;
  line-height: 1.2;
}
.closing-leave {
  font-size: 18px;
  max-width: 720px;
  margin: 0 0 32px;
  opacity: 0.95;
}
.closing-contact {
  font-family: 'Roboto Mono', monospace;
  font-size: 14px;
  opacity: 0.85;
  margin: 0;
}

/* Speaker notes — collapsed by default */
.speaker-notes {
  margin-top: 20px;
  font-size: 12px;
  color: var(--subtle);
}
.speaker-notes summary {
  cursor: pointer;
  font-family: 'Poppins', Arial, sans-serif;
  font-weight: 600;
  color: var(--main-blue);
}
.speaker-notes p {
  margin: 8px 0 0;
  padding-left: 12px;
  border-left: 2px solid var(--sky);
  font-style: italic;
}

/* Appendix — operational extras (kickoff cadence, risks, next step) */
.appendix {
  max-width: 960px;
  margin: 24px auto 0;
  padding: 0 16px;
}
.appendix-block {
  background: var(--paper);
  border-left: 4px solid var(--green);
  border-radius: 4px;
  padding: 20px 24px;
  margin-bottom: 16px;
  box-shadow: 0 1px 4px rgba(20, 31, 54, 0.04);
}
.appendix-block h2 {
  font-family: 'Poppins', Arial, sans-serif;
  color: var(--main-blue);
  font-size: 18px;
  margin: 0 0 8px;
}
.appendix-block p, .appendix-block li {
  font-size: 14px;
  margin: 4px 0;
}

@media print {
  body { background: #fff; padding: 0; }
  .deck-header { padding: 0 24px; }
  .slide { box-shadow: none; margin: 0; border-radius: 0; }
  .speaker-notes { display: none; }
  .appendix { page-break-before: always; }
}
"""


def render_html(outline: PresentationOutline) -> bytes:
    """Build a self-contained brand-styled HTML deck.

    One file, opens in any browser, prints to PDF cleanly. Each slide is a
    16:9 styled section. Speaker notes collapse into a <details> per slide.
    """
    f = outline.findings

    slides_html = "\n".join(_slide_html(s) for s in f.slides)

    head = (
        '<!DOCTYPE html>\n'
        '<html lang="en">\n'
        '<head>\n'
        '<meta charset="utf-8">\n'
        f'<title>{_esc(f.deck_title)}</title>\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<style>{_HTML_CSS}</style>\n'
        '</head>\n'
        '<body>\n'
    )

    confidence_pct = int(round(outline.overall_confidence * 100))
    meta_bits = [
        f'<strong>Outline:</strong> {_esc(outline.outline_type_id)} v{outline.prompt_version}',
        f'<strong>Run:</strong> {_esc(outline.run_id[:8])}',
        f'<strong>Slides:</strong> {len(f.slides)}',
        f'<strong>Confidence:</strong> {confidence_pct}%',
    ]
    if outline.generated_at:
        meta_bits.append(
            f'<strong>Generated:</strong> {outline.generated_at.strftime("%B %d, %Y")}'
        )

    header = (
        '<header class="deck-header">\n'
        '  <div class="deck-brand-bar">C-HAWQ — Coastal Habitat and Water Quality Initiative</div>\n'
        f'  <h1 class="deck-title">{_esc(f.deck_title)}</h1>\n'
    )
    if f.deck_subtitle:
        header += f'  <p class="deck-subtitle">{_esc(f.deck_subtitle)}</p>\n'
    header += f'  <p class="deck-meta">{"   ·   ".join(meta_bits)}</p>\n</header>\n'

    body = '<main class="deck">\n' + slides_html + '\n</main>\n'

    appendix_bits: list[str] = []
    cadence = getattr(f, "communication_cadence", None)
    if cadence:
        appendix_bits.append(
            '<section class="appendix-block">'
            '<h2>Communication cadence</h2>'
            f'<p>{_esc(cadence)}</p>'
            '</section>'
        )
    risks = getattr(f, "top_risks", None) or []
    if risks:
        risk_items = "".join(f"<li>{_esc(r)}</li>" for r in risks)
        appendix_bits.append(
            '<section class="appendix-block">'
            '<h2>Top risks</h2>'
            f'<ul>{risk_items}</ul>'
            '</section>'
        )
    next_step = getattr(f, "suggested_next_step", None)
    if next_step:
        appendix_bits.append(
            '<section class="appendix-block">'
            '<h2>Suggested next step</h2>'
            f'<p>{_esc(next_step)}</p>'
            '</section>'
        )

    appendix = (
        '<aside class="appendix">\n' + "\n".join(appendix_bits) + '\n</aside>\n'
        if appendix_bits else ""
    )

    foot = '</body>\n</html>\n'

    return (head + header + body + appendix + foot).encode("utf-8")


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def upload_outline(
    outline: PresentationOutline,
    folder_id: str = DEFAULT_FOLDER_ID,
) -> dict[str, dict]:
    """Upload outline as JSON + Word doc + HTML deck to Drive.

    Returns file metadata per format. Files land in
    {folder_id}/{contact folder}/Presentation Outlines/. The contact folder
    and "Presentation Outlines" subfolder are created on first use and
    reused on re-runs.
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
    html_bytes = render_html(outline)

    return {
        "json": _upload_or_replace(
            service, filename_for(outline, "json"),
            json_bytes, "application/json", target_folder_id,
        ),
        "docx": _upload_or_replace(
            service, filename_for(outline, "docx"),
            docx_bytes, DOCX_MIME, target_folder_id,
        ),
        "html": _upload_or_replace(
            service, filename_for(outline, "html"),
            html_bytes, HTML_MIME, target_folder_id,
        ),
    }
