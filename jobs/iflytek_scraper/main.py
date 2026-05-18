import os
import re
import html
import datetime
import requests
import google.auth
from playwright.sync_api import sync_playwright
from googleapiclient.discovery import build
from google.auth import impersonated_credentials
import base64
import assemblyai as aai
from deepgram import DeepgramClient
from googleapiclient.http import MediaIoBaseUpload
import io
import anthropic

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

def save_html_as_gdoc(html_content, filename, drive_service):

    file_metadata = {
        'name': filename,
        'parents': [os.getenv("DRIVE_FOLDER_ID")],
        'mimeType': 'application/vnd.google-apps.document' 
    }
    
    media = MediaIoBaseUpload(
        io.BytesIO(html_content), 
        mimetype='text/html', 
        resumable=True
    )
    
    try:
        file = drive_service.files().create(
            body=file_metadata, 
            media_body=media, 
            fields='id',
            supportsAllDrives=True
        ).execute()
        print(f"Successfully created Google Doc: {filename}")
        return file.get('id')
    except Exception as e:
        print(f"Failed to create Google Doc: {e}")
        return None

def transcribe_audio(audio_bytes):
    print("Attempting primary transcription (AssemblyAI)...")
    result = transcribe_audio_assemblyai(audio_bytes)
    
    transcript_text = result[0] if isinstance(result, tuple) else result
    if transcript_text and not transcript_text.startswith("ERROR:"):
        print("AssemblyAI transcription successful!")
        return transcript_text
    
    print(f"AssemblyAI failed ({transcript_text}). Triggering fallback to Deepgram...")
    
    transcript_text = transcribe_audio_deepgram(audio_bytes)
    
    if transcript_text and not transcript_text.startswith("ERROR:"):
        print("Deepgram fallback successful!")
        return transcript_text
        
    # If BOTH fail, return a total failure error
    print("CRITICAL: Both primary and fallback transcription services failed.")
    return "ERROR: ALL_SERVICES_FAILED"

def transcribe_audio_assemblyai(audio_bytes):
    aai.settings.api_key = os.getenv("ASSEMBLYAI_API_KEY")
    
    transcriber = aai.Transcriber()
    config = aai.TranscriptionConfig(
        speech_models=["universal-3-pro", "universal-2"],
        speaker_labels=True
    )
    
    try:
        transcript = transcriber.transcribe(audio_bytes, config=config)
        if transcript.status == aai.TranscriptStatus.error:
            if "credit" in transcript.error.lower():
                error_msg = "ERROR: AssemblyAI Out of Credits"
                print(error_msg)
                return error_msg
                
            print(f"ERROR: AssemblyAI Error: {transcript.error}")
            return "ERROR: Transcription completely failed."
            
        final_text = ""
        for utterance in transcript.utterances:
            final_text += f"Speaker {utterance.speaker}: {utterance.text}\n"
            
        return final_text
    except Exception as e:
        print(f"AssemblyAI Transcription Failed: {e}")
        return "ERROR: Transcription completely failed."

def transcribe_audio_deepgram(audio_bytes):
    deepgram = DeepgramClient()
        
    try:
        response = deepgram.listen.v1.media.transcribe_file(
            request=audio_bytes,
            model="nova-3",
            smart_format=True,
            diarize=True
        )
        
        alternatives = response.results.channels[0].alternatives[0]
        if hasattr(alternatives, 'paragraphs') and alternatives.paragraphs:
            return alternatives.paragraphs.transcript
        else:
            return alternatives.transcript
            
    except Exception as e:
        print(f"Deepgram Transcription Failed: {e}")
        return "ERROR: Transcription completely failed."

def generate_claude_summary(transcript_text):
    if not transcript_text or transcript_text.startswith("ERROR:"):
        return None

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    model_name = os.getenv("CLAUDE_MODEL", "claude-opus-4-7")
    
    # custom prompt 
    system_prompt = (
        "You are the C-HAWQ AI Assistant. You embody 'The Lead Scientist' and "
        "'The Supportive Advisor'. Analyze the following meeting transcript. "
        "Provide a structured summary with: \n"
        "1. A 2-sentence executive overview.\n"
        "2. Key Takeaways (Bullet points).\n"
        "3. Action Items & Next Steps (Bullet points).\n"
        "Do NOT use emojis. Maintain an objective, confident, and optimistic tone.\n"
        "CRITICAL: Output the entire response in clean, raw HTML format using <h1>, <h2>, <ul>, <li>, and <strong> tags. Do NOT wrap your response in markdown code blocks."
    )

    try:
        response = client.messages.create(
            model=model_name,
            max_tokens=1024,
            system=system_prompt,
            messages=[
                {"role": "user", "content": f"Here is the transcript to summarize:\n\n{transcript_text}"}
            ]
        )
        return response.content[0].text

    except Exception as e:
        print(f"Claude Summarization Failed: {e}")
        return "ERROR: Could not generate summary via Claude."    

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
    body_parts = {'text/plain': '', 'text/html': ''}
    
    # Start our stack with the top-level payload
    stack = [msg_data.get('payload', {})]
    while stack:
        part = stack.pop()
        mime_type = part.get('mimeType')
        
        # If text, decode and save it
        if mime_type in ['text/plain', 'text/html']:
            data = part.get('body', {}).get('data')
            if data:
                decoded = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
                body_parts[mime_type] += decoded
                
        if 'parts' in part:
            stack.extend(reversed(part['parts']))
    
    if body_parts['text/plain']:
        return body_parts['text/plain']
    elif body_parts['text/html']:
        return body_parts['text/html']
    else:
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
        # 1. Get the full, un-truncated body
        raw_body = extract_body(msg_data)
        body = html.unescape(raw_body)
        all_links = re.findall(r'(https?://[^\s"\'<>]+)', body)
        
        share_url = None
        for link in all_links:
            if "share-ap1.theainote.com" in link:
                share_url = link
                print(f"Found URL: {share_url}")
                break 
                
        if share_url:
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
            audio_elem = page.query_selector("audio") or page.query_selector("video")
            audio_src = audio_elem.get_attribute("src") if audio_elem else None
            if audio_src:
                r = requests.get(audio_src)
                save_to_drive(r.content, f"{base_name}.opus", "audio/ogg", drive_service)
                
                # Transcribe audio
                transcript_text = transcribe_audio(r.content)
                
                if transcript_text and not transcript_text.startswith("ERROR:"):
                    save_to_drive(transcript_text.encode('utf-8'), f"{base_name}_Transcript.txt", "text/plain", drive_service)
                    summary_html = generate_claude_summary(transcript_text)
                    if summary_html:
                        summary_html = summary_html.replace("```html", "").replace("```", "").strip()
                        save_html_as_gdoc(
                            summary_html.encode('utf-8'), 
                            f"{base_name} Summary",
                            drive_service
                        )
                        
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

            # TXT so far the text transcript is always blank, just skipping for now
            # if not page.is_visible(".export-options"): page.click(".share-btn")
            # with page.expect_download() as dl_txt:
            #     page.locator(".export-option", has_text="Export TXT").click()
            # download = dl_txt.value
            # txt_path = download.path()
            # with open(txt_path, "rb") as f:
            #     save_to_drive(f.read(), f"{base_name}.txt", "text/plain", drive_service)

            browser.close()
        return True
    except Exception as e:
        print(f"Scraping error: {e}")
        return False


if __name__ == "__main__":
    process_inbox(None, None)