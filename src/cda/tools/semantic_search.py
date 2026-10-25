"""Scoped vector retrieval using the context embedder."""

import asyncio

from cda.db.models import Unit
from cda.tools.context import ToolContext
from cda.tools.envelope import ToolResult, bad_args, found, not_found
from cda.tools.keyword_search import SearchArgs, hit, search_scope
from cda.tools.parts_sql import part_id, unit_query


async def semantic_search(ctx: ToolContext, args: SearchArgs) -> ToolResult:
    if ctx.embedder is None:
        return bad_args("Semantic search not available.")
    scope = await search_scope(ctx, args, "semantic_search")
    if isinstance(scope, ToolResult):
        return scope
    vector = await asyncio.to_thread(ctx.embedder.embed_query, args.query)
    score = (1 - Unit.embedding.cosine_distance(vector)).label("score")
    stmt = (
        unit_query(ctx)
        .add_columns(score)
        .where(
            Unit.kind.in_(["paragraph", "table_row"]),
            Unit.embedding.is_not(None),
            part_id.in_(scope),
        )
        .order_by(score.desc(), Unit.seq, Unit.id)
        .limit(args.limit or 10)
    )
    hits = [hit(r, args.query, "score") for r in (await ctx.conn.execute(stmt)).mappings()]
    return found({"hits": hits}) if hits else not_found("No hits; try other terms.", {"hits": []})
