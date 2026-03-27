
# C-HAWQ AI System
## High-Level Architecture Blueprint (V1 Lean Edition)

---

# 1. System Overview & Purpose

The C-HAWQ AI System is a **Task-Oriented Hybrid RAG application**. 

Its purpose is to silently read the company's existing Google Drive data, understand the business context, and assist the team by automatically drafting highly specific, formatted outputs (Artifacts) while keeping a human firmly in control of the final send.

---

# 2. Core Outputs (The Artifacts)

The system is strictly scoped to generate these outputs. (Note: General meeting summaries are generated *on-demand* via the Chat UI, not pre-generated).

* **Email Drafts:** Written in the Company tone, pushed directly to their Gmail Drafts folder.
* **Meeting Prep Notes:** Structured agendas and pain-point summaries, pushed to a new Google Doc.
* **Formal PDF Letters:** Proposals and formal follow-ups, generated and edited in the web app, then downloaded.

---

# 3. The Technology Stack

### AI
* **Gemini 1.5 Pro (Vertex AI):** The core reasoning engine. Writes the artifacts, extracts action items, and answers chat queries.
* **text-embedding-004 (Vertex AI):** Converts text into math (vectors) so the database can search by meaning.
* **LangChain:** The Python library that wires the LLM to the databases.

### Backend 
* **FastAPI:** The Python web framework. Acts as the traffic cop, catching requests from the UI or Drive and routing them.
* **Google Cloud Run:** The serverless cloud infrastructure where the FastAPI code lives.

### Frontend 
* **React (or Next.js):** A clean web dashboard containing only two things: a **Chat Interface** (for on-demand Q&A/summaries) and a **Task Manager** (to review/edit the AI's generated PDFs and emails).

### Storage & Databases (The V1 Strategy)
* **Google Drive:** The source of truth. Humans and automated scripts drop files here. The AI reads them *in-place* without moving them.
* **ChromaDB:** Used strictly for **Local Development** vector storage (fast, offline sandbox for developers).
* **Google Firestore:** Used for **V1 Production**. Stores both the structured data (Tasks, Action Items) AND the production vector embeddings (using Firestore's native vector search).
* **PostgreSQL (`pgvector`):** The **V2 Future State**. Once V1 proves successful, all data migrates here for enterprise-grade hybrid search.

---

# 4. The 8-Step System Flow

The system operates in a single, continuous loop, split into three phases:

### Phase A: Data Ingestion (Hunting & Memorizing)
1. **The Raw Input:** A Human or automated script/scraper drops a raw file (audio, transcript, PDF, etc) into their normal Google Drive folder.
2. **Clean & Hunt (Firestore):** A webhook alerts FastAPI or serverless function. The backend reads the file and cleans out filler words to create a **Cleaned Full Transcript**. It then uses the LLM to quickly hunt for one thing: *urgent action items/triggers*. These triggers are saved to **Firestore**.
3. **Vectorization (Chroma/Firestore):** The **Cleaned Full Transcript** is chopped into chunks, turned into vectors, and saved to the Vector DB (Chroma locally, Firestore in prod). This is the AI's permanent memory.

### Phase B: Action & Generation
4. **The Orchestrator Decides:** FastAPI checks the action items it just saved. If it spots a trigger, it creates a background "Task" (e.g., *"Draft an email"*). Alternatively, a human clicks a button in React to manually create a Task.
5. **Retrieval:** FastAPI searches the Vector DB for the exact past context needed for the Task.
6. **Generation:** Gemini takes the retrieved context and writes the raw text for the Email, PDF, or Google Doc.

### Phase C: Human Control & Learning
7. **Execution & Handoff:** FastAPI pushes the draft to the PM's Gmail or the React UI. **The human reviews, edits, and sends/exports it.** The AI never executes autonomously.
8. **The Learning Loop:** When the human clicks "Send/Export", the Evaluator Agent compares the AI's draft to the human's final version, extracts the lesson, and saves it so the AI improves next time.

---

# 5. Core Architectural Guardrails

* **Rule 1: The AI Adapts to the Humans.** Zero disruption to existing Drive folders. The AI reads in-place.
* **Rule 2: Asynchronous Tasks Only.** The backend must always reply to the frontend instantly with a "Task ID" while the AI generates in the background.
* **Rule 3: No Autonomous Execution.** The AI only creates drafts. A human is always the final barrier.
* **Rule 4: Full Transcripts for Memory, On-Demand for Summaries.** Do not pre-generate expensive summaries. Save the full cleaned text for accurate vector search, and let the users ask the Chat UI if they want a summary.

---