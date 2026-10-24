# Design

This is the source of truth for **how** the system is built. `01-requirements.md` says what it does; `03-tasks.md` says in what order it is built. If code and this file disagree, raise it in the PR — do not silently pick one.

Decisions are numbered **D1–D68**. Task specs cite them by number (e.g. "follow D35, D58").

## 1. Tech stack

| Area | Choice | Notes |
|---|---|---|
| Language | Python 3.12 | type hints everywhere; `mypy --strict` on `src/` |
| Packaging | `uv` (`pyproject.toml` + `uv.lock`) | src layout: `src/cda/` |
| Lint / format | `ruff` (lint + format) | line length 100 |
| Tests | `pytest`, `pytest-asyncio`; `testcontainers[postgres]` for DB tests | tests never call a real model (D-test rule below) |
| API | FastAPI + Uvicorn; Pydantic v2 | SSE via `sse-starlette` |
| DB | PostgreSQL 16 + `pgvector` (image `pgvector/pgvector:pg16`) | SQLAlchemy 2 (async, `asyncpg`) + Alembic |
| Full-text search | Postgres `tsvector` (`english` config) + `ts_rank_cd` | not called BM25 (D35) |
| Embeddings | `BAAI/bge-small-en-v1.5` via `sentence-transformers`, 384 dims, run locally | used by methods B and C only (D10) |
| PDF | PyMuPDF (`pymupdf`) | Docling only if parse tests fail (D32) |
| Agent orchestration | LangGraph (`langgraph`, `langgraph-checkpoint-postgres`) | orchestration only; worker loop is explicit nodes (D67) |
| LLM | `openai` Python SDK, Chat Completions with strict function calling | behind our own `LLMClient` interface (D46) |
| Tracing | Langfuse (self-hosted in compose, optional profile) | D25 |
| MCP | official `mcp` Python SDK, stdio transport | D13, D51 |
| Config | `pydantic-settings`, `.env` (never committed) | |
| Retries | `tenacity` | D20 |
| Synthetic PDFs | `reportlab` | T15 only |
| Containers | Docker Compose: `db`, `api`, `worker`, optional `langfuse` | local only, no cloud |
| UI (phase 2) | Next.js + TypeScript, Playwright | not in phase 1 |

Exact versions: latest stable at the time of T00, pinned in `uv.lock`. Do not add a dependency that is not in this table without saying why in the PR.

### Models (config keys)
| Key | Model | Use |
|---|---|---|
| `luna` | `gpt-6-luna` | development runs, trials, simple checks |
| `sol` | `gpt-6.1-sol` | formal evaluation, comparison (experiment E5) |

Prices live in `config/models.yaml` (per million tokens: input / cached input / output) so cost accounting never hard-codes them.

## 2. Architecture

```
            ┌──────────── FastAPI (api) ────────────┐
 browser ──▶│ packs · upload · runs · findings ·    │──SSE──▶ browser
 / curl     │ review · export     (demo-user header)│
            └──────┬───────────────────────▲────────┘
                   │ insert run (queued)   │ LISTEN/NOTIFY
                   ▼                       │
            ┌──────────── PostgreSQL + pgvector ────┐
            │ packs, documents, units, runs,        │
            │ run_events, findings, evidence, …     │
            │ RLS by team · read-only tool role     │
            └──────▲───────────────────────┬────────┘
                   │ SKIP LOCKED + lease   │ tools (read-only role)
            ┌──────┴──────── worker ───────▼────────┐
            │ parse → index                         │
            │ LangGraph run graph:                  │
            │   plan checks → Send ×15 (max 5) →    │
            │   worker loop per check → compare →   │
            │   human-review interrupt → finalise   │
            └──────────────┬────────────────────────┘
                           │ LLMClient
                           ▼
                        OpenAI API          MCP server (stdio) ── same Python functions
```

