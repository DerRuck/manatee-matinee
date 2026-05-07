"""
Pydantic models for the deck spec that the Presentation Outliner emits
and the .pptx renderer consumes.

The locked agent pattern (memory 2026-04-23): Claude generates a structured
JSON outline; backend code runs python-pptx against a branded template to
produce the actual file. Schema is the contract between the prompt and the
renderer.

Keep this small and obvious. The prompt has to be able to learn the schema
from a short JSON example, and the renderer has to be able to handle
edge cases (missing fields, oversized bullet counts) gracefully.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# Layout types the renderer knows how to draw. New layouts get added here
# AND in services/rendering/pptx.py:_render_slide. Keep these aligned.
SlideLayout = Literal["title", "title_content", "section"]


class Slide(BaseModel):
    """One slide. The Outliner emits a list of these."""

    layout: SlideLayout = "title_content"

    title: str
    subtitle: Optional[str] = None

    # Body content. For title_content slides, bullets render as a vertical
    # bullet list. For title or section slides, bullets are ignored —
    # use subtitle instead. The renderer caps at ~6 bullets per slide for
    # visual sanity; the prompt should aim for 3-5.
    bullets: list[str] = Field(default_factory=list)

    # Speaker notes attached to the slide via python-pptx's notes_slide.
    # Optional but encouraged — gives the human reviewer the "why" behind
    # bullet choices and lets them present without re-reading the deck.
    speaker_notes: Optional[str] = None


class DeckSpec(BaseModel):
    """A full deck. One spec -> one .pptx file."""

    title: str
    subtitle: Optional[str] = None
    author: Optional[str] = None

    # Cosmetic only — written to the .pptx core properties, not rendered.
    # Useful for downstream filtering / search inside Drive.
    municipality: Optional[str] = None
    project_name: Optional[str] = None

    slides: list[Slide]
