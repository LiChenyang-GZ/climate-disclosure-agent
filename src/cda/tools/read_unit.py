"""Read immutable evidence in document reading order."""

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_

from cda.db.models import Unit
from cda.tools.context import ToolContext
from cda.tools.envelope import ToolResult, forbidden, found, not_found
from cda.tools.parts_sql import inventory, unit_query


class ReadUnitArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unit_id: UUID
    mode: Literal["unit", "parent"] | None = None
    neighbours: Annotated[int, Field(ge=0, le=2)] | None = None


def cap_units(rows: list[Any], anchor: Any) -> tuple[list[dict[str, Any]], bool]:
    # Reserve the requested evidence before allocating space to neighbours.
    marker = "\n[truncated]"
    ordered = [anchor] + sorted(
        [r for r in rows if r["id"] != anchor["id"]], key=lambda r: r["seq"]
    )
    total = sum(len(r["text"]) for r in ordered)
    truncated = total > 4000
    budget = 4000 - len(marker) if truncated else 4000
    kept = []
    cut = None
    for row in ordered:
        if budget <= 0:
            break
        item = {
            k: row[k]
            for k in [
                "kind",
                "pdf_page",
                "page_half",
                "printed_page",
                "section_path",
                "part_id",
                "text",
            ]
        }
        item["unit_id"] = str(row["id"])
        if len(item["text"]) > budget:
            cut = item
        item["text"] = item["text"][:budget]
        budget -= len(item["text"])
        kept.append((row["seq"], item))
    kept.sort(key=lambda pair: pair[0])
    if truncated:
        marked = cut if cut is not None else kept[-1][1]
        marked["text"] += marker
    return [item for _, item in kept], truncated


async def read_unit(ctx: ToolContext, args: ReadUnitArgs) -> ToolResult:
    if await inventory(ctx) is None:
        return forbidden()
    base = unit_query(ctx)
    row = (await ctx.conn.execute(base.where(Unit.id == args.unit_id))).mappings().first()
    if row is None:
        return not_found()
    rows: list[Any] = [row]
    anchor = row
    if args.mode == "parent":
        if row["kind"] in ("paragraph", "table_row") and row["parent_id"] is not None:
            parent = (
                (
                    await ctx.conn.execute(
                        base.where(
                            Unit.id == row["parent_id"], Unit.document_id == row["document_id"]
                        )
                    )
                )
                .mappings()
                .first()
            )
            if parent is not None:
                anchor = parent
                rows = [parent]
        if anchor["kind"] == "section":
            rows = list(
                (
                    await ctx.conn.execute(
                        base.where(
                            Unit.document_id == row["document_id"],
                            or_(
                                Unit.id == anchor["id"],
                                (Unit.parent_id == anchor["id"])
                                & Unit.kind.in_(["paragraph", "table"]),
                            ),
                        ).order_by(Unit.seq)
                    )
                ).mappings()
            )
    elif args.neighbours:
        children = base.where(
            Unit.document_id == row["document_id"], Unit.kind.in_(["paragraph", "table_row"])
        )
        before = list(
            (
                await ctx.conn.execute(
                    children.where(Unit.seq < row["seq"])
                    .order_by(Unit.seq.desc())
                    .limit(args.neighbours)
                )
            ).mappings()
        )
        after = list(
            (
                await ctx.conn.execute(
                    children.where(Unit.seq > row["seq"]).order_by(Unit.seq).limit(args.neighbours)
                )
            ).mappings()
        )
        rows = list(reversed(before)) + [row] + after
    units, truncated = cap_units(rows, anchor)
    return found({"units": units, "truncated": truncated})