- The API never runs a check itself. It inserts a `runs` row with status `queued` (D43).
- The worker claims runs with `SELECT … FOR UPDATE SKIP LOCKED`, writes `lease_expires_at`, renews it every 30 s (D65). An expired lease can be reclaimed and resumes from the LangGraph checkpoint (D23).
- Progress: every step is a `run_events` row + `NOTIFY run_<id>`; the API streams it as SSE and supports `Last-Event-ID` (D44).
- One run at a time system-wide; within a run at most 5 checks in parallel (D45).

## 3. Package layout

```
src/cda/
  config.py            settings, model table
  db/                  models.py, session.py, rls.sql, migrations (alembic/)
  parsing/             pdf.py, layout.py (reading order, header/footer), tables.py, units.py
  indexing/            fts.py, embed.py, context_header.py
  checks/              checks.yaml, loader.py, models.py
  llm/                 client.py (LLMClient protocol), openai_client.py, fake.py, cost.py, fingerprint.py
  tools/               envelope.py, list_documents.py, keyword_search.py, read_unit.py, semantic_search.py, permissions.py
  verify/              quotes.py
  agent/               state.py, worker_graph.py, run_graph.py, nosevidence.py, prompts/ (*.md files)
  compare/             facts.py, normalise.py, inconsistencies.py
  methods/             method_a.py (agent), method_b.py (classic RAG), method_c.py, long_context.py
  runner/              claim.py, lease.py, events.py, main.py
  api/                 app.py, routes/*.py, deps.py (demo user), sse.py
  mcp_server/          server.py
  eval/                answer_key.py, metrics.py, run_eval.py, report.py
  synth/               wattlebrook.py (synthetic company generator)
tests/                 mirrors src/cda/; fixtures/ holds small PDFs and fake-LLM scripts
```

## 4. Data model (Postgres)

| Table | Key columns |
|---|---|
| `users` | id, handle (`sarah`, `alex`), role (`lead`, `analyst`) |
| `teams`, `team_members` | team ↔ user |
| `packs` | id, company, reporting_year, team_id, created_by, created_at |
| `documents` | id, pack_id, kind (`sustainability`, `annual`, `other`), filename, sha256, page_count, parser_version, deleted_at (soft delete, D53) |
| `units` | id, document_id, parent_id (nullable), kind (`paragraph`, `table_row`, `section`, `table`), pdf_page, printed_page (nullable), section_path, text, context_header, tsv (`tsvector`), embedding (`vector(384)`, nullable), parser_version, token_count. **Immutable** once written (D52) |
| `check_sets` | version, yaml_sha256 |
| `runs` | id, pack_id, method (`A`,`B`,`C`,`LC`), model_key, check_set_version, prompt_sha256, budget_usd, spent_usd, status (`queued`,`running`,`waiting_review`,`final`,`cancelled`,`failed`), lease_owner, lease_expires_at, idempotency_key (unique, D24) |
| `run_events` | id (bigserial), run_id, check_id (nullable), type, payload jsonb, created_at |
| `findings` | id, run_id, check_id, status, confidence (`high`,`low`, D40), rationale, missing_aspects, relief_applied, steps, tokens_in, tokens_out, cost_usd, version (optimistic lock) |
| `evidence` | id, finding_id, unit_id, quote, role (`supports`, `side_a`, `side_b`) |
| `facts` | id, finding_id, unit_id, name, value_num, unit, scope, year, raw |
| `inconsistencies` | id, run_id, type (`T1`–`T6`), finding_id, detail jsonb |
| `reviews` | id, finding_id, reviewer_id, decision, new_status (nullable), note (required), created_at |
| `llm_calls` | id, run_id, check_id, step, model, prompt_sha256, tools_sha256, temperature, seed, system_fingerprint, tokens, cost (D66) |
| `audit_log` | id, actor, action, target, detail, created_at |

Permissions (D29, D61): every tool query joins `packs.team_id` against the caller's teams. In addition, tools connect as role `cda_tool` (SELECT only) with RLS policies on `packs`, `documents`, `units` keyed on `current_setting('cda.user_id')`, set per transaction.

