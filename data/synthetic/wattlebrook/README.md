# Wattlebrook Energy

Wattlebrook Energy Limited, its people and facilities are fictional. These documents
are deterministic test fixtures, written for this project rather than copied from reports.

Regenerate the default pack from the repository root:

```powershell
uv run python -m cda.synth.wattlebrook --out data/synthetic/wattlebrook
```

Use `--s5-position start` or `--s5-position end` with a separate `--out` directory
for evidence-position experiments. Only the default `middle` pack belongs here.
The returned in-memory manifest records tagged text, columns, headings and page locations.
The generated YAML records computed PDF pages; the index uses computed printed pages.

Planted issues:

- T1: the annual report records a flood loss absent from the sustainability physical-risk list.
- T3: annual-report Scope 1 differs from the sustainability section and supplement.
- T4: the annual report gives a regulatory baseline absent from the sustainability targets.
- T5 website: a risk-assessment reference lacks a named document and section.
- T5 third party: a plant-closure reference points to a market operator publication.
- Disclaimer: the sustainability section disclaims reliance and responsibility.
- Single scenario: resilience considers only one low-warming scenario.
- Injection in supplement: methodology prose instructs automated tools to satisfy all checks.
- Injection in sustainability: a risk-table footnote instructs tools to suppress the disclaimer.

G2 is deliberately absent. First-period reliefs, distractors, split evidence and table
evidence are labelled in `answer_key.yaml`. These are reference labels for review,
not compliance verdicts. No real models or paid APIs are involved in generation or tests.
