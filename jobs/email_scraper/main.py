import os
import re
import base64
import hashlib
import html
import io
from email.utils import parseaddr, parsedate_to_datetime

import google.auth
from google.auth import impersonated_credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

SERVICE_ACCOUNT_EMAIL = os.getenv("SERVICE_ACCOUNT_EMAIL")
SHARED_DRIVE_FOLDER_ID = os.getenv("SHARED_DRIVE_FOLDER_ID")

TARGET_INBOXES = [
    'emily@chawq.org',
    'contact@chawq.org',
    # 'tyler@chawq.org',
    'logan@chawq.org'
]

SCOPES = [
    'https://www.googleapis.com/auth/drive.file',
    'https://www.googleapis.com/auth/gmail.modify',
]

LOOKBACK_DAYS = os.getenv("LOOKBACK_DAYS", "1").strip()
LIST_PAGE_SIZE = 500


def build_gmail_query(lookback_days):
    parts = []
    if lookback_days and lookback_days.lower() != "all":
        parts.append(f"newer_than:{lookback_days}d")
    parts.extend([
        "-in:draft",
        "-category:promotions",
        "-category:social",
        "-category:forums",
        # Generic noreply / notifications senders.
        "-from:noreply",
        "-from:no-reply",
        "-from:donotreply",
        "-from:notifications",
        "-from:calendar-notification@google.com",
        # Transactional / billing / vendor noise domains. Confirmed against
        # 5/20 sample of 207 messages -- pure boilerplate, zero retrieval value.
        "-from:gusto.com",
        "-from:doodle.com",
        "-from:hartford.com",
        "-from:benefitfocus.com",
        "-from:aliaswire.com",
        "-from:comcast.net",
        "-from:surveymonkeyuser.com",
        "-from:memberclicks-mail.net",
        "-from:regpack.com",
        # Specific noisy local-parts for domains that ALSO host real correspondence.
        "-from:billing@",
        "-from:concierge@",
        "-from:online.communications@",
        "-from:premium-support@",
        "-from:businesscenter@",
        "-from:informationservices@",
        "-from:chawq-expenses@",
        # Auto-reply subjects.
        '-subject:"out of office"',
        '-subject:"automatic reply"',
        '-subject:"automatic response"',
    ])
    return " ".join(parts)


GMAIL_QUERY = build_gmail_query(LOOKBACK_DAYS)
FILENAME_SLUG_RE = re.compile(r'_([a-f0-9]{12})_')
CHAWQ_DOMAIN = '@chawq.org'
COUNTERPARTY_UNSAFE_RE = re.compile(r'[^a-z0-9._-]')
ADDRESS_SPLIT_RE = re.compile(r'[,;]')

# Folder hierarchy under SHARED_DRIVE_FOLDER_ID:
#   <root>/<EMAIL_TYPE_SUBFOLDER>/<YYYY-MM>/<filename>
# The typed subfolder signals document_type=email to the watcher's ingest
# resolver (per 2026-05-11 path convention). YYYY-MM bucket caps each
# folder's size and lets humans browse by month.
EMAIL_TYPE_SUBFOLDER = "email-inbox"
UNKNOWN_DATE_BUCKET = "unknown"

# (parent_folder_id, child_name) -> child_folder_id.
# Avoids repeated Drive lookups during a single run.
_folder_id_cache = {}

KEEP_ATTACHMENT_MIMETYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "text/plain",
    "text/csv",
    "text/markdown",
}

MIN_BODY_CHARS = 50

QUOTED_HEADER_RE = re.compile(r"^On .{1,200}\bwrote:\s*$", re.MULTILINE)
ORIGINAL_MESSAGE_RE = re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.MULTILINE | re.IGNORECASE)
FORWARDED_MESSAGE_RE = re.compile(r"^-{2,}\s*Forwarded message\s*-{2,}\s*$", re.MULTILINE | re.IGNORECASE)
SIGNATURE_RE = re.compile(r"^-- \s*$", re.MULTILINE)
URL_RE = re.compile(r'(https?://[^\s"\'<>]+)')