## 5. Parsing and indexing (T04–T06)
- Reading order: sort PyMuPDF text blocks by column then y; detect two columns by x-gap (D32).
- Header/footer: the same text at the same position on more than half the pages is dropped (D32).
- Store `pdf_page` (1-based, used for links `#page=N` and verification) and `printed_page` when detectable (D31).
- Child units: paragraphs; paragraphs over ~350 tokens split on sentence boundaries, no overlap (D34). Tables: one child per row with header cells prefixed; the whole table is the parent, stored as Markdown (D33, D62). Sections are parents of paragraphs.
- Context header per unit: `"{filename} > {section path}"` prepended for indexing only, never in quotes (D6).
- Limits: max 600 pages, per-page timeout, worker container memory cap (D49). No OCR, no chart images.

## 6. Agent tools (T08)

All tools return the same envelope (D60):

```json
{"ok": true, "category": "found|not_found|bad_args|timeout|forbidden", "retryable": false,
 "message": "short text for the model, no stack traces or paths", "data": {...}}
```

| Tool | Args | Returns |
|---|---|---|
| `list_documents` | — | documents in the pack: id, kind, filename, page_count |
| `keyword_search` | `query: str`, `document_ids?: list`, `limit≤10` | units: id, document, pdf_page, printed_page, section_path, snippet (≤300 chars), rank |
| `read_unit` | `unit_id`, `mode: "unit"|"parent"`, `neighbours: 0..2` | full text (truncated to 4,000 chars with a marker, D15) |
| `semantic_search` | same as keyword_search | method C only |
| `submit_finding` | see §7 | ends the loop |

- Tool layer records every query (normalised: lower case, collapsed whitespace) and the documents it covered — this feeds D58.
- Document text is data, not instructions (D28). The system prompt says so; tools are read-only; findings must pass verification.

## 7. Worker loop per check (T11)

Explicit LangGraph nodes (D67), ReAct style (D2):

```
build_prompt → call_model → (tool call?) ─yes→ run_tool → update_state → guard → build_prompt
                                └─ submit_finding → verify → (ok) → done
                                                         └ (fail, first time) → build_prompt with error
                                                         └ (fail, second time) → Needs review
```

- **State** (D59): `queries_done`, `documents_covered`, `candidate_units`, `open_aspects`, `step`, `tokens`. The short state summary goes into every prompt; old tool results may be truncated.
- **Guards**: step cap 12, token cap 60k per check (D15, D22); repeated identical normalised query is rejected with a `bad_args` envelope; budget pre-deducted before each model call — if the run budget would be exceeded, stop and mark remaining checks `Not checked`.
- **`submit_finding`** args (Pydantic, strict schema, D12, D47): `status`, `rationale`, `missing_aspects[]`, `relief_applied: bool`, `evidence[] {unit_id, quote, role}`, `facts[] {name, value, unit, scope, year, unit_id}`. Invalid JSON → one retry with the validation error, then `Error`.
- **Verification** (D17 option A, T10): each quote must appear verbatim in the cited unit after whitespace normalisation; a quote may span the cited unit and the next unit (page breaks); the unit must belong to the run's pack and be visible to the user.
- **"No evidence found" rule** (D58): accepted only if `len(distinct normalised queries) ≥ min_searches` (default 4, per check in `checks.yaml`) **and** every non-deleted document in the pack was covered. Otherwise status becomes `Needs review` with reason `insufficient search`.
- **Confidence** (D40): high = ≥1 verified quote, all documents covered, no cap hit; otherwise low. "No evidence" is always low.
- Model settings: temperature 0, fixed seed, record `system_fingerprint` (D18, D48). Shared system prompt first for prompt caching (D27). Prompts are files under `agent/prompts/`; their SHA-256 is stored on the run (D39).
- Reasoning goes in `rationale` only; no separate long chain-of-thought output (D30).

