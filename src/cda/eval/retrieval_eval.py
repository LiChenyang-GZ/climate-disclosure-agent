"""Retrieval-only evaluation without a model client."""

from typing import Any

from cda.checks.loader import load_check_set
from cda.checks.models import normalise_query
from cda.db.models import Unit
from cda.eval.answer_key import AnswerKey
from cda.eval.match import token_overlap
from cda.eval.metrics import ratio
from cda.indexing.embed import HashEmbedder
from cda.methods.retrieval import _data, rrf
from cda.tools.context import ToolContext
from cda.tools.keyword_search import SearchArgs, keyword_search
from cda.tools.parts_sql import unit_query
from cda.tools.rls import tool_scope
from cda.tools.semantic_search import semantic_search


async def evaluate_retrieval(
    ctx: ToolContext, key: AnswerKey, modes: tuple[str, ...] = ("keyword", "vector", "hybrid")
) -> dict[str, Any]:
    output: dict[str, Any] = {"embedder": type(ctx.embedder).__name__, "modes": {}}
    checks = load_check_set()
    for mode in modes:
        if mode not in ("keyword", "vector", "hybrid"):
            raise ValueError("unknown retrieval mode")
        ranks: list[tuple[str, int | None]] = []
        for cell in key.cells:
            if not cell.evidence:
                continue
            fresh = ToolContext(ctx.conn, ctx.user_id, ctx.pack_id, ctx.embedder, ctx.timeout_s)
            lists = []
            check = checks.get(cell.check_id)
            async with tool_scope(ctx.conn, ctx.user_id, ctx.timeout_s):
                # Same de-duplication as method B: G1's first query repeats its title.
                queries = list(
                    dict.fromkeys(normalise_query(q) for q in [check.title, *check.queries])
                )
                for query in queries:
                    for tool in (("keyword", keyword_search), ("vector", semantic_search)):
                        if mode not in (tool[0], "hybrid"):
                            continue
                        # The interactive schema caps requests at ten. Evaluation uses
                        # the same scoped tool implementation with a twenty-hit limit.
                        args = SearchArgs.model_construct(query=query, limit=20)
                        lists.append((tool[0], _data(await tool[1](fresh, args)).get("hits", [])))
                hits = rrf(lists, 60)[:20]
                ids = [h["unit_id"] for h in hits]
                rows = (
                    (await ctx.conn.execute(unit_query(ctx).where(Unit.id.in_(ids))))
                    .mappings()
                    .all()
                    if ids
                    else []
                )
                texts = {str(r["id"]): r["text"] for r in rows}
            rank = next(
                (
                    i
                    for i, h in enumerate(hits, 1)
                    if any(
                        h["filename"] == e.document
                        and h["pdf_page"] == e.pdf_page
                        and token_overlap(texts.get(h["unit_id"], ""), e.quote)
                        for e in cell.evidence
                    )
                ),
                None,
            )
            ranks.append((str(cell.difficulty), rank))

        def metrics(values: list[int | None]) -> dict[str, float]:
            return {
                **{
                    f"Hit@{k}": ratio(sum(r is not None and r <= k for r in values), len(values))
                    for k in (1, 3, 5, 10, 20)
                },
                "MRR": ratio(sum(1 / r for r in values if r), len(values)),
            }

        output["modes"][mode] = {
            "overall": metrics([r for _, r in ranks]),
            "difficulty": {
                d: metrics([r for difficulty, r in ranks if difficulty == d])
                for d in sorted({d for d, _ in ranks})
            },
        }
    if isinstance(ctx.embedder, HashEmbedder):
        output["note"] = "Only keyword retrieval is meaningful with HashEmbedder."
    return output
