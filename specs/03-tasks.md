# Task plan

Implementation is split into small tasks. Each task is one module plus its tests, run by Codex in a fresh session (see `AGENTS.md`). Detailed task specs are written just before each batch starts, so lessons from earlier batches carry forward.

- **Model**: `luna` = gpt-6-luna (cheap), `sol` = gpt-6.1-sol (strong).
- **Tier**: `strict` = reviewed line by line, every failure case must have a test; `normal` = reviewed via summary, diff and CI.
- **Owner**: `codex` writes code; `claude` writes content that needs domain judgement (check definitions, answer keys).

## Phase 1 — backend core and evaluation (before 30 Oct, uses OpenAI credits)

| ID | Task | Depends on | Model | Tier | Owner | Batch |
|---|---|---|---|---|---|---|
| T00 | Repo skeleton: Python package (uv), FastAPI `/health`, one pytest, ruff config. Trial run of the whole Codex → PR → CI loop | — | luna | normal | codex | 0 |
| T01 | Docker Compose: Postgres with pgvector, api and worker containers; Alembic set up | T00 | luna | normal | codex | 1 |
| T02 | Database schema: packs, team membership, documents, units (parent/child), checks, runs, run_events, findings, evidence, facts, inconsistencies, reviews, audit_log | T01 | sol | normal | codex | 1 |
| T03 | `checks.yaml`: the 15 checks (AASB S2 + ASIC 2026 review), evidence criteria, allowed reliefs, facts to extract, min distinct searches. Loader and Pydantic models | T00 | luna (loader) | normal | claude (content) + codex (loader) | 1 |
| T04 | PDF parsing: reading order for two-column pages, header/footer removal, printed vs PDF page numbers, paragraph units capped at ~350 tokens, section parents | T02 | sol | normal | codex | 2 |
| T05 | PDF tables: one child unit per row with header, whole table as parent (Markdown) | T04 | sol | normal | codex | 2 |
| T06 | Indexing: contextual header per unit, Postgres full-text (`ts_rank_cd`), local `bge-small` embeddings into pgvector | T04 | sol | normal | codex | 2 |
| T07 | LLM layer: OpenAI client wrapper, strict function calling + Pydantic validation, retry with backoff, fail fast on 401, token and cost accounting, request fingerprint, fake LLM for tests | T00 | sol | normal | codex | 3 |
| T08 | Agent tools: `list_documents`, `keyword_search`, `read_unit` (child or parent), `semantic_search`; unified result envelope; team-based permission filter in every query | T06 | sol | strict | codex | 3 |
| T09 | Database-level permissions: read-only role for the tool layer, Postgres row-level security by team | T08 | sol | strict | codex | 3 |
| T10 | Quote verifier: verbatim match after whitespace normalisation, adjacent-unit join across pages, permission check on cited units | T08 | sol | strict | codex | 4 |
| T11 | Worker loop (explicit LangGraph nodes, no prebuilt agent): structured state (queries, files covered, candidate evidence, open aspects), step and token caps, repeated-query detection, "no evidence" rule, `submit_finding` | T07, T08, T10 | sol | strict | codex | 4 |
| T12 | Orchestration: outer LangGraph graph, `Send` fan-out with max concurrency 5, budget pre-deduction, Postgres checkpointer, run statuses, human-review interrupt | T11 | sol | strict | codex | 4 |
| T13 | Inconsistency detection: fact normalisation (units, scope, year), code comparison for T3/T4/T6, cross-reference check for T5 | T11 | sol | normal | codex | 5 |
| T14 | Worker process: claim runs with `SKIP LOCKED`, lease and heartbeat, `run_events` + LISTEN/NOTIFY | T12 | sol | normal | codex | 5 |
| T15 | Synthetic company (Wattlebrook Energy): generator for sustainability report + annual report with planted issues (T1, T3, T4, T5, disclaimer, single scenario, injection) and difficulty rules; answer key | T03 | sol | normal | codex (generator) + claude (issue spec, answer key review) | 5 |
| T16 | Retrieval method B (classic RAG): hybrid search with RRF, retrieval quality threshold, single model call | T08, T07 | sol | normal | codex | 6 |
| T17 | Method C (agent + semantic tool) and long-context baseline | T11, T16 | luna | normal | codex | 6 |
| T18 | Evaluation harness: answer key format, status accuracy, evidence recall, citation accuracy, process metrics, injection as hard gate, Hit@K / MRR, repeated runs, `report.md` | T15, T17 | sol | normal | codex | 6 |
| T19 | CI: ruff, mypy, pytest, retrieval eval on the synthetic company (no LLM calls) | T18 | luna | normal | codex | 6 |
| T20 | REST API: packs, upload, parse, runs, findings, review (optimistic lock), export; demo-user header; idempotency key | T12, T14 | luna | normal | codex | 7 |
| T21 | Langfuse tracing | T11 | luna | normal | codex | 7 |
| T22 | MCP server (Python, stdio): read-only tools + `start_check`, same permission functions | T08, T20 | luna | normal | codex | 7 |
| T23 | Real companies: confirm 4 ASX companies, download list, ingest, label evidence (Christy spot-checks 20%) | T06 | — | — | claude + Christy | 6–7 |
| E1–E6 | Experiments (see decision cards): run against real and synthetic packs, fill results | T18, T23 | luna / sol | — | claude | by 30 Oct |

## Phase 2 — after 31 Oct (Claude Code, no OpenAI credits)

| ID | Task |
|---|---|
| U1–U5 | Next.js UI: packs, pack detail, new check, progress (SSE), report, finding detail and review; Playwright tests for each state in PRD 9.1 |
| X1 | Docx / xlsx parsing |
| X2 | OneDrive import (Microsoft Graph) |
| X3 | Salesforce case push |
| X4 | Claude skill on top of the MCP server |
| X5 | Claude Code hooks |
| X6 | README, five screenshots, three-minute video script |

## Estimated OpenAI spend (Phase 1 coding)

About 16 tasks on `sol` at roughly $1–2 each and 7 on `luna` at under $0.30 each: about **$18–33**. The budget is $25, so the estimate will be re-checked after batch 0 using the trial task's real cost, and more tasks will move to `luna` if needed.
