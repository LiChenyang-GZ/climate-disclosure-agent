"""Fixed-query hybrid retrieval through the permission-aware tools."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from cda.checks.models import normalise_query
from cda.db.models import Unit
from cda.tools.context import ToolContext
from cda.tools.envelope import ToolResult
from cda.tools.parts_sql import unit_query
from cda.tools.registry import run_tool
from cda.tools.rls import tool_scope


class RetrievalError(Exception):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


@dataclass
class HybridResult:
    hits: list[dict[str, Any]]
    best_vector_score: float | None
    queries: list[str]


def _data(result: ToolResult) -> dict[str, Any]:
    if result.category == "not_found":
        return {}
    if not result.ok:
        raise RetrievalError(result.category)
    return result.data or {}


def rrf(lists: list[tuple[str, list[dict[str, Any]]]], k: int) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for method, hits in lists:
        for rank, hit in enumerate(hits, 1):
            uid = hit["unit_id"]
            if uid not in merged:
                merged[uid] = dict(hit, rrf_score=0.0, best_ranks={})
            item = merged[uid]
            item["rrf_score"] += 1 / (k + rank)
            ranks = item["best_ranks"]
            ranks[method] = min(rank, ranks.get(method, rank))
    return sorted(merged.values(), key=lambda h: (-h["rrf_score"], h["pdf_page"], h["unit_id"]))


async def hybrid_search(
    ctx: ToolContext, queries: list[str], *, per_list: int = 10, top_k: int = 10, rrf_k: int = 60
) -> HybridResult:
    actual = []
    seen = set()
    lists = []
    scores: list[float] = []
    for query in queries:
        normalised = normalise_query(query)
        if normalised in seen:
            continue
        seen.add(normalised)
        actual.append(query)
        for method in ("keyword_search", "semantic_search"):
            result = await run_tool(ctx, method, {"query": query, "limit": per_list})
            hits = _data(result).get("hits", [])
            lists.append((method, hits))
            if method == "semantic_search":
                scores.extend(h["score"] for h in hits)
    return HybridResult(rrf(lists, rrf_k)[:top_k], max(scores) if scores else None, actual)


async def load_context(ctx: ToolContext, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks = []
    seen = set()
    parents = set()
    for hit in hits:
        result = await run_tool(ctx, "read_unit", {"unit_id": hit["unit_id"], "mode": "unit"})
        for unit in _data(result).get("units", []):
            if unit["unit_id"] not in seen:
                blocks.append(hit | unit)
                seen.add(unit["unit_id"])
        if hit["kind"] != "table_row":
            continue
        # Search hits omit parent_id. Resolve only that metadata under the tool RLS role;
        # all text still comes from read_unit, with its standard truncation.
        async with tool_scope(ctx.conn, ctx.user_id, ctx.timeout_s):
            parent_id = await ctx.conn.scalar(
                unit_query(ctx)
                .with_only_columns(Unit.parent_id)
                .where(Unit.id == UUID(hit["unit_id"]))
            )
        if parent_id is None or parent_id in parents:
            continue
        parents.add(parent_id)
        result = await run_tool(ctx, "read_unit", {"unit_id": hit["unit_id"], "mode": "parent"})
        for unit in _data(result).get("units", []):
            if unit["unit_id"] not in seen:
                blocks.append(hit | unit)
                seen.add(unit["unit_id"])
    return blocks
