"""
Email Drafter end-to-end smoke.

Runs the orchestrated pipeline (agent -> Gmail draft -> Drive record ->
agent_runs) on one lead. Defaults to a Rookery Bay lead so you can run
it without typing all the args; override anything via flags.

Usage from backend/:
    python -m scripts.email_drafter_smoke

    # Custom lead
    python -m scripts.email_drafter_smoke \
        --first-name Jane --last-name Doe \
        --organization "SFWMD" --municipality sfwmd_fl \
        --email jane.doe@example.gov \
        --triggering-event "Florida Stormwater Conference 2026" \
        --triggering-event-summary "Discussed regional canal sediment loading."

    # Multiple recipients (the FIRST To is who the email is personalized to).
    # --to / --cc accept repeats and/or comma-separated lists. --email still
    # seeds the primary To when --to is omitted.
    python -m scripts.email_drafter_smoke \
        --email nick@rookerybay.gov \
        --cc "boss@rookerybay.gov, grants@rookerybay.gov"

    python -m scripts.email_drafter_smoke \
        --to nick@rookerybay.gov --to jane@rookerybay.gov \
        --cc director@rookerybay.gov

    # Skip the live Gmail signature append (drafts the prompt body as-is)
    python -m scripts.email_drafter_smoke --no-signature

    # Prompt-iteration mode -- skips Gmail and Drive writes
    python -m scripts.email_drafter_smoke --no-draft --no-record

Output: prints the JSON model output, the resolved To/Cc recipients,
whether a live signature was appended, the Gmail draft web_link (if
created), and the Drive web link (if written). Inspects the result's
status field -- `completed` means everything landed; `partial` means the
draft was generated but at least one side-effect failed; `failed` means
the model call itself errored.

Prereqs:
  - .env: GCP_PROJECT_ID, ANTHROPIC_API_KEY, DRIVE_OUTPUT_ROOT_FOLDER_ID,
    GMAIL_SIMMER_DEFAULT_USER (optional override).
  - Local IAM: roles/iam.serviceAccountTokenCreator on chawq-api-runtime
    for whichever user `gcloud auth list` reports as active.
  - Workspace domain-wide delegation for chawq-api-runtime:
      https://www.googleapis.com/auth/gmail.compose        (draft creation, 5/8)
      https://www.googleapis.com/auth/gmail.settings.basic  (signature read, 5/29)
    Without gmail.settings.basic the signature fetch fails gracefully and
    the draft is created without a signature.
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

from agents.email_drafter import EmailDrafterInput  # noqa: E402
from services.email_drafter_runner import run_email_drafter_for_lead  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("email_drafter_smoke")


# Hardcoded Rookery Bay defaults -- easy to invoke without flags.
DEFAULT_INPUT = EmailDrafterInput(
    contact_id="rookery_bay_smoke_contact",
    contact_first_name="Jane",
    contact_last_name="Doe",
    contact_title="Stewardship Coordinator",
    contact_organization="Rookery Bay NERR",
    contact_municipality="rookery_bay_fl",
    contact_email="tyler@chawq.org",  # safe default -- sends draft to ourselves
    triggering_event="Florida Stormwater Conference 2026",
    triggering_event_date="2026-05-05",
    triggering_event_summary=(
        "Discussed canal sediment loading on the south estuary and the "
        "regional partner network for habitat monitoring."
    ),
    from_user=None,  # falls back to GMAIL_SIMMER_DEFAULT_USER
)


def _flatten_recipients(values: list[str] | None) -> list[str] | None:
    """
    Turn repeated and/or comma-separated --to/--cc flags into a flat list.
    Returns None when nothing was passed so the dataclass default applies.
    """
    if not values:
        return None
    out: list[str] = []
    for chunk in values:
        out.extend(part.strip() for part in chunk.split(",") if part.strip())
    return out or None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--contact-id", default=DEFAULT_INPUT.contact_id)
    parser.add_argument("--first-name", default=DEFAULT_INPUT.contact_first_name)
    parser.add_argument("--last-name", default=DEFAULT_INPUT.contact_last_name)
    parser.add_argument("--title", default=DEFAULT_INPUT.contact_title)
    parser.add_argument("--organization", default=DEFAULT_INPUT.contact_organization)
    parser.add_argument("--municipality", default=DEFAULT_INPUT.contact_municipality)
    parser.add_argument("--email", default=DEFAULT_INPUT.contact_email)
    parser.add_argument(
        "--to",
        action="append",
        default=None,
        help="Primary recipient(s). Repeatable and/or comma-separated. "
        "The first address is who the email is personalized to. When "
        "omitted, --email is the sole To.",
    )
    parser.add_argument(
        "--cc",
        action="append",
        default=None,
        help="Cc recipient(s). Repeatable and/or comma-separated. "
        "Addresses already on the To line are dropped automatically.",
    )
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
        "--from-user",
        default=DEFAULT_INPUT.from_user,
        help="Gmail mailbox to impersonate (defaults to GMAIL_SIMMER_DEFAULT_USER).",
    )
    parser.add_argument(
        "--no-signature",
        action="store_true",
        help="Don't fetch/append the from_user's live Gmail signature.",
    )
    parser.add_argument(
        "--no-draft",
        action="store_true",
        help="Skip Gmail draft creation. Useful for prompt iteration.",
    )
    parser.add_argument(
        "--no-record",
        action="store_true",
        help="Skip the Drive record write.",
    )
    args = parser.parse_args()

    input_ = EmailDrafterInput(
        contact_id=args.contact_id,
        contact_first_name=args.first_name,
        contact_last_name=args.last_name,
        contact_title=args.title,
        contact_organization=args.organization,
        contact_municipality=args.municipality,
        contact_email=args.email,
        to_recipients=_flatten_recipients(args.to),
        cc_recipients=_flatten_recipients(args.cc),
        append_signature=not args.no_signature,
        triggering_event=args.triggering_event,
        triggering_event_date=args.triggering_event_date,
        triggering_event_summary=args.triggering_event_summary,
        from_user=args.from_user,
    )

    logger.info("starting email_drafter smoke run")
    result = run_email_drafter_for_lead(
        input_,
        skip_gmail=args.no_draft,
        skip_drive=args.no_record,
    )

    print()
    print("=" * 72)
    print(f"status:           {result.status}")
    print(f"run_id:           {result.run_id}")
    print(f"to:               {', '.join(result.to_recipients or []) or '(none)'}")
    print(f"cc:               {', '.join(result.cc_recipients or []) or '(none)'}")
    print(f"signature_added:  {result.signature_appended}")
    if result.draft:
        print(f"model:            {result.draft.model}")
        print(f"input_tokens:     {result.draft.input_tokens}")
        print(f"output_tokens:    {result.draft.output_tokens}")
        print(f"cache_creation:   {result.draft.cache_creation_tokens}")
        print(f"cache_read:       {result.draft.cache_read_tokens}")
        print(f"context_chunks:   {result.draft.context_chunk_count}")
        print(f"suggested_send:   {result.draft.suggested_send}")
        print()
        print(f"subject:          {result.draft.subject}")
        print()
        print("body:")
        print("-" * 72)
        print(result.draft.body)
        print("-" * 72)
        print()
        print("tone_notes:")
        print(result.draft.tone_notes)
        print()
    if result.gmail_web_link:
        print(f"gmail draft:      {result.gmail_web_link}")
    elif result.gmail_error:
        print(f"gmail error:      {result.gmail_error}")
    if result.drive_web_link:
        print(f"drive record:     {result.drive_web_link}")
    elif result.drive_error:
        print(f"drive error:      {result.drive_error}")
    if result.error:
        print(f"error:            {result.error}")
    print("=" * 72)

    # Non-zero exit if anything didn't land -- useful for shell pipelines.
    if result.status == "failed":
        sys.exit(1)
    if result.status == "partial":
        sys.exit(2)


if __name__ == "__main__":
    main()
