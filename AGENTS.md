# Rules for Codex

You are implementing one task of Climate Disclosure Agent. Read, in order:
1. this file
2. the task spec you were given (`specs/tasks/TNN.md`)
3. only the sections of `specs/02-design.md` and the decisions (D-numbers) the task cites
4. `.codex/feedback.md` if it exists — it is review feedback on your previous attempt; fix every point

## Rules
1. Do only what the task spec says. Anything in "Out of scope" of `specs/01-requirements.md` is never done.
2. Change only the files listed under **Files**. Creating a test file next to a listed module is allowed.
3. Write the failing tests first, then the implementation. Every failure case listed in the spec gets its own test.
4. Tests never call a real model or any paid API. Use `cda.llm.fake` (or a stub, before it exists).
5. Never edit `specs/`, `AGENTS.md`, `CLAUDE.md`, `.github/`, `.codex/`, `docs/`. Such changes are reverted before review.
6. If the spec is unclear, implement the most conservative reading and list it under "Uncertain" in your summary. Do not widen scope.
7. Only add dependencies from the stack table in `specs/02-design.md` §1, plus what the task lists.
8. Before finishing, run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src`, `uv run pytest -q`, and fix failures.
9. Never print, log or write secrets or environment variables.

## Final message (becomes the PR description)
Exactly three sections:

```
## Changed
- file: what and why (one line each)

## Tests
- command and result; which spec failure cases each test covers

## Uncertain
- anything you guessed, or "None"
```
