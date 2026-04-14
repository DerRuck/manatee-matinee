import io
import json
import google.auth
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import vertexai
from vertexai.generative_models import GenerativeModel, Part
# Import your vector store router
from backend.db.vector_store import save_document_to_db

# Initialize GCP Keyless Auth
credentials, project_id = google.auth.default()
drive_service = build('drive', 'v3', credentials=credentials)
vertexai.init(project="manatee-matinee", location="us-central1", credentials=credentials)
model = GenerativeModel("gemini-1.5-pro")

BADGE_PROMPT = """
You are an expert data extraction assistant. Analyze this conference badge.
Return strictly a JSON object. Schema: {"name": "", "job_title": "", "organization": "", "email": "", "phone": ""}
Return null for missing fields. Do not include markdown formatting.
"""

def process_document(drive_file_id: str, folder_id: str, task_id: str):
    print(f"[{task_id}] Fetching image {drive_file_id} from Drive...")
    try:
        # 1. Download image
        request = drive_service.files().get_media(fileId=drive_file_id)
        file_stream = io.BytesIO()
        downloader = MediaIoBaseDownload(file_stream, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
        
        # 2. Extract Data via Gemini
        image_part = Part.from_data(data=file_stream.getvalue(), mime_type="image/jpeg")
        response = model.generate_content([image_part, BADGE_PROMPT], generation_config={"response_mime_type": "application/json"})
        extracted_data = json.loads(response.text)
        
        # 3. Pass to Database Router
        save_document_to_db(drive_file_id, extracted_data)
        print(f"[{task_id}] Processing complete!")
        
    except Exception as e:
        print(f"[{task_id}] Error: {str(e)}")