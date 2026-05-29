# C-HAWQ MCP — remote / Cloud Run edition

The `mcp/` service exposes the C-HAWQ agent API as MCP tools over HTTPS so
Claude can call them as an **org-managed custom connector**. No per-user
install, no `.mcpb` bundle, no shared secret on anyone's laptop.

It replaces the local stdio MCP at `arch/chawq_mcp/` for staff users. The
local MCP stays in place for external contractors or anyone outside the
Claude org.

## Architecture

```
Claude (anywhere)
   │
   │ Streamable HTTP + X-CHAWQ-MCP-Secret
   ▼
chawq-mcp / chawq-mcp-dev  (Cloud Run, this folder)
   │
   │ HTTPS + X-CHAWQ-Secret
   ▼
chawq-api / chawq-api-dev  (Cloud Run)
   │
   ▼
Firestore, Drive, Gmail, Claude API
```

Two MCP tools exposed, identical to the local server:

- `chawq_agent_run(agent, inputs)` → POST `/agents/run`, returns `{run_id, status}`.
- `chawq_agent_status(run_id)` → GET `/agents/runs/{id}`, returns the flat completed-run JSON.

## Files

| File | Purpose |
|---|---|
| `server.py` | FastMCP server with Streamable HTTP transport. Exposes the two tools. |
| `auth.py` | ASGI middleware that validates `X-CHAWQ-MCP-Secret` on every request. |
| `requirements.txt` | `fastmcp`, `requests`, `starlette`, `uvicorn`. |
| `Dockerfile` | Python 3.12 slim, ~120 MB image. |
| `cloudbuild.dev.yaml` | Cloud Build → Cloud Run deploy for `chawq-mcp-dev`. |
| `cloudbuild.yaml` | Same for `chawq-mcp` (prod). Not yet active — see file header. |
| `smoke_remote.py` | Local smoke test against the deployed URL. Run before flipping Claude. |
| `.gcloudignore` | Keeps the deployed image small; excludes README, smoke test, etc. |

## Auth model

Two shared secrets, two distinct boundaries:

| Direction | Header | Env var (Cloud Run) | Secret Manager binding |
|---|---|---|---|
| Inbound (Claude → MCP) | `X-CHAWQ-MCP-Secret` | `CHAWQ_MCP_SHARED_SECRET` | `chawq-mcp-shared-secret:latest` |
| Outbound (MCP → API)   | `X-CHAWQ-Secret`     | `CHAWQ_SHARED_SECRET`     | `chawq-shared-secret:latest`     |

The inbound secret is what every org user inherits when the connector is
pushed to them via Claude admin settings — they never see it directly.
Rotation: update the Secret Manager value, then redeploy (the container
re-reads at start).

The outbound secret is the existing API auth and stays unchanged.

## One-time GCP setup (before the first deploy)

Run these once in the `chawq-manatee-matinee` project. Order matters.

### 1. Create the new Secret Manager secret

```bash
gcloud secrets create chawq-mcp-shared-secret \
  --replication-policy=automatic \
  --project=chawq-manatee-matinee

# Generate a 32-byte url-safe random value and store it as the first version.
# Save the printed value — Claude admin needs it too.
NEW_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
echo "Inbound secret: $NEW_SECRET"
printf "%s" "$NEW_SECRET" | gcloud secrets versions add chawq-mcp-shared-secret \
  --data-file=- \
  --project=chawq-manatee-matinee
```

### 2. Grant the runtime SA read access to both secrets

`chawq-api-runtime` is the same SA the backend uses. It already has access
to `chawq-shared-secret`; only the new MCP secret needs binding.

```bash
gcloud secrets add-iam-policy-binding chawq-mcp-shared-secret \
  --member="serviceAccount:chawq-api-runtime@chawq-manatee-matinee.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor" \
  --project=chawq-manatee-matinee
```

### 3. Set up the Cloud Build trigger

In the GCP console → Cloud Build → Triggers → Create trigger:

