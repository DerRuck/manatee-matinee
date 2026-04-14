from fastapi import APIRouter, BackgroundTasks, Request
import uuid
# Import the background task from your ingestion folder
from backend.ingestion.document_processor import process_document

router = APIRouter()

@router.post("/api/webhooks/drive-upload")
async def handle_drive_upload(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    drive_file_id = payload.get("file_id")
    parent_folder_id = payload.get("parent_folder_id", "UNKNOWN_FOLDER")
    
    if not drive_file_id:
        return {"error": "Missing file_id", "status": 400}
    
    task_id = str(uuid.uuid4())
    background_tasks.add_task(process_document, drive_file_id, parent_folder_id, task_id)
    
    return {"status": "accepted", "task_id": task_id}