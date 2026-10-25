"""Scoped PostgreSQL full-text retrieval."""

import re
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, literal_column

from cda.db.models import Unit
from cda.tools.context import ToolContext
from cda.tools.envelope import ToolResult, bad_args, forbidden, found, not_found
from cda.tools.parts_sql import inventory, part_id, unit_query


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=200)
    document_ids: list[UUID] | None = None
    part_ids: list[str] | None = None
    limit: Annotated[int, Field(ge=1, le=10)] | None = None

    @field_validator("query", mode="before")
    @classmethod
    def strip_query(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


async def search_scope(ctx: ToolContext, args: SearchArgs, tool: str) -> set[str] | ToolResult:
    docs = await inventory(ctx)
    if docs is None:
        return forbidden()
    all_docs = {d["document_id"] for d in docs}
    all_parts = {p["part_id"] for d in docs for p in d["parts"]}
    unknown = len(set(map(str, args.document_ids or [])) - all_docs) + len(
        set(args.part_ids or []) - all_parts
    )
    if unknown:
        return bad_args(f"{unknown} unknown scope identifiers.")
    scope = {
        p["part_id"]
        for d in docs
        if args.document_ids is None or UUID(d["document_id"]) in args.document_ids
        for p in d["parts"]
        if args.part_ids is None or p["part_id"] in args.part_ids
    }
    if not scope:
        return bad_args("Search scope is empty; widen document_ids or part_ids.")
    if not ctx.coverage.record(tool, args.query, scope):
        return bad_args("repeated query; try different terms or scope")
    return scope


def snippet(text: str, query: str) -> str:
    words = re.findall(r"\w+", query)
    match = (
        re.search(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\w*", text, re.IGNORECASE)
        if words
        else None
    )
    start = max(0, match.start() - 100) if match else 0
    # Markers are outside the verbatim excerpt, included in the length cap.
    end = min(len(text), start + 298)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def hit(row: Any, query: str, metric: str) -> dict[str, Any]:
    return dict(
        unit_id=str(row["id"]),
        document_id=str(row["document_id"]),
        filename=row["filename"],
        part_id=row["part_id"],
        part_kind=row["part_kind"],
        part_label=row["part_label"],
        kind=row["kind"],
        pdf_page=row["pdf_page"],
        page_half=row["page_half"],
        printed_page=row["printed_page"],
        section_path=row["section_path"],
        snippet=snippet(row["text"], query),
        **{metric: float(row[metric])},
    )


async def keyword_search(ctx: ToolContext, args: SearchArgs) -> ToolResult:
    scope = await search_scope(ctx, args, "keyword_search")
    if isinstance(scope, ToolResult):
        return scope
    query = func.websearch_to_tsquery(literal_column("'english'"), args.query)
    rank = func.ts_rank_cd(Unit.tsv, query).label("rank")
    stmt = (
        unit_query(ctx)
        .add_columns(rank)
        .where(
            Unit.kind.in_(["paragraph", "table_row"]), part_id.in_(scope), Unit.tsv.op("@@")(query)
        )
        .order_by(rank.desc(), Unit.seq, Unit.id)
        .limit(args.limit or 10)
    )
    hits = [hit(r, args.query, "rank") for r in (await ctx.conn.execute(stmt)).mappings()]
    return found({"hits": hits}) if hits else not_found("No hits; try other terms.", {"hits": []})