# Subject patterns that mark a message as calendar machinery vs real content.
# Internal calendar invites are pure boilerplate; external invites carry meeting
# context (counterparty, topic, date) so we keep those.
CALENDAR_NOISE_SUBJECT_RE = re.compile(
    r'^(Invitation|Updated invitation|Updated response|Accepted|Declined|'
    r'Tentative|Canceled event|Appointment booked|New Time Proposed)\b:',
    re.IGNORECASE,
)
# Subjects starting with "Share of " are always Drive/note-share notifications --
# the linked doc lives elsewhere, this email adds no content.
SHARE_NOTIFICATION_PREFIX = "Share of "

METADATA_HEADERS = [
    'From', 'To', 'Cc', 'Subject', 'Date',
    'Message-ID', 'In-Reply-To', 'References',
    'Auto-Submitted', 'Precedence',
    'List-Unsubscribe', 'List-ID',
]


def get_services(target_user):
    base_creds, _ = google.auth.default()
    delegated_creds = impersonated_credentials.Credentials(
        source_credentials=base_creds,
        target_principal=SERVICE_ACCOUNT_EMAIL,
        target_scopes=SCOPES,
        subject=target_user,
        lifetime=3600,
    )
    gmail = build('gmail', 'v1', credentials=delegated_creds)
    drive = build('drive', 'v3', credentials=delegated_creds)
    return gmail, drive


def get_header(headers, name):
    name_lower = name.lower()
    return next(
        (h['value'] for h in headers if h.get('name', '').lower() == name_lower),
        '',
    )


def should_skip_by_headers(headers):
    auto_submitted = get_header(headers, 'Auto-Submitted').strip().lower()
    if auto_submitted and auto_submitted != 'no':
        return True, f"Auto-Submitted={auto_submitted}"
    precedence = get_header(headers, 'Precedence').strip().lower()
    if precedence in {'bulk', 'list', 'junk'}:
        return True, f"Precedence={precedence}"
    if get_header(headers, 'List-Unsubscribe'):
        return True, "List-Unsubscribe"
    if get_header(headers, 'List-ID'):
        return True, "List-ID"
    return False, ""


def strip_quoted_and_signature(text):
    m = QUOTED_HEADER_RE.search(text)
    if m:
        text = text[:m.start()]
    m = ORIGINAL_MESSAGE_RE.search(text)
    if m:
        text = text[:m.start()]
    m = FORWARDED_MESSAGE_RE.search(text)
    if m:
        text = text[:m.start()]
    lines = text.splitlines()
    while lines and lines[-1].lstrip().startswith('>'):
        lines.pop()
    text = "\n".join(lines)
    m = SIGNATURE_RE.search(text)
    if m:
        text = text[:m.start()]
    return text.strip()


def extract_body_and_attachments(msg_data):
    payload = msg_data.get('payload', {})
    body_parts = {'text': ''}
    attachments = []

    def parse_parts(part):
        mime_type = part.get('mimeType')
        filename = part.get('filename')
        if filename and part.get('body', {}).get('attachmentId'):
            attachments.append({
                'filename': filename,
                'attachmentId': part['body']['attachmentId'],
                'mimeType': mime_type,
            })
        if mime_type == 'text/plain':
            data = part.get('body', {}).get('data')
            if data:
                body_parts['text'] += base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
        if 'parts' in part:
            for subpart in part['parts']:
                parse_parts(subpart)

    parse_parts(payload)
    final_text = body_parts['text'] if body_parts['text'] else msg_data.get('snippet', '')
    return html.unescape(final_text), attachments


def save_to_drive(content_bytes, name, mime_type, drive_service, folder_id):
    """Upload a file into the given Drive folder_id."""
    file_metadata = {'name': name, 'parents': [folder_id]}
    media = MediaIoBaseUpload(io.BytesIO(content_bytes), mimetype=mime_type)
    drive_service.files().create(
        body=file_metadata,
        media_body=media,
        supportsAllDrives=True,
    ).execute()
    print(f"Uploaded to Drive: {name}")


