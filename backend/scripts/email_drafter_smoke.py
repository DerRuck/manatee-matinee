"""
Email Drafter end-to-end smoke.

Runs the orchestrated pipeline (agent -> Gmail draft -> Drive record ->
agent_runs) on one lead. Two modes:

  Simmer (default): a fresh post-event follow-up.
  Reply (--reply):  an in-thread reply, resolved by --thread-id or by
                    searching the from_user's mailbox for the contact.

Usage from backend/:
    python -m scripts.email_drafter_smoke                  # Simmer, defaults

    # Multiple recipients (first To is who it's personalized to)
    python -m scripts.email_drafter_smoke \
        --email nick@rookerybay.gov --cc "boss@rookerybay.gov, grants@rookerybay.gov"

    # Skip the live Gmail signature append
    python -m scripts.email_drafter_smoke --no-signature

    # Reply to a specific thread
    python -m scripts.email_drafter_smoke --reply --thread-id 1899abc... \
        --triggering-event "Answer their question about the bathymetry timeline."

    # Reply, letting the agent find the most recent thread with the contact
    python -m scripts.email_drafter_smoke --reply --reply-to-contact nick@rookerybay.gov \
        --triggering-event "Confirm we can join the March workshop."

    # Prompt-iteration mode -- skips Gmail and Drive writes
    python -m scripts.email_drafter_smoke --no-draft --no-record

Prereqs:
  - .env: GCP_PROJECT_ID, ANTHROPIC_API_KEY, DRIVE_OUTPUT_ROOT_FOLDER_ID,
    GMAIL_SIMMER_DEFAULT_USER (optional override).
  - Local IAM: roles/iam.serviceAccountTokenCreator on chawq-api-runtime
    for whichever user `gcloud auth list` reports as active.
  - Workspace domain-wide delegation for chawq-api-runtime:
      gmail.compose         (draft creation, 5/8)
      gmail.settings.basic  (signature read, 5/29)
      gmail.modify          (thread read for --reply, 5/29)
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from agents.email_drafter import EmailDrafterInput  # noqa: E402
from services.email_drafter_runner import (  # noqa: E402
    run_email_drafter_for_lead,
    run_email_reply_for_lead,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("email_drafter_smoke")


DEFAULT_INPUT = EmailDrafterInput(
    contact_id="rookery_bay_smoke_contact",
    contact_first_name="Jane",
    contact_last_name="Doe",
    contact_title="Stewardship Coordinator",
    contact_organization="Rookery Bay NERR",
    contact_municipality="rookery_bay_fl",
    contact_email="tyler@chawq.org",  # safe default -- drafts to ourselves
    triggering_event="Florida Stormwater Conference 2026",
    triggering_event_date="2026-05-05",
    triggering_event_summary=(
        "Discussed canal sediment loading on the south estuary and the "
        "regional partner network for habitat monitoring."
    ),
    from_user=None,
)


def _flatten_recipients(values):
    """Repeated and/or comma-separated --to/--cc flags -> flat list or None."""
    if not values:
        return None
    out = []
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
    parser.add_argument("--to", action="append", default=None,
                        help="Primary recipient(s). Repeatable/comma-separated. First = personalized.")
    parser.add_argument("--cc", action="append", default=None,
                        help="Cc recipient(s). Repeatable/comma-separated.")
    parser.add_argument("--triggering-event", default=DEFAULT_INPUT.triggering_event,
                        help="Simmer: the event. Reply: what to convey.")
    parser.add_argument("--triggering-event-date", default=DEFAULT_INPUT.triggering_event_date)
    parser.add_argument("--triggering-event-summary", default=DEFAULT_INPUT.triggering_event_summary)
    parser.add_argument("--from-user", default=DEFAULT_INPUT.from_user,
                        help="Gmail mailbox to impersonate (defaults to GMAIL_SIMMER_DEFAULT_USER).")
    parser.add_argument("--no-signature", action="store_true",
                        help="Don't fetch/append the from_user's live Gmail signature.")
    parser.add_argument("--no-draft", action="store_true", help="Skip Gmail draft creation.")
    parser.add_argument("--no-record", action="store_true", help="Skip the Drive record write.")
    # Reply mode
    parser.add_argument("--reply", action="store_true",
                        help="Draft an in-thread reply instead of a Simmer.")
    parser.add_argument("--thread-id", default=None,
                        help="Reply: explicit Gmail thread id (wins over --reply-to-contact).")
    parser.add_argument("--reply-to-contact", default=None,
                        help="Reply: search this contact's email for the most recent thread.")
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

    if args.reply:
        logger.info("starting email_drafter REPLY smoke run")
        result = run_email_reply_for_lead(
            input_,
            thread_id=args.thread_id,
            contact_email_for_search=args.reply_to_contact,
            skip_gmail=args.no_draft,
            skip_drive=args.no_record,
        )
    else:
        logger.info("starting email_drafter smoke run")
        result = run_email_drafter_for_lead(
            input_,
            skip_gmail=args.no_draft,
            skip_drive=args.no_record,
        )

    print()
    print("=" * 72)
    print(f"agent:            {result.agent_name}")
    print(f"status:           {result.status}")
    print(f"run_id:           {result.run_id}")
    if result.thread_id:
        print(f"thread_id:        {result.thread_id}")
        print(f"thread_subject:   {result.thread_subject}")
    if result.thread_candidates:
        print(f"thread_candidates ({len(result.thread_candidates)}):")
        for c in result.thread_candidates:
            print(f"   - {c.get('thread_id')}  {c.get('date')}  {c.get('subject')}")
    print(f"to:               {', '.join(result.to_recipients or []) or '(none)'}")
    print(f"cc:               {', '.join(result.cc_recipients or []) or '(none)'}")
    print(f"signature_added:  {result.signature_appended}")
    if result.draft:
        print(f"model:            {result.draft.model}")
        print(f"input_tokens:     {result.draft.input_tokens}")
        print(f"output_tokens:    {result.draft.output_tokens}")
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

    if result.status == "failed":
        sys.exit(1)
    if result.status == "partial":
        sys.exit(2)


if __name__ == "__main__":
    main()
