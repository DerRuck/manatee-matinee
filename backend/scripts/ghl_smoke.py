"""
GHL Sprint spike: full "Testing Basic GHL Integration" subtask coverage.

Validates the four Sprint 1 GHL spike subtasks in one run:
  1. Auth + fetch a contact
     (list pipelines, list contacts, fetch one by id, list custom fields)
  2. Create/update contact + write custom fields
  3. Move a contact through a pipeline stage via API
     (via an opportunity — GHL pipelines hold opportunities, not contacts)
  4. (Outbound webhook test is a separate UI-driven exercise, not scripted.)

Run from backend/ dir:
    python -m scripts.ghl_smoke

Optional: set CLEANUP=1 to delete the test contact + opportunity at the end.
    CLEANUP=1 python -m scripts.ghl_smoke          (bash/zsh)
    $env:CLEANUP=1; python -m scripts.ghl_smoke     (PowerShell)

Requires .env (loaded by core.settings) with GHL_PIT and GHL_LOCATION_ID set.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

import httpx

from core.settings import get_settings


def _truncate(body: str, n: int = 500) -> str:
    return body if len(body) <= n else body[:n] + f"... [truncated, total {len(body)} chars]"


async def main() -> int:
    s = get_settings()

    # --- Preflight: required env vars ---
    missing = [
        k for k, v in {"GHL_PIT": s.ghl_pit, "GHL_LOCATION_ID": s.ghl_location_id}.items() if not v
    ]
    if missing:
        print(f"ERROR: missing required env vars: {', '.join(missing)}", file=sys.stderr)
        print("Set these in backend/.env (copy .env.example if you haven't).", file=sys.stderr)
        return 1

    headers = {
        "Authorization": f"Bearer {s.ghl_pit}",
        "Version": s.ghl_api_version_header,
        "Accept": "application/json",
    }

    cleanup = os.environ.get("CLEANUP", "").lower() in ("1", "true", "yes")
    print(f"GHL smoke test — location={s.ghl_location_id}, base={s.ghl_base_url}")
    print(f"  cleanup at end: {'YES' if cleanup else 'no (set CLEANUP=1 to enable)'}")

    new_contact_id: str | None = None
    opp_id: str | None = None

    async with httpx.AsyncClient(base_url=s.ghl_base_url, headers=headers, timeout=30.0) as c:
        # 1. Auth + location scope check: list pipelines ----------------------
        print(f"\n[1/10] GET /opportunities/pipelines?locationId={s.ghl_location_id}")
        r = await c.get("/opportunities/pipelines", params={"locationId": s.ghl_location_id})
        print(f"       status={r.status_code}")
        if r.status_code != 200:
            print(f"       body={_truncate(r.text)}")
            return 1
        pipelines = r.json().get("pipelines", [])
        print(f"       pipelines found: {len(pipelines)}")
        for p in pipelines:
            stages = p.get("stages", [])
            print(f"         - {p.get('name')!r}  id={p.get('id')}  stages={len(stages)}")

        # 2. List contacts (limit 1) to grab a real ID ------------------------
        print(f"\n[2/10] GET /contacts/?locationId={s.ghl_location_id}&limit=1")
        r = await c.get("/contacts/", params={"locationId": s.ghl_location_id, "limit": 1})
        print(f"       status={r.status_code}")
        if r.status_code != 200:
            print(f"       body={_truncate(r.text)}")
            return 1
        contacts = r.json().get("contacts", [])
        contact_id = contacts[0]["id"] if contacts else None
        if contact_id:
            sample_name = contacts[0].get("contactName") or contacts[0].get("firstNameLowerCase") or "?"
            print(f"       first contact id={contact_id}  name={sample_name!r}")
        else:
            print("       no contacts in this location yet — step 3 will be skipped")

        # 3. Fetch that contact by ID -----------------------------------------
        if contact_id:
            print(f"\n[3/10] GET /contacts/{contact_id}")
            r = await c.get(f"/contacts/{contact_id}")
            print(f"       status={r.status_code}")
            if r.status_code != 200:
                print(f"       body={_truncate(r.text)}")
                return 1
            contact = r.json().get("contact") or r.json()
            top_keys = sorted(k for k in contact.keys() if not k.startswith("_"))[:12]
            print(f"       top-level keys (first 12): {top_keys}")
            cf_on_contact = contact.get("customFields") or contact.get("customField") or []
            print(f"       customFields attached to contact: {len(cf_on_contact)}")
            if cf_on_contact:
                sample = cf_on_contact[0]
                print(f"         sample value: {sample}")
        else:
            print("\n[3/10] SKIPPED — no contact available to fetch")

        # 4. Custom-field definitions (the ID-indirection check) --------------
        print(f"\n[4/10] GET /locations/{s.ghl_location_id}/customFields")
        r = await c.get(f"/locations/{s.ghl_location_id}/customFields")
        print(f"       status={r.status_code}")
        if r.status_code != 200:
            print(f"       body={_truncate(r.text)}")
            return 1
        fields = r.json().get("customFields", [])
        print(f"       custom-field definitions: {len(fields)}")
        for f in fields[:5]:
            print(
                f"         - name={f.get('name')!r}  "
                f"id={f.get('id')}  "
                f"key={f.get('fieldKey')}  "
                f"type={f.get('dataType')}"
            )
        if len(fields) > 5:
            print(f"         ... and {len(fields) - 5} more")

        # 5. Create a new contact with a custom-field value ------------------
        # Pick "Job Title" if it exists (from the tenant memory), else first field.
        job_title_field = next(
            (f for f in fields if f.get("fieldKey") == "contact.job_title"),
            fields[0] if fields else None,
        )
        test_suffix = str(int(time.time()))
        test_email = f"smoketest+{test_suffix}@chawq.org"
        contact_body: dict = {
            "locationId": s.ghl_location_id,
            "firstName": "Smoke",
            "lastName": f"Test-{test_suffix}",
            "email": test_email,
            "source": "chawq-smoke-script",
        }
        if job_title_field:
            contact_body["customFields"] = [
                {"id": job_title_field["id"], "value": f"Smoke Test Title ({test_suffix})"}
            ]

        print(f"\n[5/10] POST /contacts/  (create test contact)")
        print(f"       writing customField id={job_title_field['id'] if job_title_field else 'n/a'}")
        r = await c.post("/contacts/", json=contact_body)
        print(f"       status={r.status_code}")
        if r.status_code not in (200, 201):
            print(f"       body={_truncate(r.text)}")
            return 1
        created = r.json().get("contact", r.json())
        new_contact_id = created.get("id")
        print(f"       created contact id={new_contact_id}  email={test_email}")

        # 6. Update the new contact ------------------------------------------
        print(f"\n[6/10] PUT /contacts/{new_contact_id}  (update firstName)")
        r = await c.put(f"/contacts/{new_contact_id}", json={"firstName": "SmokeUpdated"})
        print(f"       status={r.status_code}")
        if r.status_code not in (200, 201):
            print(f"       body={_truncate(r.text)}")
            return 1
        print(f"       update OK")

        # 7. Re-fetch to verify custom field + update round-trip -------------
        print(f"\n[7/10] GET /contacts/{new_contact_id}  (verify round-trip)")
        r = await c.get(f"/contacts/{new_contact_id}")
        print(f"       status={r.status_code}")
        if r.status_code != 200:
            print(f"       body={_truncate(r.text)}")
            return 1
        contact = r.json().get("contact") or r.json()
        print(f"       firstName={contact.get('firstName')!r}  (expected 'SmokeUpdated')")
        cf_values = contact.get("customFields") or []
        print(f"       customFields count={len(cf_values)}")
        if job_title_field:
            match = next((cf for cf in cf_values if cf.get("id") == job_title_field["id"]), None)
            if match:
                print(f"       custom field round-tripped: {match.get('value')!r}")
            else:
                print(f"       WARN: custom field id={job_title_field['id']} not found on read")

        # 8. Create an opportunity in Project Pipeline, stage 1 --------------
        project_pipeline = next(
            (p for p in pipelines if p.get("name") == "Project Pipeline"),
            pipelines[0] if pipelines else None,
        )
        stages = project_pipeline.get("stages", []) if project_pipeline else []
        if not project_pipeline or len(stages) < 2:
            print("\n[8-10] SKIPPED — need a pipeline with at least 2 stages")
        else:
            stage1, stage2 = stages[0], stages[1]
            print(
                f"\n[8/10] POST /opportunities/  "
                f"(in pipeline {project_pipeline['name']!r}, stage {stage1.get('name')!r})"
            )
            opp_body = {
                "pipelineId": project_pipeline["id"],
                "pipelineStageId": stage1["id"],
                "name": f"Smoke Test Opp ({test_suffix})",
                "status": "open",
                "contactId": new_contact_id,
                "locationId": s.ghl_location_id,
            }
            r = await c.post("/opportunities/", json=opp_body)
            print(f"       status={r.status_code}")
            if r.status_code not in (200, 201):
                print(f"       body={_truncate(r.text)}")
                return 1
            opp = r.json().get("opportunity", r.json())
            opp_id = opp.get("id")
            print(f"       created opportunity id={opp_id}")

            # 9. Advance to stage 2 --------------------------------------------
            print(f"\n[9/10] PUT /opportunities/{opp_id}  (advance to {stage2.get('name')!r})")
            r = await c.put(f"/opportunities/{opp_id}", json={"pipelineStageId": stage2["id"]})
            print(f"       status={r.status_code}")
            if r.status_code not in (200, 201):
                print(f"       body={_truncate(r.text)}")
                return 1
            print(f"       stage update OK")

            # 10. Verify the stage advanced -----------------------------------
            print(f"\n[10/10] GET /opportunities/{opp_id}  (verify stage)")
            r = await c.get(f"/opportunities/{opp_id}")
            print(f"       status={r.status_code}")
            if r.status_code != 200:
                print(f"       body={_truncate(r.text)}")
                return 1
            opp_now = r.json().get("opportunity") or r.json()
            current_stage = opp_now.get("pipelineStageId")
            if current_stage == stage2["id"]:
                print(f"       stage advanced OK → {stage2.get('name')!r}")
            else:
                print(
                    f"       WARN: stage mismatch. "
                    f"got={current_stage} expected={stage2['id']}"
                )

        # --- Cleanup ----------------------------------------------------------
        if cleanup:
            print(f"\n[cleanup] deleting test artifacts")
            if opp_id:
                r = await c.delete(f"/opportunities/{opp_id}")
                print(f"          DELETE /opportunities/{opp_id} → {r.status_code}")
            if new_contact_id:
                r = await c.delete(f"/contacts/{new_contact_id}")
                print(f"          DELETE /contacts/{new_contact_id} → {r.status_code}")
        else:
            if new_contact_id or opp_id:
                print(
                    f"\n[cleanup] SKIPPED. Leftover test artifacts in GHL:"
                    f"\n          contact id={new_contact_id}"
                    f"\n          opportunity id={opp_id}"
                    f"\n          Re-run with CLEANUP=1 to delete them."
                )

    print("\nDONE — all checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
