## image Storage & Processing Pipeline (V1 Lean Edition)

## 1. DB: Image Storage - Criteria & Specs

### 1.1 Storage Location
* **System of Record:** All raw files (images, PDFs, audio) remain entirely in **Google Drive**. 
* **Constraint:** Adhering to the V1 "Zero Disruption" rule, we will *not* move files to secondary blob storage. The AI must read the raw file in-place using the Google Drive API.
* **Access Control:** The system requires the Drive File ID and appropriate read permissions (via Service Account) to access files directly.

### 1.2 Database & Schema Location
* **V1 Production Environment:** **Google Firestore** is used to store both the metadata and vector embeddings.
* **Local Development Environment:** **ChromaDB** is used strictly for offline, rapid testing.

### 1.3 Universal Image/Document Schema (Firestore `documents` collection)
To support multiple file types (badges, meeting notes, generic PDFs) without breaking the system, we use the following schema:

* `id` (String/UUID)
* `drive_file_id` (String) - *Used to fetch the original image/file for human verification.*
* `upload_timestamp` (Timestamp)
* `uploader_id` (String)
* `parent_folder_id` (String) - *Used in V1 for routing logic.*
* `document_type` (String) - *Enum: `conference_badge`, `meeting_notes`, `general_pdf`, `unknown`, `etc`*
* `processing_status` (String) - *Enum: `pending`, `success`, `failed`*
* `raw_text` (Text) - *The full cleaned text of the document/OCR for memory and vectorization.*
* `extracted_metadata` (Map/Object) - *Flexible dictionary based on `document_type`:*
    * *If `conference_badge`: `{name, job_title, organization, email, phone}`*
    * *If `meeting_notes`: `{attendees, date, key_topics}`*
* `action_items_found` (Boolean) - *Did the AI find triggers for a background task?*
* `embedding` (Vector/Array) - *The `text-embedding-004` output for semantic search.*

---

## 2. Processing Pipeline Architecture

### 2.1 Routing Strategy: V1 vs. V2
* **V1 (Current): Drive Folder Routing.** The FastAPI webhook determines how to process the file based strictly on which Drive folder it was dropped into (e.g., dropping a file into the "Lead Badges" folder triggers the badge schema; the "Meeting Notes" folder triggers the notes schema).
* **V2 (Planned): AI Triage Agent.** All files will go into a single ingestion pipeline. An AI Agent will execute a preliminary "Triage" step to automatically classify the document type before dynamically loading the correct extraction prompt and schema.

### 2.2 Pipeline Flow (Asynchronous)
1. **Trigger:** A file is uploaded to a monitored Google Drive folder. A webhook alerts the FastAPI backend.
2. **Acknowledge & Route:** FastAPI identifies the `parent_folder_id`, selects the appropriate prompt, and immediately returns a `200 OK` / `Task ID` to close the webhook loop.
3. **Extraction:** The background worker fetches the file via the Drive API and passes it to **Gemini (Vertex AI)** using the folder-specific prompt.
4. **Vectorization:** The `raw_text` is passed to **text-embedding-004** to generate the semantic vector.
5. **Storage:** The document object and vector are saved to ChromaDB (Local) or Firestore (Production).
6. **Action:** If `action_items_found` is true, Firestore triggers the orchestration agent to draft the necessary artifacts (emails, docs).

### 2.3 GCP "manatee-matinee" Implementation Path
1. **Service Account:** Create `c-hawq-reader@manatee-matinee.iam.gserviceaccount.com` with `Vertex AI User` and `Cloud Datastore User` roles.
2. **Drive Access:** Share the target Google Drive folders directly with the Service Account email.
3. **Drive Webhook:** Use native Google Drive API Push Notifications to POST to Cloud Run.
4. **Deployment:** Deploy FastAPI to Cloud Run attached to the Service Account for keyless native authentication.
