"""
Letter Agent end-to-end smoke.

Runs the orchestrated pipeline (agent → PDF render → Drive PDF + audit
.md → agent_runs) on one lead. Defaults to a Rookery Bay lead so you
can run it without typing all the args; override anything via flags.

Usage from backend/:
    python -m scripts.letter_smoke

    # Custom lead
    python -m scripts.letter_smoke \
        --first-name Jane --last-name Doe \
        --organization "SFWMD" --municipality sfwmd_fl \
        --sender-name "Tyler Q. Author" --sender-title "Lead Scientist" \
        --sender-email "tyler@chawq.org" \
        --triggering-event "Florida Stormwater Conference 2026" \
        --triggering-event-summary "Discussed regional canal sediment loading."

    # Prompt-iteration mode — skips PDF render and Drive writes
    python -m scripts.letter_smoke --no-pdf --no-record

Output: prints the JSON model output, the PDF link (if written), the
audit-md link (if written). Inspects the result's status field —
`completed` means everything landed; `partial` means the model ran but
at least one side-effect failed; `failed` means the agent itself
errored.

Prereqs:
  - .env: GCP_PROJECT_ID, ANTHROPIC_API_KEY, DRIVE_OUTPUT_ROOT_FOLDER_ID.
  - WeasyPrint system deps installed (pango, cairo, libffi, glib,
    gdk-pixbuf, harfbuzz). On macOS: `brew install weasyprint`. On
    Cloud Run: apt-get install in the Dockerfile.
  - Local IAM: roles/iam.serviceAccountTokenCreator on chawq-api-runtime
    for whichever user `gcloud auth list` reports as active.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow running as a script without -m gymnastics.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from agents.letter import LetterInput  # noqa: E402
from services.letter_runner import run_letter_for_lead  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("letter_smoke")


# Hardcoded Rookery Bay defaults — easy to invoke without flags.
DEFAULT_INPUT = LetterInput(
    contact_id="rookery_bay_smoke_contact",
    contact_first_name="Jane",
    contact_last_name="Doe",
    contact_title="Stewardship Coordinator",
    contact_organization="Rookery Bay NERR",
    contact_municipality="rookery_bay_fl",
    triggering_event="Florida Stormwater Conference 2026",
    triggering_event_date="2026-05-05",
    triggering_event_summary=(
        "Discussed canal sediment loading on the south estuary and the "
        "regional partner network for habitat monitoring."
    ),
    sender_name="Tyler Q. Author",
    sender_title="Lead Scientist, C-HAWQ",
    sender_email="tyler@chawq.org",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--contact-id", default=DEFAULT_INPUT.contact_id)
    parser.add_argument("--first-name", default=DEFAULT_INPUT.contact_first_name)
    parser.add_argument("--last-name", default=DEFAULT_INPUT.contact_last_name)
    parser.add_argument("--title", default=DEFAULT_INPUT.contact_title)
    parser.add_argument("--organization", default=DEFAULT_INPUT.contact_organization)
    parser.add_argument("--municipality", default=DEFAULT_INPUT.contact_municipality)
    parser.add_argument("--sender-name", default=DEFAULT_INPUT.sender_name)
    parser.add_argument("--sender-title", default=DEFAULT_INPUT.sender_title)
    parser.add_argument("--sender-email", default=DEFAULT_INPUT.sender_email)
    parser.add_argument(
        "--triggering-event", default=DEFAULT_INPUT.triggering_event
    )
    parser.add_argument(
        "--triggering-event-date", default=DEFAULT_INPUT.triggering_event_date
    )
    parser.add_argument(
        "--triggering-event-summary",
        default=DEFAULT_INPUT.triggering_event_summary,
    )
    parser.add_argument(
        "--no-pdf",
        action="store_true",
        help="Skip PDF render + Drive PDF upload. Useful for prompt iteration.",
    )
    parser.add_argument(
        "--no-record",
        action="store_true",
        help="Skip the Drive audit-md companion write.",
    )
    args = parser.parse_args()

    input_ = LetterInput(
        contact_id=args.contact_id,
        contact_first_name=args.first_name,
        contact_last_name=args.last_name,
        contact_title=args.title,
        contact_organization=args.organization,
        contact_municipality=args.municipality,
        triggering_event=args.triggering_event,
        triggering_event_date=args.triggering_event_date,
        triggering_event_summary=args.triggering_event_summary,
        sender_name=args.sender_name,
        sender_title=args.sender_title,
        sender_email=args.sender_email,
    )

    logger.info("starting letter smoke run")
    run_result = run_letter_for_lead(
        input_,
        skip_pdf=args.no_pdf,
        skip_drive=args.no_record,
    )

    print()
    print("=" * 72)
    print(f"status:           {run_result.status}")
    print(f"run_id:           {run_result.run_id}")
    if run_result.result:
        r = run_result.result
        print(f"model:            {r.model}")
        print(f"input_tokens:     {r.input_tokens}")
        print(f"output_tokens:    {r.output_tokens}")
        print(f"cache_creation:   {r.cache_creation_tokens}")
        print(f"cache_read:       {r.cache_read_tokens}")
        print(f"context_chunks:   {r.context_chunk_count}")
        print()
        print(f"subject_line:     {r.subject_line}")
        print(f"recipient:        {r.recipient_name}, {r.recipient_title}")
        print(f"organization:     {r.recipient_organization}")
        print()
        print("opening:")
        print("-" * 72)
        print(r.opening_paragraph)
        print("-" * 72)
        print()
        print(f"observation paragraphs: {len(r.observation_paragraphs)}")
        for i, p in enumerate(r.observation_paragraphs, 1):
            print(f"  [{i}] {p[:140]}{'...' if len(p) > 140 else ''}")
        print()
        print(f"ideas paragraphs: {len(r.ideas_paragraphs)}")
        for i, p in enumerate(r.ideas_paragraphs, 1):
            print(f"  [{i}] {p[:140]}{'...' if len(p) > 140 else ''}")
        print()
        print("offer:")
        print(r.offer_paragraph)
        print()
        print("closing:")
        print(r.closing_paragraph)
        print()
        print("tone_notes:")
        print(r.tone_notes)
        print()
    if run_result.pdf_drive_web_link:
        print(f"pdf:              {run_result.pdf_drive_web_link}")
    elif run_result.pdf_error:
        print(f"pdf error:        {run_result.pdf_error}")
    if run_result.audit_drive_web_link:
        print(f"audit md:         {run_result.audit_drive_web_link}")
    elif run_result.audit_error:
        print(f"audit error:      {run_result.audit_error}")
    if run_result.error:
        print(f"error:            {run_result.error}")
    print("=" * 72)

    if run_result.status == "failed":
        sys.exit(1)
    if run_result.status == "partial":
        sys.exit(2)


if __name__ == "__main__":
    main()
