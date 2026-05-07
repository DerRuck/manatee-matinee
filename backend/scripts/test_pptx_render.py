"""
Smoke test for the .pptx renderer.

Builds a small DeckSpec by hand (no agent in the loop), renders it to
backend/scripts/_render_test_output.pptx, and prints the path.

Run from the backend/ dir:
    python -m scripts.test_pptx_render

This exists so the renderer can be validated independently of the
Outliner agent. Once the Outliner prompt lands in Sprint 3, the pairing
test will be: agent emits JSON -> validate against DeckSpec -> render.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running as a script from inside backend/ without -m gymnastics.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.rendering.pptx import render_deck_to_file
from services.rendering.spec import DeckSpec, Slide


def build_sample_spec() -> DeckSpec:
    """A small but realistic deck — title + section + two content slides."""
    return DeckSpec(
        title="Rookery Bay Restoration Brief",
        subtitle="Initial scoping for the Henderson Creek dredging effort",
        author="C-HAWQ",
        municipality="Rookery Bay",
        project_name="Henderson Creek Dredging Scoping",
        slides=[
            Slide(
                layout="title",
                title="Rookery Bay Restoration Brief",
                subtitle="Initial scoping for the Henderson Creek dredging effort",
                speaker_notes=(
                    "Open with the meeting context — this brief was prepared "
                    "after the Apr 18 Taylor Creek consultation."
                ),
            ),
            Slide(
                layout="section",
                title="What we heard",
                subtitle="Key concerns from the field visit",
            ),
            Slide(
                layout="title_content",
                title="Sediment loading is the top concern",
                bullets=[
                    "Reserve staff flag visible turbidity downstream of the inlet",
                    "Existing monitoring runs quarterly; gaps during storm events",
                    "Army Corps coordination is the bottleneck on permit timing",
                    "C-HAWQ funded bathymetry would unlock the Phase 1 scope",
                ],
                speaker_notes=(
                    "Bullets 1 and 2 came from Carol's notes; bullets 3 and 4 "
                    "from the Army Corps follow-up call. Tie back to the "
                    "Exploration Funds pillar without naming it directly."
                ),
            ),
            Slide(
                layout="title_content",
                title="Proposed next steps",
                bullets=[
                    "Run a 30-day continuous turbidity baseline at three sites",
                    "Share the data brief with the Reserve and Army Corps jointly",
                    "Schedule the Phase 1 scoping meeting for early June",
                ],
                speaker_notes="Make the next-step ask concrete and dated.",
            ),
        ],
    )


def main() -> None:
    spec = build_sample_spec()
    out_path = Path(__file__).parent / "_render_test_output.pptx"
    written = render_deck_to_file(spec, out_path)
    print(f"Rendered {len(spec.slides)} slides -> {written}")


if __name__ == "__main__":
    main()
