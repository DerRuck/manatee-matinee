# Local Dev Guide

How to work on the C-HAWQ backend from your own machine — set up auth, run the API locally, hit the webhook routes, and push code through the deploy pipeline.

The audience is anyone working on the backend. Command blocks below show **PowerShell** and **Bash** (macOS/Linux/WSL) variants side by side.

---

## Prerequisites

Install once:

- Python 3.12 (matches the Cloud Run runtime).
- Google Cloud SDK — `gcloud --version` should print a version number.
- A clone of `manatee-matinee` at `main`.

You do **not** need Docker for day-to-day work; the deploy uses buildpacks.

---

## Permissions for a new contributor

If this is your first time working on the project, you need GCP permissions before any of the local commands will work. Ask whoever owns `chawq-manatee-matinee` to apply the grants below. Replace `<you>@chawq.org` with your actual email.

**Required** — without these, the Drive client and Vertex AI calls will fail:

```bash
# Impersonate chawq-api-runtime (required by the local Drive + Vertex code paths)
gcloud iam service-accounts add-iam-policy-binding \
  chawq-api-runtime@chawq-manatee-matinee.iam.gserviceaccount.com \
  --member="user:<you>@chawq.org" \
  --role="roles/iam.serviceAccountTokenCreator"

# Required for ADC to set the quota project correctly
gcloud projects add-iam-policy-binding chawq-manatee-matinee \
  --member="user:<you>@chawq.org" \
  --role="roles/serviceusage.serviceUsageConsumer"
```

**Recommended** — read-only access to debug your own failures:

```bash
gcloud projects add-iam-policy-binding chawq-manatee-matinee \
  --member="user:<you>@chawq.org" --role="roles/logging.viewer"
gcloud projects add-iam-policy-binding chawq-manatee-matinee \
  --member="user:<you>@chawq.org" --role="roles/run.viewer"
gcloud projects add-iam-policy-binding chawq-manatee-matinee \
  --member="user:<you>@chawq.org" --role="roles/cloudbuild.builds.viewer"
```

You do **not** need (and should not ask for): `iam.serviceAccountUser`, `roles/owner`, or any write-shaped role. Manual `gcloud run deploy` from a developer machine is not part of the workflow — CI/CD owns deploys.

---

## One-time auth setup

Two pieces of auth wire up local dev: your user-level Google credentials, and impersonation of the runtime service account.

### 1. Application Default Credentials

PowerShell:

```powershell
gcloud auth application-default login
```

Bash:

```bash
gcloud auth application-default login
```

Sign in as your own `chawq.org` account (the one that received the IAM grants above). The runtime service account `chawq-api-runtime` is impersonated automatically by the local code path; you do not authenticate as the SA directly.

To check which identity ADC currently uses:

```bash
gcloud auth application-default print-access-token > /dev/null  # confirms creds work
gcloud auth list
```

PowerShell version of the access-token check:

```powershell
gcloud auth application-default print-access-token | Out-Null
gcloud auth list
```

### 2. gcloud CLI identity (separate from ADC)

The two are independent. Set the gcloud-CLI identity once:

```bash
gcloud config set account <you>@chawq.org
gcloud config set project chawq-manatee-matinee
```

Same command works in PowerShell. This is what `gcloud run`, `gcloud firestore`, and similar admin commands use.

---

## Repo-local setup

PowerShell:

```powershell
cd path\to\manatee-matinee\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Bash:

```bash
cd path/to/manatee-matinee/backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

You'll know it worked when the prompt is prefixed with `(.venv)`.

### .env at repo root

The repo expects a `.env` file at `backend/.env` (next to `requirements.txt`). Start from the example:

PowerShell:

```powershell
Copy-Item backend\.env.example backend\.env
```

Bash:

```bash
cp backend/.env.example backend/.env
```

Then fill in the values. Required for local work:

