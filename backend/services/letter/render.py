"""
Letter PDF rendering.

Spec-generator + renderer pattern: the LetterAgent emits structured
JSON; this module renders that JSON against a Jinja2 letterhead
template, then WeasyPrint converts the HTML to a PDF byte string.

The HTML template lives at `services/letter/template/letterhead.html`
and carries all the brand styling (Main Blue, Secondary Green, Futura
PT headings, Source Sans 3 body, single-page letter layout, signature
block).

Dependencies:
  - jinja2 (already in requirements.txt for the research agent)
  - weasyprint (added for the letter agent)

WeasyPrint pulls in C-level deps at runtime: pango, cairo, libffi,
glib, gdk-pixbuf, harfbuzz. The Cloud Run base image (python:3.11-slim)
doesn't ship these — see the project's Dockerfile/cloudbuild notes for
the apt-get install line. Locally on macOS: `brew install weasyprint`.

# TODO(signature image): the signed-name line is currently a styled
# cursive-font rendering of the typed name. V2 swaps to an embedded
# PNG handwritten signature once the asset lands. Deferred 5/12.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from agents.letter import LetterResult

logger = logging.getLogger(__name__)


_TEMPLATE_DIR = Path(__file__).resolve().parent / "template"
_TEMPLATE_NAME = "letterhead.html"


# Module-level Jinja env: filesystem loader rooted at the template
# directory, autoescape on for HTML/XML targets. Reused across calls.
_jinja_env: Optional[Environment] = None


def _get_jinja_env() -> Environment:
    """Lazy-load the Jinja env. One per process is plenty."""
    global _jinja_env
    if _jinja_env is None:
        _jinja_env = Environment(
            loader=FileSystemLoader(str(_TEMPLATE_DIR)),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
    return _jinja_env


def render_letter_to_html(
    result: LetterResult,
    *,
    sender_name: Optional[str] = None,
    sender_title: Optional[str] = None,
    sender_email: Optional[str] = None,
    generated_at: Optional[datetime] = None,
) -> str:
    """
    Render the letterhead template against the agent result + sender
    block. Returns the HTML as a string — exposed separately so a
    caller can preview the HTML (or write it alongside the PDF for
    debugging) without going through WeasyPrint.

    `sender_*` args override what's on the result. Useful when the
    runner has a resolved sender that didn't make it into the model's
    structured output (sender info isn't part of the LetterAgent's
    LLM output — it lives on input + result echo).

    `generated_at` defaults to "now in UTC" if not supplied.
    """
    if generated_at is None:
        generated_at = datetime.now(tz=timezone.utc)

    env = _get_jinja_env()
    template = env.get_template(_TEMPLATE_NAME)

    return template.render(
        recipient_name=result.recipient_name,
        recipient_title=result.recipient_title,
        recipient_organization=result.recipient_organization,
        subject_line=result.subject_line,
        opening_paragraph=result.opening_paragraph,
        observation_paragraphs=result.observation_paragraphs,
        ideas_paragraphs=result.ideas_paragraphs,
        offer_paragraph=result.offer_paragraph,
        closing_paragraph=result.closing_paragraph,
        signature_name=result.signature_name,
        sender_name=sender_name or result.sender_name or result.signature_name,
        sender_title=sender_title or result.sender_title,
        sender_email=sender_email or result.sender_email,
        generated_date=generated_at.strftime("%B %d, %Y"),
    )


def render_letter_to_pdf(
    result: LetterResult,
    *,
    sender_name: Optional[str] = None,
    sender_title: Optional[str] = None,
    sender_email: Optional[str] = None,
    generated_at: Optional[datetime] = None,
) -> bytes:
    """
    Render the letter to a PDF byte string ready to upload to Drive.

    The runner calls this once per run and hands the bytes to the Drive
    upload helper. Failure modes:
      - ImportError on weasyprint: surface a clear message (env not set
        up locally). Caller can choose to fall back to HTML-only.
      - WeasyPrint internal error during render: bubble up; caller
        wraps in try/except and sets drive_error on the run result.

    Why bytes and not a tempfile path: WeasyPrint can `write_pdf()` to
    a file path OR return bytes when called as `HTML(string=...).write_pdf()`.
    Bytes are cleaner — no temp-file cleanup, and the Drive upload helper
    already wraps a BytesIO around its content.
    """
    try:
        from weasyprint import HTML
    except ImportError as exc:
        raise RuntimeError(
            "weasyprint is required to render letter PDFs. "
            "Install it locally with `pip install weasyprint` and "
            "ensure the system deps (pango, cairo, libffi, harfbuzz, "
            "gdk-pixbuf, glib) are installed. On the Cloud Run image, "
            "add them via apt-get in the Dockerfile."
        ) from exc

    html_str = render_letter_to_html(
        result,
        sender_name=sender_name,
        sender_title=sender_title,
        sender_email=sender_email,
        generated_at=generated_at,
    )

    # base_url lets the renderer resolve any relative asset paths
    # (future signature image, brand logo). For V1 there are none, but
    # passing the template dir future-proofs the call.
    pdf_bytes = HTML(string=html_str, base_url=str(_TEMPLATE_DIR)).write_pdf()
    if not pdf_bytes:
        raise RuntimeError("weasyprint produced an empty PDF byte string")
    return pdf_bytes
