import os
import re
import datetime
import requests
import google.auth
from playwright.sync_api import sync_playwright
from googleapiclient.discovery import build
import base64
# from google.oauth2 import service_account
# from google.auth.transport.requests import Request
from google.auth import impersonated_credentials

# --- Configuration ---

TARGET_INBOX_EMAIL = os.getenv("TARGET_INBOX_EMAIL")
SERVICE_ACCOUNT_EMAIL = os.getenv("SERVICE_ACCOUNT_EMAIL")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")
SENDER_EMAIL = os.getenv("SENDER_EMAIL")

PROCESSING_LABEL = "PROCESSING_IFLYTEK"
TARGET_LABEL = "PROCESSED_IFLYTEK"

SCOPES = [
    'https://www.googleapis.com/auth/drive.file', 
    'https://www.googleapis.com/auth/gmail.modify'
]

# --- Configuration END ---

def get_services():

    base_creds, _ = google.auth.default()

    TARGET_USER = os.getenv("TARGET_INBOX_EMAIL")
    SERVICE_ACCOUNT_EMAIL = os.getenv("SERVICE_ACCOUNT_EMAIL")

    delegated_creds = impersonated_credentials.Credentials(
        source_credentials=base_creds,
        target_principal=SERVICE_ACCOUNT_EMAIL,
        target_scopes=SCOPES,
        subject=TARGET_USER,
        lifetime=3600
    )

    gmail = build('gmail', 'v1', credentials=delegated_creds)
    drive = build('drive', 'v3', credentials=delegated_creds)

    return gmail, drive


def extract_body(msg_data):
    payload = msg_data.get('payload', {})
    parts = payload.get('parts', [])

    for part in parts:
        if part['mimeType'] == 'text/plain':
            data = part['body'].get('data')
            if data:
                return base64.urlsafe_b64decode(data).decode()

    return msg_data.get('snippet', '')

def process_inbox(event, context):

    gmail, drive = get_services()

    # 1. Find un-labeled emails from the specific sender
    query = f"from:{SENDER_EMAIL} -label:{TARGET_LABEL}"
    results = gmail.users().messages().list(userId='me', q=query).execute()
    messages = results.get('messages', [])

    if not messages:
        print("No new emails to process.")
        return

    for msg in messages:
        msg_data = gmail.users().messages().get(userId='me', id=msg['id']).execute()
        # body = msg_data.get('snippet', '')
        body = extract_body(msg_data)
        
        # 2. Extract the iFlytek URL using Regex
        url_match = re.search(r'https://share-ap1\.theainote\.com/note-share/fusion\?shareId=[a-zA-Z0-9]+', body)
        
        if url_match:
            share_url = url_match.group(0)
            print(f"Found URL: {share_url}")
            
            # 3. Scrape and Upload
            success = scrape_and_upload(share_url, drive)
            
            # 4. Mark as Processed
            if success:
                label_id = get_or_create_label(gmail, TARGET_LABEL)

                gmail.users().messages().modify(
                    userId='me',
                    id=msg['id'],
                    body={'addLabelIds': [label_id]}
                ).execute()

def save_to_drive(content, name, mime, service):
    from googleapiclient.http import MediaIoBaseUpload
    import io
    file_metadata = {'name': name, 'parents': [DRIVE_FOLDER_ID]}
    media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime)
    # service.files().create(body=file_metadata, media_body=media).execute()
    service.files().create(body=file_metadata,media_body=media,supportsAllDrives=True).execute()

def get_or_create_label(service, label_name):
    # Logic to ensure the PROCESSED label exists in Gmail
    labels = service.users().labels().list(userId='me').execute().get('labels', [])
    for l in labels:
        if l['name'] == label_name: return l['id']
    new_label = service.users().labels().create(userId='me', body={'name': label_name}).execute()
    return new_label['id']

def scrape_and_upload(url, drive_service):
    # Naming Scheme: YYYY-MM-DD_Client-Project_MeetingType_Topic.ext
    date_prefix = datetime.datetime.now().strftime("%Y-%m-%d")
    
    try:
        with sync_playwright() as p:
            # browser = p.chromium.launch(headless=True)
            browser = p.chromium.launch(headless=True,args=["--no-sandbox", "--disable-dev-shm-usage"])
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.goto(url)

            # 1. Wait for the title element to appear
            page.wait_for_selector(".note-title")
            raw_title = page.locator(".note-title").inner_text()

            # 2. Clean the title (remove characters that break file systems)
            clean_title = re.sub(r'[^\w\s-]', '_', raw_title).strip()
            base_name = f"{date_prefix}_{clean_title}"

            # --- Audio ---
            audio_src = page.get_attribute("audio", "src") or page.get_attribute("video", "src")
            if audio_src:
                r = requests.get(audio_src)
                save_to_drive(r.content, f"{base_name}.opus", "audio/ogg", drive_service)

            # --- PDF & TXT ---
            page.wait_for_selector(".share-btn")
            page.click(".share-btn")
            page.wait_for_selector(".export-options")

            # PDF
            with page.expect_download() as dl_pdf:
                page.locator(".export-option", has_text="Export PDF").click()
            download = dl_pdf.value
            pdf_path = download.path()
            with open(pdf_path, "rb") as f:
                save_to_drive(f.read(), f"{base_name}.pdf", "application/pdf", drive_service)

            # TXT
            if not page.is_visible(".export-options"): page.click(".share-btn")
            with page.expect_download() as dl_txt:
                page.locator(".export-option", has_text="Export TXT").click()
            download = dl_txt.value
            txt_path = download.path()
            with open(txt_path, "rb") as f:
                save_to_drive(f.read(), f"{base_name}.txt", "text/plain", drive_service)

            browser.close()
        return True
    except Exception as e:
        print(f"Scraping error: {e}")
        return False


if __name__ == "__main__":
    process_inbox(None, None)