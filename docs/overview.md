
# C-HAWQ AI System
## High-Level Architecture Blueprint (V2 Multi-Agent Edition)

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
* **Visual Presentations (Slide Decks):** Introductory and discovery slide decks.

---

# 3. The Technology Stack

### AI
* **Gemini (Vertex AI) or Claude:** The core reasoning engine. Writes the artifacts, extracts action items, and answers chat queries.
* **text-embedding-004 (Vertex AI) or Voyage embeddings:** Converts text into math (vectors) so the database can search by meaning.
* **LangGraph:** The multi-agent orchestration framework. Manages the state, routes tasks between different agents, and handles "loops" (like pausing for human approval).

### Backend 
* **FastAPI:** The Python web framework. Acts as the traffic cop, catching requests from the UI or Drive and routing them.
* **Google Cloud Run:** The serverless cloud infrastructure where the FastAPI code lives.
* **GoHighLevel:** Integrates with the CRM allowing 

### Frontend 
* **React (or Next.js):** A clean web dashboard containing only two things: a **Chat Interface** (for on-demand Q&A/summaries) and a **Task Manager** (to review/edit the AI's generated PDFs and emails).
* **Slack API:** An easy to implement chat interface for interaction with the agents 

### Storage & Databases (The V1 Strategy)
* **Google Drive:** The source of truth. Humans and automated scripts drop files here. The AI reads them *in-place* without moving them.
* **ChromaDB:** Used strictly for **Local Development** vector storage (fast, offline sandbox for developers).
* **Google Firestore:** Used for **V1 Production**. Stores both the structured data (Tasks, Action Items) AND the production vector embeddings (using Firestore's native vector search).
* **PostgreSQL (`pgvector`):** The **V2 Future State**. Once V1 proves successful, all data migrates here for enterprise-grade hybrid search.

---

## 4. The Agent Ecosystem

The system utilizes LangGraph to manage a hierarchy of "Supervisor" agents that delegate complex tasks to specialized "Subagents." 

* **1. Deep Research Agent (Supervisor):**
    * *Internal Memory Subagent:* Searches the Firestore Vector DB to pull past C-HAWQ transcripts, internal notes, and previous emails.
    * *External Scraper Subagent:* Uses web scraping tools to hunt down municipal budgets, local news, and organizational history on the public internet.
* **2. Email Agent (Supervisor):**.
    * *Triage Subagent (Inbound):* Monitors inbound webhook data to categorize messages (e.g., idle chat vs. high-quality lead intent).
    * *Drafter Subagent (Outbound):* Synthesizes context to write scheduling or follow-up replies mimicking the C-HAWQ brand voice.
* **3. Presentation Agent (Supervisor):**.
    * *Outliner Subagent:* Focuses purely on sales psychology and the proven process to draft the narrative text for the slides.
    * *Design / API Subagent:* Takes the approved outline and translates it into the rigid JSON formatting required to execute the Google Slides API.
* **4. Letter Agent (Standalone):** Synthesizes context to draft formal Google Doc letters, ensuring strict alignment with the brand guide.
* **5. Evaluation & Scoring Agent (Standalone):** The pipeline manager. Continuously evaluates the lead against C-HAWQ's qualifying criteria and updates the lead's priority score and stage in the Firestore database.
* **6. Improvement Loop Agent (Standalone):** The self-refining engine. Ingests internal post-mortem debriefs and team corrections to optimize the system's prompts for future execution.

---

# 5. The System Flow

The system operates in a continuous stateful loop split into three phases:

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

# 6. Core Architectural Guardrails

* **Rule 1: The AI Adapts to the Humans.** Zero disruption to existing Drive folders. The AI reads in-place.
* **Rule 2: Asynchronous Tasks Only.** The backend must reply to webhooks and UI requests instantly while the multi-agent network processes in the background.
* **Rule 3: No Autonomous Execution.** The AI only creates drafts. A human is always the final barrier.
* **Rule 4: Full Transcripts for Memory, On-Demand for Summaries.** Do not pre-generate expensive summaries. Save the full cleaned text for accurate vector search, and let the users ask the Chat UI if they want a summary.
* **Rule 5: Stateful Lead Tracking.** The system must never treat a prompt as an isolated event; it must always check Firestore to know exactly where the lead is in the proven process before acting.

---