## 8. Run graph (T12)
`load_checks → Send(check) ×15 with max_concurrency=5 (D16, D45) → compare (T13) → if any Needs review: interrupt (wait for human) → finalise`. Postgres checkpointer (D23). Statuses per requirements. Errors: 429 → exponential backoff, max 3 retries (D20); 401 → fail the whole run immediately (D21).

## 9. Inconsistency detection (T13, D41, D42)
- Workers extract facts into `facts`. Code compares T3 (restated figures), T4 (regulatory target in annual report missing from targets), T6 (reporting period).
- Normalise units (t / kt / Mt CO2e → tCO2e), carry `scope` (group / Australian operations) and `year`; if scope or year differ or a number cannot be extracted → `Needs review`, never `Inconsistent`.
- T1, T2 are judged by the worker for the relevant check and need a quote from each document (`side_a`, `side_b`).
- T5: worker lists cross-references; code checks the referenced document exists in the pack and the reference names a section.
- `Inconsistent` has the highest precedence over other statuses.

## 10. Retrieval methods (T16, T17)
| Method | How |
|---|---|
| A | agent with `list_documents`, `keyword_search`, `read_unit` (default) |
| B | classic RAG: queries = check description + 2–3 pre-written queries from `checks.yaml` (D36); hybrid FTS + vector merged by RRF, k=60 (D37); top-k from experiment; if top score < threshold → `Needs review`, no model call (D63); one model call with `submit_finding` |
| C | A plus `semantic_search` |
| LC | long-context baseline: whole pack in one prompt (D4); used only in experiments |

No reranker in phase 1 (D9, experiment E3).

## 11. API (T20)
`X-Demo-User: sarah|alex` header selects the user (no real auth). Routes: `POST/GET /packs`, `POST /packs/{id}/documents` (upload), `POST /packs/{id}/parse`, `POST /runs` (needs `Idempotency-Key`), `GET /runs/{id}`, `GET /runs/{id}/events` (SSE), `POST /runs/{id}/cancel`, `GET /runs/{id}/findings`, `GET /findings/{id}`, `POST /findings/{id}/reviews` (lead only; `version` for optimistic lock; note required), `GET /runs/{id}/export?format=md|json|csv`. Quotes are returned as plain text; Markdown export escapes special characters (D50).

## 12. MCP server (T22)
Stdio, local only. Calls the same Python functions as the API, including permission functions (D51). Tools: `list_packs`, `get_run`, `get_findings`, `search_pack`, `start_check`. No review through MCP.

## 13. Evaluation (T18, T19)
- Answer key per pack: check → expected status, expected evidence (document, pdf_page, key text). Evidence match = same page and key-text overlap (D54).
- Metrics: status accuracy, evidence recall, citation accuracy, process metrics (repeat-query rate, document coverage, mean steps, cap-hit rate), Hit@K and MRR for retrieval; injection tests are a hard gate reported separately (D64). Each method run 3 times, report ranges (D48).
- Real companies: use the company's AASB S2 index table where present; otherwise Claude labels and Christy spot-checks 20% (D55).
- CI on every push: unit tests, parse tests, fake-LLM flow tests, keyword-retrieval recall on the synthetic company — no paid calls. Full eval is manual (D56).

## 14. Testing rules
- No test calls OpenAI. Use `llm/fake.py`: a scripted fake that returns a given sequence of tool calls.
- DB tests use testcontainers Postgres with pgvector, real migrations, real RLS.
- `strict` tasks: every failure case listed in the task spec has its own test, written first and seen failing.

## 15. Decision digest

Full reasoning (Chinese) is in the owner's notes; this is the binding summary.

