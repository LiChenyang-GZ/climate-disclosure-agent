# Requirements

## What it is
Climate Disclosure Agent locates evidence for Australian mandatory climate disclosure requirements in a listed company's reports. A reviewer uploads a company's **sustainability report** and **annual report**; the system checks them against **15 checks** (11 from AASB S2, 4 from ASIC's 2026 review findings), cites every piece of evidence down to file, page and paragraph, flags **inconsistencies between the two reports**, and hands uncertain items to a human.

It locates evidence to support review. It never states that a company is or is not compliant.

## Users
A fictional advisory firm, Harbourline Advisory. Two demo accounts, switched in the header, no real login:

| Account | Role | Can see |
|---|---|---|
| `sarah` | Engagement Lead | All packs. Creates packs, uploads, parses, starts checks, reviews, exports, pushes to Salesforce |
| `alex` | Analyst | Only packs whose team includes him. Starts checks and exports on those packs. Cannot review |

Permissions are enforced in data access (queries and Postgres row-level security), never by prompt.

## Core flow
1. Create a pack (company + reporting year + team) → upload PDFs → parse
2. Start a check (retrieval method, model, budget cap) → live progress per check
3. Report: one row per check with a status, evidence count, review state; Draft until all "Needs review" items are reviewed, then Final
4. Finding detail: rationale, evidence cards with verbatim quotes, side-by-side inconsistency view, agent trace; review form
5. Export (Markdown / JSON / CSV); optional push of follow-ups to Salesforce as Cases
6. MCP server: the same read-only capabilities plus `start_check`, for Claude Desktop / Claude Code

## The 15 checks
G1 board oversight · G2 management role · S1 risks and opportunities with time horizons · S2 business model and value chain effects · S3 strategy response and transition plan · S4 current and anticipated financial effects (or why not quantified) · S5 scenario analysis with at least a 1.5°C and a ≥2.5°C scenario · R1 risk management process and integration · M1 Scope 1 and 2 emissions, Scope 3 or stated first-year relief · M2 cross-industry metrics · M3 climate targets including regulatory targets (e.g. Safeguard Mechanism) and progress · A1 no impermissible disclaimer · A2 weather-related financial effects in the annual report reflected as climate risks · A3 cross-references specify document and section and point to entity-published documents · A4 mandatory disclosures clearly identifiable (index table or separation).

Full definitions live in `checks.yaml` (task T03).

## Finding statuses
| Status | When |
|---|---|
| Evidence found | At least one verified quote fully covers the check; stated transitional relief counts and is labelled "relief applied" |
| Partial evidence | Evidence covers part of the check; the rationale names the missing part |
| No evidence found | Decided by code, not the model: at least N distinct normalised queries (default 4, per check) **and** every document in the pack searched. Otherwise → Needs review |
| Inconsistent | The two reports disagree on the same fact (highest precedence) |
| Needs review | Quote verification failed twice, step/token cap hit, or "no evidence" claimed without enough search |
| Not checked | Cancelled or budget exhausted before this check ran |
| Error | System error; reason recorded |

## Inconsistency types (each from a published source)
T1 past weather losses vs unidentified climate risk (ASIC 2026 obs. 2; REP 839 p11) · T2 climate commitments vs financial statement assumptions (Carbon Tracker; ESMA; REP 839 p4, connecting the sustainability report with the financial report) · T3 annual report restates climate figures differently (RG 280.116) · T4 regulatory target mentioned in annual report but missing from targets (ASIC obs. 6; REP 839 pp21–22) · T5 non-compliant cross-reference (ASIC obs. 5; REP 839 pp24–25; RG 280.86–88) · T6 reporting period mismatch (RG 280.95). Sources checked against the PDFs on 27 Oct 2026.

## Out of scope
Real authentication; AASB S1 or other standards; compliance verdicts, scores or rankings; recalculating emissions; editing check definitions in the UI; scanned PDFs / OCR; numbers inside chart images; auto-downloading reports; a chat box in the UI; mobile layout; cloud deployment (local only for now).

## Non-functional
- One company (~200–300 pages) checked in under 10 minutes with the cheap model
- Every run shows tokens, cost and duration; default budget cap $2
- `docker compose up` starts everything; tests never call a real model
- Reproducible: every run records model, retrieval method, check-set version, prompt hash