| Var | Value | Where it comes from |
|---|---|---|
| `GCP_PROJECT_ID` | `chawq-manatee-matinee` | Fixed |
| `GCP_LOCATION` | `us-central1` | Fixed |
| `ANTHROPIC_API_KEY` | `sk-ant-...` | C-HAWQ Anthropic console |
| `CLAUDE_MODEL` | `claude-sonnet-4-6` | Default in `.env.example` |
| `GHL_PIT` | `pit-...` | GHL → Settings → Private Integrations |
| `GHL_LOCATION_ID` | `As8Nc8kEs6J86YgDIi9Q` | Fixed for the C-HAWQ tenant |
| `DRIVE_WATCH_FOLDER_ID` | Test Output folder ID | Drive UI → folder → URL |

`DRIVE_SERVICE_ACCOUNT_FILE` is no longer used — the Drive client impersonates `chawq-api-runtime` via ADC. Leave the line as-is or delete it.

The `.env` file is gitignored. Don't commit it.

---

## Running things locally

### Smoke test the Hello World agent

The fastest sanity check that auth, the .env, and the Anthropic client are all wired up:

```bash
python -m backend.utils.test_agent
```

You should see a Claude-generated paragraph followed by token counts. Run from the repo root, not `backend/`. Same command in PowerShell.

### Run the API

PowerShell:

```powershell
cd backend
uvicorn app.main:app --reload --port 8080
```

Bash:

```bash
cd backend
uvicorn app.main:app --reload --port 8080
```

Visit `http://localhost:8080/docs` for the interactive API UI (dev-only — disabled in prod). `GET /health` should return `{"status":"ok"}`.

### Hit the GHL webhook locally

PowerShell:

```powershell
$body = @{
    contact_id = "test-contact-123"
    first_name = "Test"
    last_name  = "User"
    email      = "test@example.com"
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://localhost:8080/webhooks/ghl" `
    -Method Post `
    -Body $body `
    -ContentType "application/json"
```

Bash:

```bash
curl -X POST http://localhost:8080/webhooks/ghl \
  -H "Content-Type: application/json" \
  -d '{
    "contact_id": "test-contact-123",
    "first_name": "Test",
    "last_name":  "User",
    "email":      "test@example.com"
  }'
```

Watch the uvicorn logs in the other terminal — you should see `ghl webhook received` and `ghl webhook -> hello_world runner enqueued`.

### Run the test suite

```bash
cd backend
pytest -q
```

Same command in PowerShell. Smoke tests should pass on a fresh checkout. Run this before any push — CI/CD runs the same suite, and a red local pytest means a red pipeline.

### Vector ingest TODO

The corpus ingest is a one-shot CLI:

```bash
cd backend
python -m scripts.ingest_demo_corpus --folder-id <DRIVE_FOLDER_ID>
```

Same command in PowerShell. The folder must follow the `<municipality>/<document_type>/file` layout — see `scripts/ingest_demo_corpus.py` for the maps. To smoke-test a vector query against ingested content:

```bash
python -m scripts.test_vector_query --query "what concerns came up about dredging?"
```

---

## Push-to-deploy flow

Two services, two branches, two pipelines.

| Branch | Cloud Build config | Cloud Run service | URL | Anthropic key |
|---|---|---|---|---|
| `dev` | `cloudbuild.dev.yaml` | `chawq-api-dev` | `https://chawq-api-dev-783495307551.us-central1.run.app` | `anthropic-api-key-dev` |
| `main` | `cloudbuild.yaml` | `chawq-api` | `https://chawq-api-783495307551.us-central1.run.app` | `anthropic-api-key` |

Both services run as `chawq-api-runtime`. Both builds run as `chawq-builder`. They share the same Firestore database for V1 — chunks ingested from dev are tagged via `data_source` so prod queries can filter them out if needed. Real DB-level isolation is a V2 decision.

### The flow

Day-to-day work goes onto `dev`, gets validated against the dev URL, then merges to `main` via PR.

```bash
git checkout dev
git pull origin dev
# work, commit
git push origin dev          # fires the dev trigger -> chawq-api-dev redeploys
```

When the change is ready for prod:

```text
Open a PR: dev -> main on GitHub
After approval + merge, the main trigger fires -> chawq-api redeploys
```

