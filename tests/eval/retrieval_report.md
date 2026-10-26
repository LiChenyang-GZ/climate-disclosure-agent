# Retrieval only (no model calls)
- pack: Tiny
- key version: 1
- git commit: —
- date: —

## Retrieval
Embedder: HashEmbedder
Vector scores use HashEmbedder, a hashed bag-of-words (lexical) baseline, not semantic embeddings; run with `--embedder bge` for semantic retrieval.

| Mode / difficulty | Hit@1 | Hit@3 | Hit@5 | Hit@10 | Hit@20 | MRR |
|---|---|---|---|---|---|---|
| hybrid / overall | 0.750 | 0.750 | 0.750 | 0.750 | 0.750 | 0.750 |
| keyword / overall | 0.750 | 0.750 | 0.750 | 0.750 | 0.750 | 0.750 |
| vector / overall | 0.750 | 0.750 | 0.750 | 0.750 | 0.750 | 0.750 |
