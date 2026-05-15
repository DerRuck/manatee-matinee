"""
Smoke-test all 26 research agent types in pipeline order.

Each test uses --no-web-search (training-data only) to run fast and free.
A pass means: inputs resolved, binder retrieved, Claude responded, schema
validated, and render_docx produced a non-empty Word document.
Web search correctness is tested separately in golden evals.

The test fixture follows one real project thread throughout —
Rookery Bay NERR / Marco Shores Lake & ICW Dredging P3 — so each step's
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

    # Lobbyist registration check — must clear before outreach to Rookery Bay NERR
    ("LOBBY-1", {
        "contact_id":        "ghl_rookery_001",
        "municipality_name": "Naples",
        "jurisdiction_name": "Collier County",
        "jurisdiction_type": "county",
    }),

    # Municipality background — research Rookery Bay NERR / Collier County before Step 1
    ("PW-3", {
        "contact_id":        "ghl_rookery_001",
        "municipality_name": "Naples",
        "county":            "Collier",
        "state":             "FL",
        "contact_name":      "Jared",
        "contact_title":     "Director, Rookery Bay NERR",
    }),

    # =========================================================================
    # PHASE 1 — DISCOVERY (Steps 1–4)
    # =========================================================================

    # S1-4: Internet research on Jared (Director, Rookery Bay NERR) before first contact
    ("S1-4", {
        "contact_id":        "ghl_rookery_001",
        "municipality_name": "Naples",
        "contact_name":      "Jared",
        "contact_title":     "Director",
        "organization":      "Rookery Bay National Estuarine Research Reserve",
    }),

    # S1-2: LinkedIn connection prep for Jared
    ("S1-2", {
        "contact_id":        "ghl_rookery_001",
        "municipality_name": "Naples",
        "contact_name":      "Jared",
        "contact_title":     "Director",
        "organization":      "Rookery Bay National Estuarine Research Reserve",
    }),

    # PW-1: Conference attendee research — mapping leads before FSBPA annual event
    ("PW-1", {
        "contact_id":        "ghl_conference_001",
        "municipality_name": None,
        "conference_name":   "FSBPA 2026 Annual Conference",
        "conference_date":   "2026-09-15",
        "location":          "Florida",
    }),

    # S3-PREP: Pre-meeting research package — intake meeting with Jared, Marissa, Rachel
    ("S3-PREP", {
        "contact_id":        "ghl_rookery_001",
        "municipality_name": "Naples",
        "county":            "Collier",
        "contact_name":      "Jared",
        "contact_title":     "Director, Rookery Bay NERR",
        "meeting_date":      "2026-01-08",
        "project_hint": (
            "Rookery Bay NERR manages 110,000 acres; key water quality challenges at "
            "Marco Shores Lake (PFAS, snook passage, crocodile infertility) and Shell "
            "Island Road (hydro sheet flow, gopher tortoise). USACE ICW dredge "
            "approaching. Also meeting Marissa Figueroa (Coastal Training Program "
            "Coordinator) and Rachel Schneberger (Community Monitoring Specialist)."
        ),
    }),

    # S3-3: Commission/board meeting prep — observe before intake
    ("S3-3", {
        "contact_id":        "ghl_rookery_001",
        "municipality_name": "Naples",
        "meeting_date":      "2026-01-07",
        "meeting_goal":      "observe",
        "project_status": (
            "Pre-Step-3 — Jared agreed to intake meeting after Logan met Rookery Bay "
            "staff at Swap Fest 2025. No formal proposal yet."
        ),
    }),

    # S4-DECK: Deck research — visual and data assets for C-HAWQ Step 4 presentation
    ("S4-DECK", {
        "contact_id":        "ghl_rookery_001",
        "municipality_name": "Naples",
        "project_focus": (
            "Marco Shores Lake fish passage and water quality restoration; "
            "Shell Island Road hydrological sheet flow; USACE ICW dredging "
            "beneficial reuse via P3 administration"
        ),
        "problem_areas": (
            "Marco Shores Lake: undersized box culvert blocking snook passage, "
            "PFAS and heavy metals suspected from airport runoff and golf course, "
            "crocodile infertility documented for 25 years with zero surviving young. "
            "Shell Island Road: disrupted sheet flow into Rookery Bay estuary, "
            "gopher tortoise habitat at risk from standard fencing. "
            "ICW: USACE planning to dump ~150,000 CY dredge spoil on Key Island "
            "without scientific oversight or restoration value."
        ),
        "champion_priorities": (
            "Jared wants rigorous science behind every solution and state/federal "
            "sign-off. Marissa focused on nature-based solutions and policy translation. "
            "Rachel needs improved citizen science methodology and permittable protocols. "
            "All three want projects that generate publishable data and protect reserve "
            "designation."
        ),
    }),

    # S4-LETTER: Champion briefing letter — sent after January 8 intake meeting
    ("S4-LETTER", {
        "contact_id":        "ghl_rookery_001",
        "municipality_name": "Naples",
        "county":            "Collier",
        "champion_name":     "Jared",
        "champion_title":    "Director, Rookery Bay National Estuarine Research Reserve",
        "meeting_date":      "2026-01-08",
        "project_description": (
            "Three interrelated projects at Rookery Bay NERR: (1) Marco Shores Lake "
            "fish passage — replace undersized box culvert and conduct PFAS, heavy "
            "metals, and fish stock surveys; (2) Shell Island Road hydrological "
            "restoration — restore sheet flow while protecting gopher tortoise habitat; "
            "(3) USACE ICW dredge beneficial reuse — C-HAWQ as P3 administrator to "
            "direct ~150,000 CY of dredge spoil toward science-backed habitat "
            "restoration rather than unmanaged Key Island placement."
        ),
        "key_points_discussed": (
            "Jared described crocodile infertility at Marco Shores Lake — eggs hatched "
            "for 25 years but zero surviving young, suspected PFAS/contaminant link. "
            "Rachel's citizen science monitoring is hampered by vandalism of monitoring "
            "lines and non-permittable methods. Marissa highlighted the Mangrove Coast "
            "Collaborative and interest in nature-based solutions. Bonefish Tarpon Trust "
            "and MacDonald Engineering are already engaged on permitting. USACE dredge "
            "timeline urgent — decision point approaching end of June."
        ),
        "agreed_next_steps": (
            "C-HAWQ to prepare a formal letter and slide deck describing partnership "
            "structure and financial offer. "
            "Jared to share project plans and images from Shell Island Road file. "
            "C-HAWQ to send AI-generated podcast summary of the Chuck Courtney "
            "limnological dissertation on Marco Shores Lake for community use. "
            "Schedule follow-up meeting before end of February."
        ),
        "champion_motivation": (
            "Jared has a backlog of critical projects with no funding mechanism. "
            "He expressed excitement about the fiscal boost C-HAWQ can provide but "
            "needs to verify C-HAWQ's bona fides before committing to anything. "
            "The USACE timeline creates urgency."
        ),
        "funding_interest": (
            "NOAA NERR funding, FL Office of Resilience and Coastal Protection grants, "
            "USACE beneficial reuse funds, Bonefish Tarpon Trust partnership capital"
        ),
    }),

    # =========================================================================
    # PHASE 2 — DEVELOPMENT (Steps 5–6)
    # =========================================================================

    # S5-1: Internal presentation prep — briefing Rookery Bay staff and Friends of Rookery
    ("S5-1", {
        "contact_id":        "ghl_rookery_001",
        "municipality_name": "Naples",
        "county":            "Collier",
        "presentation_date": "2026-03-25",
        "audience_roles": (
            "Jared (Director), Marissa Figueroa (Coastal Training Program Coordinator), "
            "Rachel Schneberger (Community Monitoring Specialist), "
            "Friends of Rookery Bay executive staff (fiscal executor for reserve investments)"
        ),
        "project_description": (
            "Three-project C-HAWQ partnership: Marco Shores Lake fish passage and "
            "contaminant survey; Shell Island Road hydrological restoration; USACE "
            "ICW dredge beneficial reuse P3. Total commitment $6–9M, 18-month timeline, "
            "proposed start September 2027."
        ),
        "project_type": "habitat_restoration_and_beneficial_reuse",
        "champion_concerns": (
            "Jared needs to brief Alex (statewide FL Office of Resilience and Coastal "
            "Protection) on who C-HAWQ is before any formal commitment. State has to "
            "understand and approve all messaging. Friends of Rookery Bay staff may "
            "ask about administrative liability for bonding a USACE project."
        ),
    }),

    # S5-2: Post-internal meeting debrief — notes from March 25 Friends of Rookery meeting
    ("S5-2", {
        "contact_id":        "ghl_rookery_001",
        "municipality_name": "Naples",
        "presentation_date": "2026-03-25",
        "champion_name":     "Jared",
        "project_description": "Rookery Bay NERR Multi-Project Partnership",
        "meeting_notes": (
            "Attended by: Jared (Director, Rookery Bay NERR), Emily Begin and Chris "
            "Ripley (C-HAWQ), Friends of Rookery Bay executive staff. "
            "Emily provided C-HAWQ organizational overview — three pillars: research, "
            "technical assistance grants, public education. "
            "Jared was engaged on the dredge beneficial reuse concept but repeated "
            "need to verify C-HAWQ credentials before state conversations. "
            "Friends of Rookery Bay staff asked whether C-HAWQ could administer USACE "
            "funds under a P3 structure — confirmed yes, with precedent from Everglades "
            "City (C-HAWQ local attorney handled that transaction). "
            "Marissa raised concern about thin-layer placement in mangroves — noted "
            "it's never been done and SFWMD only just piloted it near Turkey Point. "
            "Rachel did not attend but sent notes: monitoring methodology upgrade and "
            "citizen science permittability remain her top priorities. "
            "Key blocker: USACE timeline. Army Corps wants decision by end of June; "
            "Jared is moving deliberately and does not want to rush the science."
        ),
    }),

    # S6-1: Grant opportunity research — funding identification for Rookery Bay package
    ("S6-1", {
        "contact_id":        "ghl_rookery_001",
        "municipality_name": "Naples",
        "county":            "Collier",
        "project_type":      "coastal_habitat_restoration_and_beneficial_reuse",
        "estimated_cost_usd": 9_000_000,
        "project_overview": (
            "Three-project coastal restoration package at Rookery Bay NERR: "
            "(1) Marco Shores Lake box culvert replacement for fish passage + PFAS/"
            "metals/fish stock surveys (~$2M); "
            "(2) Shell Island Road hydrological sheet flow restoration with gopher "
            "tortoise protection infrastructure (~$2.63M boardwalk); "
            "(3) USACE ICW dredge beneficial reuse — C-HAWQ P3 administration of "
            "~150,000 CY dredge spoil toward mangrove thin-layer placement and "
            "habitat reconstruction (~$4.37M, partially offset by USACE funds). "
            "Outstanding Florida Waters designation. NERR federal/state sublease."
        ),
        "p3_intent": "yes",
    }),

    # S6-2: Project narrative draft — commission and grant writing assets
    ("S6-2", {
        "contact_id":        "ghl_rookery_001",
        "municipality_name": "Naples",
        "county":            "Collier",
        "project_name":      "Rookery Bay NERR Coastal Restoration Partnership",
        "project_type":      "coastal_habitat_restoration_and_beneficial_reuse",
        "problem_statement": (
            "Rookery Bay NERR's 110,000-acre estuary faces compounding stressors: "
            "an undersized box culvert at Marco Shores Lake blocks snook fish passage "
            "and traps PFAS-contaminated water linked to suspected crocodile infertility "
            "(zero surviving young over 25 years); disrupted sheet flow along Shell "
            "Island Road degrades tidal exchange into the estuary; and an Army Corps "
            "dredge project threatens to deposit ~150,000 CY of spoil on Key Island "
            "without scientific oversight, risking turbidity damage to the reserve."
        ),
        "estimated_cost_usd": 9_000_000,
        "project_description": (
            "C-HAWQ will partner with Rookery Bay NERR as P3 administrator to: "
            "replace the Marco Shores Lake box culvert and conduct comprehensive "
            "contaminant and fish surveys; restore hydrological sheet flow along "
            "Shell Island Road with gopher tortoise infrastructure; and administer "
            "USACE dredge funds to direct beneficial reuse toward science-backed "
            "habitat reconstruction rather than unmanaged spoil placement."
        ),
        "key_data": (
            "Marco Shores Lake: crocodile eggs hatching for 25 consecutive years, "
            "zero surviving young — suspected PFAS link. Snook trapped in lake since "
            "culvert installed. Airport, golf course, and road runoff identified as "
            "contamination vectors. "
            "Shell Island Road: standard fencing not permittable (blocks swale drainage). "
            "USACE dredge: TigerTail Beach baseline placement would cost $2.79M more "
            "than C-HAWQ Scenario B beneficial reuse placement."
        ),
        "funding_approach": (
            "NOAA NERR program + FL Office of Resilience and Coastal Protection: ~$6M | "
            "C-HAWQ private match (P3 capital + bond): ~$3M | "
            "USACE beneficial reuse funds (offset): TBD"
        ),
        "anticipated_timeline": "Construction/implementation start September 2027, 18-month program",
        "community_benefit": (
            "Restored fish passage and water quality for 110,000-acre estuary; "
            "resolution of 25-year crocodile reproductive failure; first documented "
            "thin-layer mangrove placement protocol in Florida; protected gopher "
            "tortoise corridor along Shell Island Road"
        ),
    }),

    # S6-3: Commission/board presentation prep — Collier County BCC vote on partnership
    ("S6-3", {
        "contact_id":        "ghl_rookery_001",
        "municipality_name": "Naples",
        "county":            "Collier",
        "project_name":      "Rookery Bay NERR Coastal Restoration Partnership",
        "presentation_date": "2026-09-15",
        "project_type":      "coastal_habitat_restoration_and_beneficial_reuse",
        "estimated_cost_usd": 9_000_000,
        "project_package_contents": (
            "Engineering feasibility study (MacDonald Company), USACE beneficial reuse "
            "cost comparison (C-HAWQ Scenario B vs. TigerTail Beach baseline), "
            "Marco Shores Lake survey scope, Shell Island Road boardwalk design "
            "($2.63M estimate), P3 term sheet, state sublease analysis"
        ),
        "commission_composition": (
            "Collier County Board of County Commissioners (5 members). "
            "FDEP/FL Office of Resilience and Coastal Protection (Alex, statewide "
            "director) must approve state messaging before formal presentation. "
            "Friends of Rookery Bay board (fiscal executor) — supportive but cautious "
            "on bonding liability. Bonefish Tarpon Trust — engaged partner, already "
            "funding MacDonald engineering work."
        ),
        "champion_read": (
            "Jared is the internal champion but needs state sign-off from Alex first. "
            "Main risk: USACE timeline pressure — Corps wants a decision by end of "
            "June, Jared wants deliberate science process. Bonefish Tarpon Trust "
            "relationship is key to credibility with commissioners."
        ),
    }),

    # =========================================================================
    # PHASE 3 — MOBILIZATION (Steps 7–8)
    # =========================================================================

    # S7-PLAN: Community event plan — Rookery Bay community science day at Marco Shores Lake
    ("S7-PLAN", {
        "contact_id":        "ghl_rookery_001",
        "municipality_name": "Naples",
        "county":            "Collier",
        "project_name":      "Rookery Bay NERR Coastal Restoration Partnership",
        "event_type":        "community_science_day",
        "target_event_date": "2026-08-15",
        "project_description": (
            "Multi-project coastal restoration partnership at Rookery Bay NERR: "
            "Marco Shores Lake fish passage and contaminant survey, Shell Island Road "
            "hydrological restoration, and USACE ICW dredge beneficial reuse."
        ),
        "champion_name":     "Jared",
        "champion_role":     "Director, Rookery Bay NERR",
        "existing_partners": (
            "Bonefish Tarpon Trust (engaged, co-funding MacDonald engineering). "
            "UF/IFAS Collier County Extension (Marissa's academic network). "
            "Marco Shores community HOA (Rachel's citizen-science base). "
            "Friends of Rookery Bay nonprofit."
        ),
        "venue_options":     "Rookery Bay NERR Environmental Learning Center, or Marco Shores Lake shoreline access",
        "target_attendance": 80,
    }),

    # S7-1: Post-event debrief — Rookery Bay community science day
    ("S7-1", {
        "contact_id":        "ghl_rookery_001",
        "municipality_name": "Naples",
        "event_name":        "Rookery Bay Community Science Day",
        "event_date":        "2026-08-15",
        "project_description": "Rookery Bay NERR Coastal Restoration Partnership",
        "event_notes": (
            "Approximately 95 people attended at the NERR Environmental Learning Center. "
            "Water quality demonstration station drew the most engagement — the before/"
            "after jar comparison of lake water near the culvert vs. estuary water was "
            "the most-photographed element. "
            "Rachel's citizen science methodology demonstration drew ~25 people; several "
            "Marco Shores residents volunteered to join the monitoring program. "
            "Notable conversations: "
            "Two Collier County BCC staff attended and collected the project fact sheet. "
            "A Bonefish Tarpon Trust board member said the crocodile infertility story "
            "would be very compelling for their donor base. "
            "Local fishing club president asked specifically about snook restoration "
            "timeline — left his card. "
            "WGCU public radio sent a reporter who interviewed Rachel about citizen science. "
            "52 sign-in entries collected. Sign-in sheet included commissioner district "
            "breakdowns. "
            "Logistics: parking was constrained at the Learning Center — coordinate "
            "overflow with NERR staff next time."
        ),
        "new_contacts": (
            "Collier County BCC staff (2) — collected project materials, no name given. "
            "Bonefish Tarpon Trust board member — interested in crocodile infertility "
            "story for donor outreach. "
            "Naples fishing club president — wants snook passage timeline. "
            "WGCU public radio reporter — interviewed Rachel on citizen science."
        ),
    }),

    # S8-1: Political landscape mapping — pre-vote commissioner profiles
    ("S8-1", {
        "contact_id":        "ghl_rookery_001",
        "municipality_name": "Naples",
        "county":            "Collier",
        "project_name":      "Rookery Bay NERR Coastal Restoration Partnership",
        "project_type":      "coastal_habitat_restoration_and_beneficial_reuse",
        "estimated_cost_usd": 9_000_000,
        "commissioner_assessments": (
            "Collier County BCC — 5 members; environmental projects historically get "
            "mixed reception due to development-friendly commission composition. "
            "FDEP Alex (FL Office of Resilience and Coastal Protection): statewide "
            "director must approve project before BCC presentation — key gatekeeper. "
            "Friends of Rookery Bay board: supportive but cautious on bonding liability. "
            "USACE Jacksonville District project manager: wants cost savings, open to "
            "P3 if C-HAWQ provides engineering specs and timeline."
        ),
        "city_staff_notes": (
            "Jared (Director): champion but constrained by state chain of command. "
            "Marissa (CTP Coordinator): science and policy translator, key internal ally. "
            "Rachel (Community Monitoring): citizen science lead, needs permittable "
            "methodology — strong field credibility with residents."
        ),
        "community_support": (
            "Bonefish Tarpon Trust, UF/IFAS Extension, Marco Shores community HOA, "
            "Friends of Rookery Bay, Naples fishing club"
        ),
        "known_opposition": (
            "USACE Jacksonville District timeline pressure — Corps wants decision by "
            "end of June, may proceed without reserve input if delayed."
        ),
        "vote_timeframe": "Collier County BCC meeting September 15, 2026 — approximately 6 weeks out",
    }),

    # S8-2: Community support letter — for Bonefish Tarpon Trust and UF/IFAS to sign
    ("S8-2", {
        "contact_id":        "ghl_rookery_001",
        "municipality_name": "Naples",
        "county":            "Collier",
        "project_name":      "Rookery Bay NERR Coastal Restoration Partnership",
        "project_summary": (
            "A public-private partnership between Rookery Bay NERR, C-HAWQ, and "
            "partner organizations to restore Marco Shores Lake fish passage, repair "
            "hydrological flow along Shell Island Road, and administer USACE dredge "
            "funds toward science-backed beneficial reuse — total investment $9M, "
            "protecting 110,000 acres of Outstanding Florida Waters."
        ),
        "community_benefits": (
            "Restored snook passage between Marco Shores Lake and Rookery Bay estuary; "
            "investigation and remediation of suspected PFAS contamination linked to "
            "25-year crocodile reproductive failure; first documented thin-layer "
            "mangrove placement protocol in Florida; protected gopher tortoise habitat "
            "along Shell Island Road"
        ),
        "commission_body_name": "Collier County Board of County Commissioners",
        "signature_line_count": 5,
    }),

    # S8-3: Politician-friendly briefing — one-on-one with FDEP Alex (state gatekeeper)
    ("S8-3", {
        "contact_id":        "ghl_rookery_001",
        "municipality_name": "Naples",
        "county":            "Collier",
        "project_name":      "Rookery Bay NERR Coastal Restoration Partnership",
        "official_name":     "Alex",
        "official_title":    "Statewide Director, FL Office of Resilience and Coastal Protection",
        "tenure":            "Statewide director overseeing all NERR sites and coastal resilience programs",
        "known_priorities": (
            "State-sanctioned science, interagency coordination, no surprises from NERR "
            "staff on federal projects, protecting NERR designation"
        ),
        "political_style": (
            "Cautious, chain-of-command oriented. Jared confirmed Alex will ask "
            "'Who is C-HAWQ?' before anything moves forward. Needs full organizational "
            "brief and bona fides documentation."
        ),
        "known_concerns": (
            "Any private organization getting involved in a USACE project at a state-"
            "managed NERR site raises flags — liability, optics, federal-state-private "
            "chain of authority. Needs to understand P3 legal structure and Everglades "
            "City precedent before blessing outreach."
        ),
        "project_backstory": (
            "C-HAWQ met Rookery Bay staff at Swap Fest December 2025. Intake meeting "
            "January 8, 2026. Sent organizational letter and slide deck February 2026. "
            "March 25 meeting with Friends of Rookery Bay and Jared confirmed interest "
            "contingent on state approval. USACE timeline creating urgency."
        ),
        "project_description": (
            "C-HAWQ proposes to serve as P3 administrator for Rookery Bay NERR, "
            "directing USACE dredge funds toward beneficial reuse while funding "
            "Marco Shores Lake fish passage and Shell Island Road hydrological "
            "restoration from private capital."
        ),
        "environmental_problem": (
            "Marco Shores Lake: blocked fish passage, suspected PFAS contamination, "
            "25-year crocodile reproductive failure. Shell Island Road: disrupted "
            "tidal hydrology. ICW: unmanaged dredge spoil placement threatening estuary."
        ),
        "exploration_grant_amount": 250_000,
        "municipality_ask": (
            "State authorization for Rookery Bay NERR to enter a P3 agreement with "
            "C-HAWQ as project administrator under F.S. § 255.065, consistent with "
            "Everglades City precedent"
        ),
    }),

    # =========================================================================
    # PHASE 4 — EXECUTION (Steps 9–10)
    # =========================================================================

    # S9-1: Kickoff deck — first formal meeting after P3 agreement signed
    ("S9-1", {
        "contact_id":        "ghl_rookery_001",
        "municipality_name": "Naples",
        "project_name":      "Rookery Bay NERR Coastal Restoration Partnership",
        "kickoff_date":      "2026-11-01",
        "municipality_attendees": (
            "Jared (Director, Rookery Bay NERR), Marissa Figueroa (Coastal Training "
            "Program Coordinator), Rachel Schneberger (Community Monitoring Specialist), "
            "Friends of Rookery Bay executive director"
        ),
        "chawq_attendees":   "Chris Ripley, Emily Begin",
        "gc_partner_name":   "MacDonald Company (engineering and design)",
        "grant_admin_attendee": (
            "NOAA NERR Program Officer; FL Office of Resilience and Coastal Protection rep"
        ),
        "project_description": (
            "Marco Shores Lake fish passage (box culvert replacement + PFAS/metals/"
            "fish surveys), Shell Island Road hydrological restoration ($2.63M "
            "boardwalk), and USACE ICW dredge beneficial reuse P3 administration."
        ),
        "funding_structure": (
            "NOAA + FL Office of Resilience and Coastal Protection (public): $6,000,000 | "
            "C-HAWQ private match (P3 capital + bond): $3,000,000 | "
            "USACE beneficial reuse offset: TBD | Total: $9,000,000+"
        ),
        "milestone_schedule": (
            "Permitting and USACE coordination: Nov 2026–Jun 2027 | "
            "Marco Shores Lake surveys: Jan–Apr 2027 | "
            "Shell Island Road construction: May–Dec 2027 | "
            "Box culvert installation: Sep 2027 | "
            "Dredge beneficial reuse: Sep 2027–Mar 2028 | "
            "Monitoring: Ongoing"
        ),
        "open_items": (
            "State Lands sublease amendment for P3 administrator role not yet filed. "
            "USACE cooperative agreement terms under negotiation. "
            "Bonefish Tarpon Trust capital commitment letter pending."
        ),
    }),

    # S9-2: Media & reporter research — for partnership announcement
    ("S9-2", {
        "contact_id":        "ghl_rookery_001",
        "municipality_name": "Naples",
        "county":            "Collier",
        "announcement_type": "partnership_announcement",
        "project_summary": (
            "Rookery Bay NERR and C-HAWQ have signed a public-private partnership to "
            "restore Marco Shores Lake fish passage, repair Sheet Island Road "
            "hydrological flow, and administer USACE dredge funds toward science-"
            "backed beneficial reuse — protecting 110,000 acres of Outstanding "
            "Florida Waters and investigating 25 years of crocodile reproductive failure."
        ),
        "champion_name":     "Jared",
        "champion_title":    "Director, Rookery Bay National Estuarine Research Reserve",
        "chawq_spokesperson": "Chris Ripley, Executive Director, C-HAWQ",
        "project_type":      "coastal_habitat_restoration_and_beneficial_reuse",
    }),

    # S9-3: Grant compliance checklist — after NOAA NERR award
    ("S9-3", {
        "contact_id":        "ghl_rookery_001",
        "municipality_name": "Naples",
        "project_name":      "Rookery Bay NERR Coastal Restoration Partnership",
        "grant_program_name": "NOAA National Estuarine Research Reserve Program",
        "award_amount_usd":  3_500_000,
        "grant_period_start": "2026-10-01",
        "grant_period_end":  "2030-09-30",
        "administering_agency": "National Oceanic and Atmospheric Administration (NOAA)",
        "project_scope": (
            "Marco Shores Lake fish passage restoration, water quality surveys, and "
            "Shell Island Road hydrological restoration at Rookery Bay NERR, "
            "Collier County, Florida"
        ),
        "p3_partners": "C-HAWQ (P3 administrator), MacDonald Company (engineering)",
    }),

    # S9-4: P3 proposal drafting — RFP for construction and restoration partner
    ("S9-4", {
        "contact_id":        "ghl_rookery_001",
        "municipality_name": "Naples",
        "county":            "Collier",
        "project_name":      "Rookery Bay NERR Coastal Restoration Partnership",
        "project_description": (
            "Design, permit, and construct: (1) box culvert replacement at Marco "
            "Shores Lake for snook fish passage; (2) Shell Island Road $2.63M "
            "boardwalk and hydrological sheet flow restoration with gopher tortoise "
            "infrastructure; (3) thin-layer dredge spoil placement in mangrove "
            "habitat using ~150,000 CY from USACE ICW project. Includes 3-year "
            "post-construction monitoring with UF/IFAS academic partners."
        ),
        "estimated_cost_usd": 9_000_000,
        "procurement_path":  "solicited_rfp",
        "funding_structure": (
            "NOAA + FL ORCP public grants: $6,000,000 | "
            "C-HAWQ private match: $3,000,000 | "
            "USACE dredge funds (offset): TBD"
        ),
        "complexity_factors": (
            "Work within Outstanding Florida Waters and NERR federal designation. "
            "State Lands sublease constraints — all activity subject to state review. "
            "Thin-layer mangrove placement is undocumented in Florida — contractor "
            "must commit to scientific monitoring and methodology publication. "
            "Gopher tortoise relocation or fencing solution must be pre-approved by "
            "FWC. USACE schedule coordination required. "
            "Academic monitoring (UF/IFAS) for 3 years post-construction required."
        ),
        "project_type": "coastal_habitat_restoration_and_beneficial_reuse",
    }),

    # S9-5: Partnership agreement summary — reviewing draft P3 agreement with NERR
    ("S9-5", {
        "contact_id":        "ghl_rookery_001",
        "municipality_name": "Naples",
        "county":            "Collier",
        "project_name":      "Rookery Bay NERR Coastal Restoration Partnership",
        "partner_name":      "Rookery Bay National Estuarine Research Reserve / Friends of Rookery Bay",
        "estimated_cost_usd": 9_000_000,
        "agreement_text": (
            "DRAFT P3 PARTNERSHIP AGREEMENT — Rookery Bay NERR Coastal Restoration\n\n"
            "Parties: Rookery Bay National Estuarine Research Reserve (Reserve, managed "
            "under State Lands sublease), Friends of Rookery Bay (Fiscal Executor), "
            "C-HAWQ (Nonprofit P3 Administrator).\n\n"
            "Section 1 — Scope: C-HAWQ shall serve as P3 administrator for three "
            "restoration projects: Marco Shores Lake fish passage and contaminant "
            "survey; Shell Island Road hydrological restoration; and USACE ICW dredge "
            "beneficial reuse placement. All work described in Exhibit A.\n\n"
            "Section 2 — Capital Commitment: C-HAWQ shall provide $3,000,000 in "
            "private match capital and post a performance bond equal to 2x the C-HAWQ "
            "capital commitment prior to commencement. Bond releases upon project "
            "close and NOAA acceptance of final monitoring report.\n\n"
            "Section 3 — Grant Administration: Friends of Rookery Bay is the applicant "
            "of record for all NOAA and FL ORCP grants. C-HAWQ administers funds under "
            "subgrant agreement subject to audit by NOAA and the FL Auditor General.\n\n"
            "Section 4 — USACE Coordination: C-HAWQ shall negotiate with USACE "
            "Jacksonville District for administration of dredge beneficial reuse funds "
            "consistent with the Everglades City precedent. Reserve retains veto "
            "authority over spoil placement locations.\n\n"
            "Section 5 — State Lands: All work within the State Lands sublease boundary "
            "requires prior written approval from FL ORCP. Reserve shall submit sublease "
            "amendment within 60 days of agreement execution.\n\n"
            "Section 6 — Intellectual Property: All scientific data, monitoring results, "
            "and project documentation become property of the Reserve and NOAA upon "
            "project close. C-HAWQ retains right to publish findings with Reserve "
            "co-authorship.\n\n"
            "Section 7 — Risk Allocation: If USACE declines to transfer fund "
            "administration, USACE scope reverts to standard placement; C-HAWQ "
            "obligations limited to Marco Shores and Shell Island Road components. "
            "Neither party liable for USACE scheduling decisions.\n\n"
            "Section 8 — Termination: Either party may terminate for convenience with "
            "90 days written notice. C-HAWQ entitled to reimbursement for committed "
            "expenditures through termination date only.\n\n"
            "Section 9 — F.S. 255.065: The parties acknowledge this agreement is "
            "structured consistent with Florida's Public-Private Partnership statute "
            "and Everglades City precedent for nonprofit P3 administration of public "
            "funds."
        ),
    }),

    # S10-1: Project case study — after Marco Shores Lake and Shell Island Road completion
    ("S10-1", {
        "contact_id":        "ghl_rookery_001",
        "municipality_name": "Naples",
        "county":            "Collier",
        "project_name":      "Rookery Bay NERR Coastal Restoration Partnership",
        "project_type":      "coastal_habitat_restoration_and_beneficial_reuse",
        "project_scope": (
            "Replaced undersized box culvert at Marco Shores Lake restoring snook fish "
            "passage; conducted PFAS, heavy metals, and fish stock surveys; completed "
            "$2.63M Shell Island Road boardwalk restoring hydrological sheet flow with "
            "gopher tortoise protection; administered USACE ICW dredge beneficial reuse "
            "placing ~140,000 CY of spoil via first documented thin-layer mangrove "
            "placement protocol in Florida."
        ),
        "key_outcomes": (
            "Snook passage confirmed within 8 months of culvert installation — "
            "first documented movement in 30+ years. "
            "PFAS levels in Marco Shores Lake declined 62% over 2-year monitoring period "
            "following source controls. "
            "Crocodile nesting monitored: 3 surviving juveniles documented in Year 2 — "
            "first in 25+ years of reserve records. "
            "USACE C-HAWQ Scenario B saved $2.79M vs. TigerTail Beach baseline; "
            "savings reinvested into mangrove habitat research. "
            "UF/IFAS doctoral candidate completed first thesis on thin-layer placement "
            "in Florida mangroves."
        ),
        "total_cost_usd":    9_200_000,
        "funding_sources": (
            "NOAA NERR Program: $3,500,000 | FL Office of Resilience and Coastal "
            "Protection: $2,500,000 | C-HAWQ private match: $3,000,000 | "
            "USACE beneficial reuse offset: $200,000"
        ),
        "champion_name":     "Jared",
        "champion_title":    "Director, Rookery Bay National Estuarine Research Reserve",
        "project_duration":  "20 months (construction start September 2027, substantial completion May 2029)",
        "champion_motivation": (
            "Jared described a backlog of critical projects with no funding path. "
            "The crocodile infertility case — 25 years of hatching with zero surviving "
            "young — was the most visible symbol of how long these problems had gone "
            "unaddressed."
        ),
        "community_impact": (
            "Marco Shores community residents trained as permitted citizen science "
            "monitors; 4 community survey events during project with 160 volunteers; "
            "Bonefish Tarpon Trust used crocodile recovery story in national donor "
            "campaign; WGCU ran 3-part radio series on the project."
        ),
        "challenges_overcome": (
            "USACE timeline required C-HAWQ to finalize engineering specs for Key "
            "Island beneficial reuse in under 90 days. State Lands sublease amendment "
            "took 5 months longer than projected — C-HAWQ restructured phasing so "
            "Marco Shores surveys could proceed while lease was processed."
        ),
        "champion_quotes": (
            '"We had a crocodile population that hadn\'t raised a single young in '
            '25 years. C-HAWQ helped us finally find out why — and fix it." — Jared, '
            "Director, Rookery Bay NERR"
        ),
    }),

    # S10-2: Referral outreach research — Jared refers Everglades City Manager
    ("S10-2", {
        "contact_id":        "ghl_evglades_001",
        "municipality_name": "Everglades City",
        "champion_name":     "Jared",
        "champion_municipality": "Rookery Bay NERR",
        "referral_name":     "Donna Medina",
        "referral_title":    "City Manager",
        "referral_municipality": "City of Everglades City, Florida",
        "referral_known_info": (
            "Jared mentioned Donna has been trying to address water quality and "
            "flooding issues in the Barron River corridor for two years. Everglades "
            "City is small (~400 residents) but is the legal precedent for C-HAWQ's "
            "P3 fund administration model — the local attorney who structured the "
            "Rookery Bay agreement previously did a similar transaction for the city. "
            "Donna is familiar with C-HAWQ by reputation and reportedly supportive."
        ),
        "completed_project_name": "Rookery Bay NERR Coastal Restoration Partnership",
        "completed_project_outcome": (
            "Snook passage restored after 30+ years; first crocodile juveniles in 25 "
            "years documented; $2.79M USACE savings via beneficial reuse; first "
            "Florida thin-layer mangrove placement thesis published"
        ),
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