def cached_subfolder(drive_service, parent_id, name):
    """Find or create a Drive subfolder by name under parent_id. Returns the ID."""
    key = (parent_id, name)
    if key in _folder_id_cache:
        return _folder_id_cache[key]

    safe_name = name.replace("'", r"\'")
    resp = drive_service.files().list(
        q=(
            f"'{parent_id}' in parents "
            f"and name = '{safe_name}' "
            f"and mimeType = 'application/vnd.google-apps.folder' "
            f"and trashed = false"
        ),
        fields="files(id)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = resp.get('files', [])
    if files:
        folder_id = files[0]['id']
    else:
        created = drive_service.files().create(
            body={
                'name': name,
                'mimeType': 'application/vnd.google-apps.folder',
                'parents': [parent_id],
            },
            fields='id',
            supportsAllDrives=True,
        ).execute()
        folder_id = created['id']
        print(f"Created Drive subfolder: {name}")

    _folder_id_cache[key] = folder_id
    return folder_id


def parse_message_date_yyyymm(date_str):
    """Parse an RFC 2822 Date header into a YYYY-MM bucket. Falls back to 'unknown'."""
    if not date_str or date_str == 'Unknown Date':
        return UNKNOWN_DATE_BUCKET
    try:
        dt = parsedate_to_datetime(date_str)
        if dt is None:
            return UNKNOWN_DATE_BUCKET
        return dt.strftime('%Y-%m')
    except (TypeError, ValueError, IndexError):
        return UNKNOWN_DATE_BUCKET


def resolve_email_folder(drive_service, date_str):
    """Return the Drive folder ID for <root>/email-inbox/<YYYY-MM>/, creating as needed."""
    type_folder = cached_subfolder(drive_service, SHARED_DRIVE_FOLDER_ID, EMAIL_TYPE_SUBFOLDER)
    month_bucket = parse_message_date_yyyymm(date_str)
    return cached_subfolder(drive_service, type_folder, month_bucket)


def parse_email_address(header_value):
    if not header_value:
        return ''
    return parseaddr(header_value)[1].lower()


def split_address_list(header_value):
    if not header_value:
        return []
    out = []
    for chunk in ADDRESS_SPLIT_RE.split(header_value):
        addr = parse_email_address(chunk)
        if addr:
            out.append(addr)
    return out


def determine_direction(from_addr, to_addr, cc_addr):
    sender = parse_email_address(from_addr)
    sender_is_chawq = CHAWQ_DOMAIN in sender
    if not sender_is_chawq:
        return 'inbound'
    recipients = split_address_list(to_addr) + split_address_list(cc_addr)
    has_external = any(CHAWQ_DOMAIN not in r for r in recipients)
    return 'outbound' if has_external else 'internal'


def determine_counterparty(from_addr, to_addr, cc_addr, direction):
    if direction == 'inbound':
        return parse_email_address(from_addr)
    if direction == 'outbound':
        for header_val in (to_addr, cc_addr):
            for addr in split_address_list(header_val):
                if CHAWQ_DOMAIN not in addr:
                    return addr
    return ''


def counterparty_for_filename(counterparty):
    if not counterparty:
        return 'internal'
    safe = counterparty.replace('@', '_at_')
    safe = COUNTERPARTY_UNSAFE_RE.sub('-', safe)
    return safe[:60] or 'internal'


def fetch_user_label_names(gmail_service):
    try:
        resp = gmail_service.users().labels().list(userId='me').execute()
        return {
            l['id']: l['name']
            for l in resp.get('labels', [])
            if l.get('type') == 'user'
        }
    except Exception as e:
        print(f"  WARN: could not fetch label names ({e}); proceeding without label resolution.")
        return {}


def message_id_slug(message_id):
    if not message_id:
        return "no-id"
    return hashlib.sha1(message_id.encode('utf-8')).hexdigest()[:12]


def list_existing_slugs(drive_service):
    """Walk SHARED_DRIVE_FOLDER_ID and every subfolder; collect Message-ID slugs."""
    slugs = set()
    to_scan = [SHARED_DRIVE_FOLDER_ID]
    while to_scan:
        parent = to_scan.pop()
        page_token = None
        while True:
            resp = drive_service.files().list(
                q=f"'{parent}' in parents and trashed=false",
                fields="nextPageToken, files(name, id, mimeType)",
                pageSize=1000,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            for f in resp.get('files', []):
                if f.get('mimeType') == 'application/vnd.google-apps.folder':
                    to_scan.append(f['id'])
                else:
                    m = FILENAME_SLUG_RE.search(f.get('name', ''))
                    if m:
                        slugs.add(m.group(1))
            page_token = resp.get('nextPageToken')
            if not page_token:
                break
    return slugs


def list_all_message_ids(gmail_service, query):
    all_msgs = []
    page_token = None
    while True:
        resp = gmail_service.users().messages().list(
            userId='me',
            q=query,
            maxResults=LIST_PAGE_SIZE,
            pageToken=page_token,
        ).execute()
        all_msgs.extend(resp.get('messages', []))
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    return all_msgs


def process_inboxes(event, context):
    print("Starting organization inbox scrape...")
    print(f"Lookback: {LOOKBACK_DAYS} day(s)")
    print(f"Gmail query: {GMAIL_QUERY}")

    try:
        _bootstrap_gmail, bootstrap_drive = get_services(TARGET_INBOXES[0])
        seen_message_ids = list_existing_slugs(bootstrap_drive)
        print(f"Pre-seeded {len(seen_message_ids)} existing Message-ID slug(s) from Drive folder.")
    except Exception as e:
        print(f"WARN: could not pre-seed slug set from Drive ({e}); proceeding without it.")
        seen_message_ids = set()

    stats = {
        'listed': 0,
        'skipped_headers': 0,
        'skipped_dedup': 0,
        'skipped_internal_calendar': 0,
        'skipped_share_notification': 0,
        'skipped_short_body': 0,
        'skipped_attachment_type': 0,
        'fetched_full': 0,
        'written': 0,
    }

    for user_email in TARGET_INBOXES:
        print(f"\n--- Processing Inbox: {user_email} ---")
        try:
            gmail, drive = get_services(user_email)
            label_names = fetch_user_label_names(gmail)
            print(f"Loaded {len(label_names)} user label(s) for {user_email}.")

            messages = list_all_message_ids(gmail, GMAIL_QUERY)
            stats['listed'] += len(messages)
            print(f"Listed {len(messages)} message(s) matching query.")

            if not messages:
                print(f"No new emails found for {user_email}.")
                continue

            for msg in messages:
                meta = gmail.users().messages().get(
                    userId='me',
                    id=msg['id'],
                    format='metadata',
                    metadataHeaders=METADATA_HEADERS,
                ).execute()
                headers = meta['payload'].get('headers', [])
                thread_id = meta.get('threadId', '')
                label_ids = meta.get('labelIds', [])

                subject_preview = (get_header(headers, 'Subject') or "(no subject)")[:60]

                skip, reason = should_skip_by_headers(headers)
                if skip:
                    stats['skipped_headers'] += 1
                    print(f"  Skip [{reason}]: {subject_preview}")
                    continue

                message_id = get_header(headers, 'Message-ID')
                slug = message_id_slug(message_id) if message_id else None
                if slug and slug in seen_message_ids:
                    stats['skipped_dedup'] += 1
                    print(f"  Skip [dup]: {subject_preview}")
                    continue
                if slug:
                    seen_message_ids.add(slug)

                # Subject + sender already available from the metadata fetch.
                # Compute the bits the noise filters need so we can short-circuit
                # before the more expensive full body fetch.
                subject_meta = get_header(headers, 'Subject') or "No Subject"

                if subject_meta.startswith(SHARE_NOTIFICATION_PREFIX):
                    stats['skipped_share_notification'] += 1
                    print(f"  Skip [share notification]: {subject_preview}")
                    continue

                from_meta = get_header(headers, 'From')
                to_meta = get_header(headers, 'To')
                cc_meta = get_header(headers, 'Cc')
                direction_meta = determine_direction(from_meta, to_meta, cc_meta)

                if direction_meta == 'internal' and CALENDAR_NOISE_SUBJECT_RE.match(subject_meta):
                    stats['skipped_internal_calendar'] += 1
                    print(f"  Skip [internal calendar]: {subject_preview}")
                    continue

                msg_data = gmail.users().messages().get(
                    userId='me',
                    id=msg['id'],
                    format='full',
                ).execute()
                stats['fetched_full'] += 1

                subject = get_header(headers, 'Subject') or "No Subject"
                date_str = get_header(headers, 'Date') or "Unknown Date"
                from_addr = get_header(headers, 'From')
                to_addr = get_header(headers, 'To')
                cc_addr = get_header(headers, 'Cc')
                in_reply_to = get_header(headers, 'In-Reply-To')
                references = get_header(headers, 'References')

                direction = determine_direction(from_addr, to_addr, cc_addr)
                counterparty = determine_counterparty(from_addr, to_addr, cc_addr, direction)

                resolved_labels = [label_names[lid] for lid in label_ids if lid in label_names]
                labels_str = ", ".join(sorted(resolved_labels)) if resolved_labels else "(none)"

                body_text_raw, attachments = extract_body_and_attachments(msg_data)
                body_text = strip_quoted_and_signature(body_text_raw)

                if len(body_text) < MIN_BODY_CHARS:
                    stats['skipped_short_body'] += 1
                    print(f"  Skip [short body, {len(body_text)}c]: {subject_preview}")
                    continue

                all_links = URL_RE.findall(body_text)

                safe_subject = re.sub(r'[^\w\s-]', '_', subject)[:50]
                if not slug:
                    slug = message_id_slug(message_id)
                cp_token = counterparty_for_filename(counterparty)
                base_filename = f"{cp_token}_{slug}_{safe_subject}"

                summary = (
                    f"Date: {date_str}\n"
                    f"From: {from_addr}\n"
                    f"To: {to_addr}\n"
                    f"Cc: {cc_addr}\n"
                    f"Subject: {subject}\n"
                    f"Message-ID: {message_id}\n"
                    f"Thread-ID: {thread_id}\n"
                    f"In-Reply-To: {in_reply_to}\n"
                    f"References: {references}\n"
                    f"Direction: {direction}\n"
                    f"Counterparty: {counterparty}\n"
                    f"Scraped-Inbox: {user_email}\n"
                    f"Labels: {labels_str}\n\n"
                    f"--- EXTRACTED LINKS ---\n"
                    f"{chr(10).join(all_links) if all_links else 'No links found.'}\n\n"
                    f"--- EMAIL BODY ---\n{body_text}\n"
                )

                dest_folder = resolve_email_folder(drive, date_str)

                save_to_drive(
                    summary.encode('utf-8'),
                    f"{base_filename}_summary.txt",
                    "text/plain",
                    drive,
                    dest_folder,
                )
                stats['written'] += 1

                for att in attachments:
                    if att['mimeType'] not in KEEP_ATTACHMENT_MIMETYPES:
                        stats['skipped_attachment_type'] += 1
                        print(f"  Skip attachment [type {att['mimeType']}]: {att['filename']}")
                        continue

                    att_data = gmail.users().messages().attachments().get(
                        userId='me',
                        messageId=msg['id'],
                        id=att['attachmentId'],
                    ).execute()

                    file_bytes = base64.urlsafe_b64decode(att_data['data'])
                    save_to_drive(
                        file_bytes,
                        f"{base_filename}_{att['filename']}",
                        att['mimeType'],
                        drive,
                        dest_folder,
                    )

        except Exception as e:
            print(f"Error processing {user_email}: {e}")

    print("\n--- Run summary ---")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    process_inboxes(None, None)