| D | Decision |
|---|---|
| 1 | Plain code dispatches the 15 checks; the agent is used only to find evidence |
| 2 | Worker uses ReAct (act, observe, decide), not plan-and-execute |
| 3 | LangGraph for orchestration, parallelism, checkpoints, human interrupt |
| 4 | Retrieval (default) and long-context are both built and compared (E1) |
| 5 | Units follow document structure (page, heading, paragraph, table row), not fixed length |
| 6 | Context header `file > section` prepended for indexing; LLM-written context only as E2 |
| 7 | Method B uses hybrid FTS + vector with RRF; agent relies mainly on keyword search |
| 8 | pgvector in the same Postgres as metadata and permissions |
| 9 | No reranker in phase 1 (E3) |
| 10 | Local `bge-small-en-v1.5` embeddings |
| 11 | Small tools (list, search, read) rather than one "find evidence" tool |
| 12 | Findings submitted via `submit_finding` with a Pydantic schema |
| 13 | Function calling inside the system; MCP for external clients |
| 14 | Short-term memory only, within one check; no cross-run memory |
| 15 | Truncate long tool results + step cap; no summarisation |
| 16 | One worker per check, run in parallel |
| 17 | Code verifies quotes; an LLM judge is optional later |
| 18 | temperature 0 (E4) |
| 19 | Cheap model for development; both models in formal eval (E5) |
| 20 | 429 → exponential backoff, max 3 retries |
| 21 | 401 → stop the whole run at once |
| 22 | Per-run budget, pre-deducted before each call |
| 23 | LangGraph Postgres checkpointer; resume after crash |
| 24 | Idempotency key on run creation |
| 25 | Langfuse traces + DB audit log |
| 26 | Eval set + CI threshold for prompt changes |
| 27 | Shared system prompt first to benefit from prompt caching |
| 28 | Document text is data, not instructions; read-only tools; verified findings |
| 29 | Permission filter in every retrieval SQL |
| 30 | Reasoning only in `rationale` |
| 31 | Store both printed and PDF page numbers; navigate by PDF page |
| 32 | PyMuPDF block sorting + repeated header/footer removal; Docling only if needed |
| 33 | Table rows as units with headers; charts out of scope |
| 34 | Paragraph units ≤ ~350 tokens, sentence split, no overlap |
| 35 | Postgres full-text with `ts_rank_cd`, not true BM25 |
| 36 | Method B queries: check description + pre-written queries |
| 37 | RRF k = 60; top-k decided by experiment |
| 38 | Each check spec: requirement text, what counts, what does not, reliefs, facts, preferred documents |
| 39 | Prompts are versioned files; hash stored per run |
| 40 | Confidence high/low by rule |
| 41 | T3/T4/T6 compared in code; T1/T2 judged by worker with both quotes; T5 checked in code |
| 42 | Unit normalisation; different scope/year → Needs review |
| 43 | Checks run in a separate worker process; API only enqueues |
| 44 | Progress via `run_events` + LISTEN/NOTIFY + SSE with Last-Event-ID |
| 45 | Max 5 checks in parallel; one run at a time |
| 46 | OpenAI only, behind our own `LLMClient` |
| 47 | Strict function calling + Pydantic; one retry on invalid output |
| 48 | Seed + fingerprint; eval repeats 3 times |
| 49 | PDF limits: 600 pages, per-page timeout, memory cap |
| 50 | Quotes shown as plain text; escape on export |
| 51 | MCP server calls the same Python functions |
| 52 | Units immutable; re-parse creates new units |
| 53 | Soft delete for documents |
| 54 | Evidence match = same page + key-text overlap |
| 55 | Real-company answer keys from index tables or Claude labels + 20% human spot-check |
| 56 | Free tests on every push; paid eval manual |
| 57 | Show extracted text with highlight; open original PDF at `#page=N` |
| 58 | "No evidence" only with ≥ N distinct queries and full document coverage |
| 59 | Worker progress kept in structured state |
| 60 | Unified tool result envelope |
| 61 | Read-only DB role + RLS as a second permission layer |
| 62 | Parent/child units: search children, read parents |
| 63 | Method B retrieval-quality threshold |
| 64 | Process metrics, injection hard gate, judge calibration, Hit@K/MRR |
| 65 | Run lease with 30 s heartbeat |
| 66 | Request fingerprint per model call |
| 67 | Worker loop as explicit nodes, no prebuilt agent |
| 68 | E6: evidence position test (start / middle / end) |
