"""
Smoke-test all 24 research agent types in pipeline order.

Each test uses --no-web-search (training-data only) to run fast and free.
A pass means: inputs resolved, binder retrieved, Claude responded, schema
validated, and render_docx produced a non-empty Word document.
Web search correctness is tested separately in golden evals.

The test fixture follows one real project thread throughout —
Gainesville / Hogtown Creek Stormwater Retrofit — so each step's
inputs reflect what the previous step would have produced.

Run from backend/:
    python scripts/test_all_agents.py
    python scripts/test_all_agents.py --stop-on-fail
    python scripts/test_all_agents.py --types LOBBY-1 S5-1 S9-3
    python scripts/test_all_agents.py --web-search --drive --save-dir /tmp/briefs
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve()
_BACKEND = _HERE.parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from dotenv import load_dotenv
load_dotenv(_BACKEND.parent / ".env")

from agents.research_agent import ResearchAgent

# ---------------------------------------------------------------------------
# Test order — functional pipeline sequence
# ---------------------------------------------------------------------------

TESTS: list[tuple[str, dict]] = [

    # =========================================================================
    # PRE-WORK — compliance and prospect intelligence
    # =========================================================================

    # Lobbyist registration check — must clear before any formal outreach
    ("LOBBY-1", {
        "contact_id":        "ghl_test_005",
        "municipality_name": "Alachua County",
        "jurisdiction_name": "Alachua County",
        "jurisdiction_type": "county",
    }),

    # Municipality background — research Gainesville before Step 1
    ("PW-3", {
        "contact_id":        "ghl_test_003",
        "municipality_name": "Gainesville",
        "county":            "Alachua",
        "state":             "FL",
        "contact_name":      "Alex Rivera",
        "contact_title":     "Public Works Director",
    }),

    # =========================================================================
    # PHASE 1 — DISCOVERY (Steps 1–4)
    # =========================================================================

    # S1-4: Full internet research on Alex Rivera before first contact
    ("S1-4", {
        "contact_id":        "ghl_test_003",
        "municipality_name": "Gainesville",
        "contact_name":      "Alex Rivera",
        "contact_title":     "Public Works Director",
        "organization":      "City of Gainesville",
    }),

    # S1-2: LinkedIn connection prep
    ("S1-2", {
        "contact_id":        "ghl_test_003",
        "municipality_name": "Gainesville",
        "contact_name":      "Alex Rivera",
        "contact_title":     "Public Works Director",
        "organization":      "City of Gainesville",
    }),

    # PW-1: Conference attendee research — pre-event lead mapping
    ("PW-1", {
        "contact_id":        "ghl_test_conference_001",
        "municipality_name": None,
        "conference_name":   "FSBPA 2026 Annual Conference",
        "conference_date":   "2026-09-15",
        "location":          "Florida",
    }),

    # S3-PREP: Pre-meeting research package — intake meeting with Alex
    ("S3-PREP", {
        "contact_id":        "ghl_test_003",
        "municipality_name": "Gainesville",
        "county":            "Alachua",
        "contact_name":      "Alex Rivera",
        "contact_title":     "Public Works Director",
        "meeting_date":      "2026-06-10",
        "project_hint":      "Stormwater retrofit along Hogtown Creek; stalled in FDEP for two years.",
    }),

    # S3-3: Commission meeting prep — observe before intake meeting
    ("S3-3", {
        "contact_id":        "ghl_test_003",
        "municipality_name": "Gainesville",
        "meeting_date":      "2026-06-09",
        "meeting_goal":      "observe",
        "project_status":    "Pre-Step-3 — contact agreed to intake, no formal proposal yet.",
    }),

    # S4-DECK: Deck research — presentation prep after Go decision
    ("S4-DECK", {
        "contact_id":        "ghl_test_003",
        "municipality_name": "Gainesville",
        "project_focus":     "Hogtown Creek stormwater retrofit and riparian buffer restoration",
        "problem_areas":     "Hogtown Creek — three undersized outfalls causing nitrogen loading into the Santa Fe River watershed.",
        "champion_priorities": (
            "Visible water quality results, P3 funding structure, nature-based approach. "
            "Alex wants before/after data that will hold up at a commission meeting."
        ),
    }),

    # =========================================================================
    # PHASE 2 — DEVELOPMENT (Steps 5–6)
    # =========================================================================

    # S5-1: Internal presentation prep — briefing city staff
    ("S5-1", {
        "contact_id":        "ghl_test_003",
        "municipality_name": "Gainesville",
        "county":            "Alachua",
        "presentation_date": "2026-07-15",
        "audience_roles":    (
            "Public Works Director, Stormwater Manager, Parks and Recreation staff, "
            "City Manager's office"
        ),
        "project_description": (
            "Stormwater retrofit along Hogtown Creek — three outfall improvements and "
            "4 acres of riparian buffer restoration to reduce nitrogen loading into "
            "the creek by an estimated 40%."
        ),
        "project_type":      "stormwater_retrofit",
        "champion_concerns": (
            "Finance staff may ask about ongoing maintenance costs. "
            "Public Works may push back on construction timeline overlapping rainy season."
        ),
    }),

    # S5-2: Post-internal meeting debrief — notes from city staff briefing
    ("S5-2", {
        "contact_id":        "ghl_test_003",
        "municipality_name": "Gainesville",
        "presentation_date": "2026-07-15",
        "champion_name":     "Alex Rivera",
        "project_description": "Hogtown Creek Stormwater Retrofit",
        "meeting_notes": (
            "Attended by: Alex Rivera (Public Works Dir, champion), Keisha Thompson "
            "(Stormwater Mgr), Marcus Dunn (Parks), Linda Patel (City Manager's office). "
            "Alex's intro was strong. Keisha asked detailed questions about sediment load "
            "data — very engaged, asked to receive the monitoring report when done. "
            "Marcus was quiet but asked about the riparian planting species list — seemed "
            "interested in aesthetics and parks use. "
            "Linda was skeptical: asked three times about liability if construction disturbs "
            "neighboring properties. Seemed unconvinced by the P3 structure. "
            "No outright opposition. Alex said after the meeting Linda is the key blocker. "
            "Follow-up: Alex will set up a separate call with Linda and the city attorney."
        ),
    }),

    # S6-1: Grant opportunity research — funding identification
    ("S6-1", {
        "contact_id":        "ghl_test_003",
        "municipality_name": "Gainesville",
        "county":            "Alachua",
        "project_type":      "stormwater_retrofit",
        "estimated_cost_usd": 2_800_000,
        "project_overview": (
            "Retrofit three stormwater outfalls and restore 4 acres of riparian buffer "
            "along Hogtown Creek to reduce nitrogen loading by an estimated 40%. "
            "Hogtown Creek is on FDEP's impaired waters list for nitrogen."
        ),
        "p3_intent": "yes",
    }),

    # S6-2: Project narrative draft — commission and grant writing assets
    ("S6-2", {
        "contact_id":        "ghl_test_003",
        "municipality_name": "Gainesville",
        "county":            "Alachua",
        "project_name":      "Hogtown Creek Stormwater Retrofit",
        "project_type":      "stormwater_retrofit",
        "problem_statement": (
            "Hogtown Creek receives nitrogen-laden stormwater runoff from three "
            "undersized and unmaintained outfalls, contributing to algae blooms and "
            "degraded water quality throughout the Santa Fe River watershed."
        ),
        "estimated_cost_usd": 2_800_000,
        "project_description": (
            "Retrofit three stormwater outfalls and restore 4 acres of riparian "
            "buffer along Hogtown Creek, targeting a 40% reduction in nitrogen loading."
        ),
        "key_data": (
            "Current nitrogen loading: 1,200 lbs/year at the three monitored outfalls. "
            "FDEP 2024 basin assessment rated the Creek as impaired for nitrogen."
        ),
        "funding_approach":  "FDEP Section 319 grant (~$1.2M) + P3 private capital ($800K) + City stormwater fund ($800K)",
        "anticipated_timeline": "Construction start Q1 2027, 14-month build",
        "community_benefit": (
            "Improved water quality and aquatic habitat for 3.2 miles of Hogtown Creek; "
            "reduced flood risk for adjacent neighborhoods"
        ),
    }),

    # S6-3: Commission presentation prep — vote on the project package
    ("S6-3", {
        "contact_id":        "ghl_test_003",
        "municipality_name": "Gainesville",
        "county":            "Alachua",
        "project_name":      "Hogtown Creek Stormwater Retrofit",
        "presentation_date": "2026-09-08",
        "project_type":      "stormwater_retrofit",
        "estimated_cost_usd": 2_800_000,
        "project_package_contents": (
            "Engineering feasibility study, FDEP Section 319 pre-application, "
            "preliminary cost estimate (±30%), P3 partnership term sheet"
        ),
        "commission_composition": (
            "5-member City Commission. Mayor Harvey Ward (environmental advocate, strong ally). "
            "Commissioner Reina Soto (fiscal conservative, skeptical of P3). "
            "Commissioner Bryan Eastman (neutral, data-driven, attended community event). "
            "Two commissioners: no prior contact, disposition unknown."
        ),
        "champion_read": (
            "Alex says Harvey will champion the vote. Reina is the main risk — her concern "
            "is whether the city retains full control of the creek corridor. Bryan will "
            "follow the data."
        ),
    }),

    # =========================================================================
    # PHASE 3 — MOBILIZATION (Steps 7–8)
    # =========================================================================

    # S7-1: Post-event debrief — Hogtown Creek community day
    ("S7-1", {
        "contact_id":        "ghl_test_003",
        "municipality_name": "Gainesville",
        "event_name":        "Hogtown Creek Community Day",
        "event_date":        "2026-08-02",
        "project_description": "Hogtown Creek Stormwater Retrofit",
        "event_notes": (
            "Roughly 120 people attended. Exhibit drew strong engagement at the water "
            "quality demonstration station — the algae jar comparison was the most-"
            "photographed element. Logan's habitat talk drew ~30 people for the full "
            "15 minutes. "
            "Notable conversations: "
            "Dr. Sarah Macon (Alachua Conservation Trust board member) — very interested, "
            "asked about volunteer monitoring after construction, said ACT would consider "
            "co-signing a letter of support. "
            "James Whitfield (Hogtown Creek Estates HOA president) — supportive but worried "
            "about construction noise and access disruption. Left his card. "
            "Commissioner Bryan Eastman attended for about 20 minutes and spent time at "
            "the before/after photo panels. Did not introduce himself. "
            "34 sign-in entries collected. Gainesville Sun sent a photographer, no reporter. "
            "Setup issue: tent blew over in afternoon wind — need weights next time."
        ),
        "new_contacts": (
            "Dr. Sarah Macon — Alachua Conservation Trust board member, interested in "
            "post-construction monitoring partnership. "
            "James Whitfield — Hogtown Creek Estates HOA president, supportive but "
            "concerned about construction disruption."
        ),
    }),

    # S8-1: Political landscape mapping — pre-vote commissioner profiles
    ("S8-1", {
        "contact_id":        "ghl_test_003",
        "municipality_name": "Gainesville",
        "county":            "Alachua",
        "project_name":      "Hogtown Creek Stormwater Retrofit",
        "project_type":      "stormwater_retrofit",
        "estimated_cost_usd": 2_800_000,
        "commissioner_assessments": (
            "Harvey Ward (Mayor): strong environmental advocate, supportive. "
            "Reina Soto: fiscal conservative, skeptical of P3, worried about city retaining control. "
            "Bryan Eastman: data-driven, neutral, attended community event. "
            "Two commissioners: no prior contact, unknown."
        ),
        "city_staff_notes": (
            "City Manager: supportive of stormwater improvements. "
            "City Attorney: has questions about P3 liability. "
            "Finance Director: cautious about matching fund obligations."
        ),
        "community_support": "Alachua Conservation Trust (Dr. Sarah Macon), Hogtown Creek Estates HOA",
        "known_opposition":  "Local property owner near outfall #2 has raised concerns about construction access.",
        "vote_timeframe":    "Commission meeting September 8, 2026 — approximately 5 weeks out",
    }),

    # S8-2: Community support letter — for ACT and HOA to sign and submit
    ("S8-2", {
        "contact_id":        "ghl_test_003",
        "municipality_name": "Gainesville",
        "county":            "Alachua",
        "project_name":      "Hogtown Creek Stormwater Retrofit",
        "project_summary": (
            "A public-private partnership to retrofit three stormwater outfalls and "
            "restore 4 acres of riparian buffer along Hogtown Creek, reducing nitrogen "
            "loading by 40% and improving water quality for 3.2 miles of the creek."
        ),
        "community_benefits": (
            "Improved water quality and aquatic habitat; reduced algae blooms; lower "
            "flood risk for adjacent neighborhoods; restored trail access along the "
            "creek corridor"
        ),
        "commission_body_name": "Gainesville City Commission",
        "signature_line_count": 5,
    }),

    # S8-3: Politician-friendly briefing — one-on-one with Commissioner Soto
    ("S8-3", {
        "contact_id":        "ghl_test_003",
        "municipality_name": "Gainesville",
        "county":            "Alachua",
        "project_name":      "Hogtown Creek Stormwater Retrofit",
        "official_name":     "Commissioner Reina Soto",
        "official_title":    "City Commissioner",
        "tenure":            "3 years on the commission",
        "known_priorities":  "Fiscal responsibility, transparent procurement, city retaining control of public assets",
        "political_style":   "Data-driven, skeptical of outside organizations, asks detailed contract questions before voting",
        "known_concerns": (
            "Worried the P3 structure gives C-HAWQ or the GC too much control over the "
            "creek corridor. Asked staff whether the city attorney has reviewed P3 "
            "precedents in Gainesville."
        ),
        "project_backstory": (
            "Alex Rivera introduced C-HAWQ after attending the Florida Stormwater "
            "Association conference. Three internal briefings completed. "
            "Project package delivered to commission."
        ),
        "project_description": "Stormwater outfall retrofit and riparian buffer restoration along Hogtown Creek.",
        "environmental_problem": "Hogtown Creek is on FDEP's impaired waters list for nitrogen. Three aging outfalls are the primary source.",
        "exploration_grant_amount": 185_000,
        "municipality_ask": (
            "Authorization for the City Manager to execute the P3 term sheet and begin "
            "formal procurement under F.S. § 255.065"
        ),
    }),

    # =========================================================================
    # PHASE 4 — EXECUTION (Steps 9–10)
    # =========================================================================

    # S9-1: Kickoff deck — first formal meeting after P3 agreement signed
    ("S9-1", {
        "contact_id":        "ghl_test_003",
        "municipality_name": "Gainesville",
        "project_name":      "Hogtown Creek Stormwater Retrofit",
        "kickoff_date":      "2026-11-12",
        "municipality_attendees": "Alex Rivera (Public Works Director), Keisha Thompson (Stormwater Manager), Linda Patel (City Manager's office)",
        "chawq_attendees":   "Emily Begin, Logan Davies",
        "gc_partner_name":   "Coastal Restoration Partners LLC",
        "grant_admin_attendee": "Maria Santos, FDEP Section 319 Program Manager",
        "project_description": "Retrofit three stormwater outfalls and restore 4 acres of riparian buffer along Hogtown Creek.",
        "funding_structure": (
            "FDEP Section 319 grant: $1,200,000 | "
            "P3 private capital (Coastal Restoration Partners): $800,000 | "
            "City stormwater fund: $800,000 | Total: $2,800,000"
        ),
        "milestone_schedule": (
            "Permitting: Nov 2026–Apr 2027 | Procurement: Jan–Mar 2027 | "
            "Construction: May 2027–Jul 2028 | Monitoring: Ongoing"
        ),
        "open_items": (
            "Sovereignty land lease application for outfall #3 not yet submitted. "
            "NPDES permit timeline TBD."
        ),
    }),

    # S9-2: Media & reporter research — for partnership announcement
    ("S9-2", {
        "contact_id":        "ghl_test_003",
        "municipality_name": "Gainesville",
        "county":            "Alachua",
        "announcement_type": "partnership_announcement",
        "project_summary": (
            "Gainesville and C-HAWQ have signed a public-private partnership to "
            "restore water quality along Hogtown Creek through stormwater outfall "
            "retrofits and riparian buffer restoration."
        ),
        "champion_name":     "Alex Rivera",
        "champion_title":    "Public Works Director, City of Gainesville",
        "chawq_spokesperson": "Emily Begin, C-HAWQ",
        "project_type":      "stormwater_retrofit",
    }),

    # S9-3: Grant compliance checklist — after FDEP Section 319 award
    ("S9-3", {
        "contact_id":        "ghl_test_003",
        "municipality_name": "Gainesville",
        "project_name":      "Hogtown Creek Stormwater Retrofit",
        "grant_program_name": "FDEP Section 319 Nonpoint Source Management Program",
        "award_amount_usd":  1_200_000,
        "grant_period_start": "2026-10-01",
        "grant_period_end":  "2029-09-30",
        "administering_agency": "Florida Department of Environmental Protection",
        "project_scope": (
            "Stormwater outfall retrofit and riparian buffer restoration along "
            "Hogtown Creek, Gainesville FL"
        ),
        "p3_partners":       "Coastal Restoration Partners LLC",
    }),

    # S9-4: P3 proposal drafting — RFP for GC partner selection
    ("S9-4", {
        "contact_id":        "ghl_test_003",
        "municipality_name": "Gainesville",
        "county":            "Alachua",
        "project_name":      "Hogtown Creek Stormwater Retrofit",
        "project_description": (
            "Retrofit three aging stormwater outfalls along Hogtown Creek and restore "
            "4 acres of riparian buffer. Includes hydraulic engineering, native "
            "planting, and a 3-year post-construction monitoring program."
        ),
        "estimated_cost_usd": 2_800_000,
        "procurement_path":  "solicited_rfp",
        "funding_structure": (
            "P3 private capital: $800,000 | "
            "FDEP Section 319 grant: $1,200,000 | "
            "City stormwater fund: $800,000"
        ),
        "complexity_factors": (
            "GC must contribute $800K upfront private capital. "
            "Sovereignty land lease required for outfall #3 on navigable waterway. "
            "FDEP Section 319 grant requires municipality as applicant of record. "
            "Academic monitoring requirement (UF IFAS) for 3 years post-construction."
        ),
        "project_type":      "stormwater_retrofit",
    }),

    # S9-5: Partnership agreement summary — reviewing draft P3 agreement
    ("S9-5", {
        "contact_id":        "ghl_test_003",
        "municipality_name": "Gainesville",
        "county":            "Alachua",
        "project_name":      "Hogtown Creek Stormwater Retrofit",
        "partner_name":      "Coastal Restoration Partners LLC",
        "estimated_cost_usd": 2_800_000,
        "agreement_text": (
            "DRAFT P3 PARTNERSHIP AGREEMENT — Hogtown Creek Stormwater Retrofit\n\n"
            "Parties: City of Gainesville (Municipality), C-HAWQ (Nonprofit Facilitator), "
            "Coastal Restoration Partners LLC (Implementation Partner).\n\n"
            "Section 1 — Scope: Coastal Restoration Partners shall design, permit, "
            "construct, and warrant the stormwater outfall retrofits and riparian "
            "buffer restoration described in Exhibit A.\n\n"
            "Section 2 — Capital Commitment: Coastal Restoration Partners shall "
            "contribute $800,000 in private capital prior to commencement of "
            "construction. Disbursement schedule is tied to permit issuance milestones.\n\n"
            "Section 3 — Grant Administration: The City is the applicant of record for "
            "the FDEP Section 319 grant ($1,200,000). Grant funds shall be disbursed "
            "to Coastal Restoration Partners upon completion of each construction "
            "phase milestone.\n\n"
            "Section 4 — Risk Allocation: If any required permit is denied, Coastal "
            "Restoration Partners may terminate this agreement with 30 days notice. "
            "The City shall not be liable for permit denial costs incurred by the "
            "Implementation Partner.\n\n"
            "Section 5 — Sovereign Submerged Lands: The City shall apply for any "
            "required FDEP Board of Trustees consent of use for outfall #3. Timeline "
            "delays attributable to sovereignty land lease processing shall not "
            "constitute breach by either party.\n\n"
            "Section 6 — Warranty: Coastal Restoration Partners warrants all "
            "construction work for 2 years from substantial completion.\n\n"
            "Section 7 — Intellectual Property: All project documentation, designs, "
            "and monitoring data produced under this agreement become property of "
            "the City upon project close.\n\n"
            "Section 8 — Termination: Either party may terminate for convenience "
            "with 60 days written notice. Coastal Restoration Partners is entitled "
            "to compensation for work completed through termination date only.\n\n"
            "Section 9 — F.S. 255.065: The parties acknowledge this agreement is "
            "structured consistent with Florida's Public-Private Partnership statute."
        ),
    }),

    # S10-1: Project case study — after project completion
    ("S10-1", {
        "contact_id":        "ghl_test_003",
        "municipality_name": "Gainesville",
        "county":            "Alachua",
        "project_name":      "Hogtown Creek Stormwater Retrofit",
        "project_type":      "stormwater_retrofit",
        "project_scope": (
            "Retrofitted 3 stormwater outfalls and restored 4.2 acres of riparian "
            "buffer along 1.8 miles of Hogtown Creek."
        ),
        "key_outcomes": (
            "Nitrogen loading reduced by 44% (from 1,200 lbs/year to 675 lbs/year). "
            "FDEP removed Hogtown Creek from the impaired waters list 18 months "
            "post-construction. Three algae bloom events in 2024 vs. zero in 2028."
        ),
        "total_cost_usd":    2_850_000,
        "funding_sources":   "FDEP Section 319: $1,200,000 | P3 capital: $800,000 | City fund: $850,000",
        "champion_name":     "Alex Rivera",
        "champion_title":    "Public Works Director, City of Gainesville",
        "project_duration":  "22 months (construction start May 2027, substantial completion March 2029)",
        "champion_motivation": (
            "Alex had been fighting for Hogtown Creek funding for six years before "
            "C-HAWQ arrived. The FDEP impaired status was a source of personal frustration."
        ),
        "community_impact": (
            "1,200 residents adjacent to the creek corridor; 3 community cleanup events "
            "during construction with 280 volunteers; dedicated trail access restored "
            "to the riparian buffer."
        ),
        "challenges_overcome": (
            "Sovereignty land lease for outfall #3 took 14 months instead of 9. "
            "C-HAWQ restructured phasing so outfalls #1 and #2 could proceed while "
            "the lease was processed."
        ),
        "champion_quotes":   '"Six years I tried to get this funded. C-HAWQ did it in 14 months." — Alex Rivera',
    }),

    # S10-2: Referral outreach research — Alex refers Newberry City Manager
    ("S10-2", {
        "contact_id":        "ghl_test_004",
        "municipality_name": "Newberry",
        "champion_name":     "Alex Rivera",
        "champion_municipality": "Gainesville",
        "referral_name":     "Marcus Webb",
        "referral_title":    "City Manager",
        "referral_municipality": "City of Newberry, Florida",
        "referral_known_info": (
            "Alex mentioned Marcus has been trying to address water quality issues "
            "in the Newberry canal system for two years. City is small (~5,000 "
            "residents) but motivated. Marcus attended the Hogtown Creek community "
            "event and spoke with Logan Davies."
        ),
        "completed_project_name": "Hogtown Creek Stormwater Retrofit",
        "completed_project_outcome": "44% reduction in nitrogen loading; Hogtown Creek removed from FDEP impaired waters list",
    }),
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

@dataclass
class Result:
    research_type: str
    passed: bool
    elapsed: float
    input_tokens: int
    output_tokens: int
    binder_chars: int
    docx_bytes: int
    brief: object | None
    error: str | None


def run_one(research_type: str, contact: dict, web_search: bool = False) -> Result:
    from services.research_agent.runner import retrieve_binder_context, load_prompt
    from services.research_agent.drive_sync import render_docx

    yaml_path = _BACKEND / "prompts" / "research_agent" / research_type / "v1.yaml"
    cfg = load_prompt(yaml_path)
    binder_text = retrieve_binder_context(cfg)

    t0 = time.time()
    try:
        agent = ResearchAgent(research_type)
        brief, meta = agent.run(contact, no_web_search=not web_search, verbose=False)

        docx = render_docx(brief)
        if not docx:
            raise RuntimeError("render_docx returned empty bytes")

        return Result(
            research_type=research_type,
            passed=True,
            elapsed=meta["elapsed_sec"],
            input_tokens=meta["input_tokens"],
            output_tokens=meta["output_tokens"],
            binder_chars=len(binder_text),
            docx_bytes=len(docx),
            brief=brief,
            error=None,
        )
    except Exception as exc:
        return Result(
            research_type=research_type,
            passed=False,
            elapsed=round(time.time() - t0, 1),
            input_tokens=0,
            output_tokens=0,
            binder_chars=len(binder_text),
            docx_bytes=0,
            brief=None,
            error=str(exc),
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stop-on-fail", action="store_true",
                    help="Halt on first failure instead of running all tests")
    ap.add_argument("--types", nargs="+", metavar="TYPE",
                    help="Run only these research types (e.g. LOBBY-1 S1-4)")
    ap.add_argument("--drive", action="store_true",
                    help="Upload each passing brief to Google Drive")
    ap.add_argument("--drive-folder",
                    help="Drive folder ID (defaults to drive_sync.DEFAULT_FOLDER_ID)")
    ap.add_argument("--save-dir", metavar="DIR",
                    help="Save each passing brief as JSON in this local directory")
    ap.add_argument("--web-search", action="store_true",
                    help="Enable web_search and web_fetch tools (slower, billed)")
    args = ap.parse_args()

    if "ANTHROPIC_API_KEY" not in os.environ:
        print("ERROR: ANTHROPIC_API_KEY not set.")
        sys.exit(1)

    save_dir = Path(args.save_dir) if args.save_dir else None
    if save_dir:
        save_dir.mkdir(parents=True, exist_ok=True)

    tests = [(rt, c) for rt, c in TESTS if not args.types or rt in args.types]

    print(f"\n{'TYPE':<12} {'RESULT':<8} {'TIME':>6}  {'IN':>6}  {'OUT':>5}  {'BINDER':>7}  {'DOCX':>6}  OUTPUT")
    print("─" * 90)

    results: list[Result] = []
    for research_type, contact in tests:
        print(f"{research_type:<12} {'running':<8}", end="", flush=True)
        r = run_one(research_type, contact, web_search=args.web_search)
        results.append(r)

        status = "PASS" if r.passed else "FAIL"
        output_note = ""

        if r.passed and r.brief:
            if save_dir:
                from services.research_agent.drive_sync import filename_for
                out_path = save_dir / filename_for(r.brief, "json")
                out_path.write_text(r.brief.model_dump_json(indent=2), encoding="utf-8")
                output_note = f"  saved → {out_path.name}"

            if args.drive:
                try:
                    from services.research_agent.drive_sync import upload_brief, DEFAULT_FOLDER_ID
                    folder = args.drive_folder or DEFAULT_FOLDER_ID
                    files = upload_brief(r.brief, folder_id=folder)
                    links = "  ".join(f["webViewLink"] for f in files.values())
                    output_note = f"  drive → {links}"
                except Exception as exc:
                    output_note = f"  drive FAILED: {str(exc)[:40]}"

        err_snippet = f"  {r.error[:50]}..." if r.error else output_note
        docx_str = f"{r.docx_bytes // 1024}K" if r.docx_bytes else "—"
        print(
            f"\r{r.research_type:<12} {status:<8} "
            f"{r.elapsed:>5.1f}s  {r.input_tokens:>6,}  {r.output_tokens:>5,}  "
            f"{r.binder_chars:>6,}c  {docx_str:>5}{err_snippet}"
        )
        if not r.passed and args.stop_on_fail:
            break

    passed = sum(1 for r in results if r.passed)
    total = len(results)
    print("─" * 90)
    print(f"\n{passed}/{total} passed", end="")
    if passed == total:
        print("  ✓ all green")
    else:
        print()
        for r in results:
            if not r.passed:
                print(f"\n  {r.research_type} error:\n  {r.error}")
    print()

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
