import os
import re
import base64
import html
import io
import google.auth
from google.auth import impersonated_credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# --- CONFIGURATION ---
SERVICE_ACCOUNT_EMAIL = os.getenv("SERVICE_ACCOUNT_EMAIL")
SHARED_DRIVE_FOLDER_ID = os.getenv("SHARED_DRIVE_FOLDER_ID")

# The specific inboxes you want to scrape daily
TARGET_INBOXES = [
    'emily@chawq.org', 
    'admin@chawq.org', 
    'tyler@chawq.org'
]

SCOPES = [
    'https://www.googleapis.com/auth/drive.file', 
    'https://www.googleapis.com/auth/gmail.modify'
]

def get_services(target_user):
    """Generates impersonated credentials for a specific user."""
    base_creds, _ = google.auth.default()
    delegated_creds = impersonated_credentials.Credentials(
        source_credentials=base_creds,
        target_principal=SERVICE_ACCOUNT_EMAIL,
        target_scopes=SCOPES,
        subject=target_user,
        lifetime=3600
    )
    gmail = build('gmail', 'v1', credentials=delegated_creds)
    drive = build('drive', 'v3', credentials=delegated_creds)
    return gmail, drive

def extract_body_and_attachments(msg_data):
    """Recursively pulls the text body and a list of attachment metadata."""
    payload = msg_data.get('payload', {})
    body_parts = {'text': ''}
    attachments = []
    
    def parse_parts(part):
        mime_type = part.get('mimeType')
        filename = part.get('filename')
        
        # 1. Grab Attachments
        if filename and part.get('body', {}).get('attachmentId'):
            attachments.append({
                'filename': filename,
                'attachmentId': part['body']['attachmentId'],
                'mimeType': mime_type
            })
            
        # 2. Grab Text (Prefer plain text)
        if mime_type == 'text/plain':
            data = part.get('body', {}).get('data')
            if data:
                body_parts['text'] += base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
        
        # 3. Dig deeper if nested
        if 'parts' in part:
            for subpart in part['parts']:
                parse_parts(subpart)
                
    parse_parts(payload)
    
    # Fallback to snippet if text extraction fails
    final_text = body_parts['text'] if body_parts['text'] else msg_data.get('snippet', '')
    return html.unescape(final_text), attachments

def save_to_drive(content_bytes, name, mime_type, drive_service):
    """Uploads a file to the Shared Drive."""
    file_metadata = {'name': name, 'parents': [SHARED_DRIVE_FOLDER_ID]}
    media = MediaIoBaseUpload(io.BytesIO(content_bytes), mimetype=mime_type)
    drive_service.files().create(
        body=file_metadata, 
        media_body=media, 
        supportsAllDrives=True
    ).execute()
    print(f"Uploaded to Drive: {name}")

def process_inboxes(event, context):
    print("Starting daily organization inbox scrape...")
    
    for user_email in TARGET_INBOXES:
        print(f"\n--- Processing Inbox: {user_email} ---")
        try:
            gmail, drive = get_services(user_email)
            
            # Get emails from the last 24 hours
            results = gmail.users().messages().list(userId='me', q="newer_than:1d").execute()
            messages = results.get('messages', [])
            
            if not messages:
                print(f"No new emails found for {user_email}.")
                continue
                
            for msg in messages:
                # Get the full email payload
                msg_data = gmail.users().messages().get(userId='me', id=msg['id']).execute()
                
                # Extract headers for metadata
                headers = msg_data['payload'].get('headers', [])
                subject = next((h['value'] for h in headers if h['name'] == 'Subject'), "No Subject")
                date_str = next((h['value'] for h in headers if h['name'] == 'Date'), "Unknown Date")
                
                # Clean subject for safe filenames
                safe_subject = re.sub(r'[^\w\s-]', '_', subject)[:50] 
                base_filename = f"{user_email.split('@')[0]}_{safe_subject}"

                # Parse the body and find attachments
                body_text, attachments = extract_body_and_attachments(msg_data)
                
                # Find all URLs in the text
                all_links = re.findall(r'(https?://[^\s"\'<>]+)', body_text)
                
                # --- Step A: Save Summary Text & Links to Drive ---
                summary = f"Date: {date_str}\nSubject: {subject}\n\n--- EXTRACTED LINKS ---\n"
                summary += "\n".join(all_links) if all_links else "No links found."
                summary += f"\n\n--- EMAIL BODY ---\n{body_text}"
                
                save_to_drive(summary.encode('utf-8'), f"{base_filename}_summary.txt", "text/plain", drive)
                
                # --- Step B: Download & Save Native Attachments ---
                for att in attachments:
                    att_id = att['attachmentId']
                    # Use the special attachment API endpoint
                    att_data = gmail.users().messages().attachments().get(
                        userId='me', messageId=msg['id'], id=att_id
                    ).execute()
                    
                    file_bytes = base64.urlsafe_b64decode(att_data['data'])
                    save_to_drive(file_bytes, f"{base_filename}_{att['filename']}", att['mimeType'], drive)

        except Exception as e:
            print(f"Error processing {user_email}: {e}")

if __name__ == "__main__":
    process_inboxes(None, None)