Direct pushes to `main` are blocked at the GitHub branch-protection layer (require PR + review). Same git commands work in PowerShell.

`git push` fires whichever trigger matches the branch pattern. Watch builds in the [Cloud Build console](https://console.cloud.google.com/cloud-build/builds?project=chawq-manatee-matinee). pytest runs first, then `gcloud run deploy --source=...` redeploys the matching service. End-to-end takes 6–10 minutes per pipeline.

### Confirm the deploy is live

Replace `<service>` with `chawq-api` (prod) or `chawq-api-dev` (dev).

PowerShell:

```powershell
gcloud run revisions list --service=<service> --region=us-central1 --limit=5
Invoke-RestMethod https://<service>-783495307551.us-central1.run.app/health
```

Bash:

```bash
gcloud run revisions list --service=<service> --region=us-central1 --limit=5
curl https://<service>-783495307551.us-central1.run.app/health
```

The revision list should show your new revision with today's timestamp. `/health` returns `{"status":"ok"}`.

### Roll back

If a deploy turns out to be bad, route traffic back to the previous revision without re-deploying. Use `chawq-api` for prod, `chawq-api-dev` for dev.

PowerShell:

```powershell
gcloud run services update-traffic <service> `
    --to-revisions=<PRIOR_REVISION>=100 `
    --region=us-central1
```

Bash:

```bash
gcloud run services update-traffic <service> \
    --to-revisions=<PRIOR_REVISION>=100 \
    --region=us-central1
```

`<PRIOR_REVISION>` comes from the `revisions list` above (e.g. `chawq-api-00007-bvr`). Traffic shifts instantly — no rebuild needed.

### Manual deploy (escape hatch)

If CI/CD is broken and you need to ship from local, see the **Manual deploy** block in `backend/README.md`. Your gcloud session needs `ServiceAccountUser` on `chawq-api-runtime`; only project owners have that.

---

## Reading Cloud Run logs

The deployed services log to Cloud Logging. Swap `chawq-api` for `chawq-api-dev` to read dev logs.

PowerShell:

```powershell
gcloud logging read `
    'resource.type=cloud_run_revision AND resource.labels.service_name=chawq-api' `
    --limit=50 `
    --format='value(timestamp,severity,textPayload,jsonPayload.message)'
```

Bash:

```bash
gcloud logging read \
    'resource.type=cloud_run_revision AND resource.labels.service_name=chawq-api' \
    --limit=50 \
    --format='value(timestamp,severity,textPayload,jsonPayload.message)'
```

Or open the [Cloud Run service in the console](https://console.cloud.google.com/run/detail/us-central1/chawq-api/logs?project=chawq-manatee-matinee) and use the LOGS tab.

The alert policy `chawq-api ERROR alert` fires on any ERROR-severity log line on the prod service and emails `chawq-api-errors-email`. The dev service is not currently wired to that alert — dev errors stay in logs only.

---

## Common issues

**`The caller does not have permission` from Drive.** Either your IAM grants haven't been applied, or ADC is signed in as the wrong user. Confirm `gcloud auth list` shows the same account that received the `serviceAccountTokenCreator` grant. If it's correct, re-run `gcloud auth application-default login`.

**`PERMISSION_DENIED: ... act as service account 106...`** during a manual deploy. The runtime SA wasn't passed explicitly. Add `--service-account=chawq-api-runtime@chawq-manatee-matinee.iam.gserviceaccount.com`. CI/CD already includes this.

**Vertex returns `input token count is X but the model supports up to 20000`.** A single embed batch overflowed the 20K-token cap. This is handled in `services/embeddings/vertex.py` via dynamic batching at 12K estimated tokens; if you change the chunker or the batch heuristic, leave headroom — transcripts pack ~1.5–1.6 tokens/word, which is higher than the words×1.3 estimator.

**The pipeline runs but the new code isn't live.** Check the Cloud Build console for the run status. The prod trigger is on `^main$`, the dev trigger is on `^dev$` — pushes to feature branches don't deploy anywhere. Confirm you pushed to the right branch (`git log origin/<branch>..HEAD` should be empty after a successful push).