- **Name:** `chawq-mcp-dev`
- **Event:** Push to branch
- **Source:** the repo, branch `^dev$`
- **Included files filter (glob):** `mcp/**`
- **Configuration:** Cloud Build config file
- **Location:** Repository, file `mcp/cloudbuild.dev.yaml`
- **Service account:** `chawq-builder@chawq-manatee-matinee.iam.gserviceaccount.com`

The path filter keeps backend-only changes from redeploying the MCP and
vice versa.

### 4. First deploy

Either push something to `dev` under `mcp/` to fire the trigger, or run
manually from the repo root:

```bash
gcloud builds submit \
  --config=mcp/cloudbuild.dev.yaml \
  --project=chawq-manatee-matinee \
  .
```

Note the service URL printed at the end — something like
`https://chawq-mcp-dev-XXXXXXXXXX-uc.a.run.app`. The MCP endpoint lives at
that URL + `/mcp/` (trailing slash).

## Smoke test the deploy

Before pointing Claude at the new URL, run the smoke locally:

```bash
export CHAWQ_MCP_URL=https://chawq-mcp-dev-XXXX.us-central1.run.app/mcp/
export CHAWQ_MCP_SHARED_SECRET=<the value from step 1>

pip install fastmcp requests   # or use a venv with mcp/requirements.txt
python mcp/smoke_remote.py
```

Expected output:

```
Tools exposed: ['chawq_agent_run', 'chawq_agent_status']
Firing chawq_agent_run (research / PW-3, Rookery Bay)...
  -> run_id = <uuid>
  poll 1: status = pending
  poll 2: status = running
  ...
  poll N: status = completed
Final response: {...with drive_web_link...}
```

Anything else, check `Cloud Run → chawq-mcp-dev → Logs` before continuing.

## Wire into Claude (org connector)

The exact UI may shift, but the substance is:

1. **Claude admin → Connectors → Add custom connector**.
2. **Connector name:** `C-HAWQ Agent` (or whatever your org will see).
3. **Server URL:** `https://chawq-mcp-dev-XXXX.us-central1.run.app/mcp/`.
4. **Authentication:** Custom header.
   - Header name: `X-CHAWQ-MCP-Secret`
   - Header value: the secret from GCP setup step 1.
5. **Scope:** entire organization (or a specific group during pilot).
6. **Push** to Cowork via `managedMcpServers` so org users pick it up
   automatically without installing anything.

Verify by opening Cowork as any org user and asking *"what MCP tools do you
have access to?"* — `chawq_agent_run` and `chawq_agent_status` should show
up automatically.

## Updating the server

Change code in `mcp/`, push to `dev`. The trigger redeploys automatically.
Cloud Run does a rolling cutover; the Claude connector URL stays the same.

No action required on org users — the next tool call hits the new revision.

## Rotating the inbound secret

1. Generate a new value and store it as a new Secret Manager version:
   `gcloud secrets versions add chawq-mcp-shared-secret --data-file=-`
2. Redeploy the Cloud Run service (any trivial commit on `dev` under
   `mcp/`, or run the cloudbuild manually). The new container reads the
   `:latest` version at startup.
3. Update the header value in the Claude admin connector.

Step 3 has to come within a few minutes of step 2 to avoid 401s for org
users. If you want zero downtime, add the new secret as a non-`:latest`
version first, deploy a revision pinned to that version, update Claude,
then promote the new value to `:latest`.

## Prod cutover (later)

`cloudbuild.yaml` is staged but **not** active. Prod `chawq-api` is still
on the older schema (see memory `project_chawq_prod_dev_drift`). Once prod
catches up:

1. Update `CHAWQ_API_BASE` in `cloudbuild.yaml` to the prod API URL.
2. Create a second Cloud Build trigger on `main` with `mcp/**` filter, pointing at `cloudbuild.yaml`.
3. Deploy → smoke test → swap the Claude admin connector URL from the dev MCP to the prod MCP.

The inbound secret can stay the same across dev and prod (it's an MCP-layer
thing, not an API-layer thing) or you can issue separate values per
environment. Either way works.
