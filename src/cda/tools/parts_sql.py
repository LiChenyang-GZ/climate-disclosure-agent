"""Query-time logical part expressions and document inventory."""

from typing import Any

from sqlalchemy import String, and_, case, cast, exists, func, select

from cda.db.models import Document, DocumentPart, Pack, TeamMember, Unit
from cda.tools.context import ToolContext
from cda.tools.permissions import visible_pack_clause

# Correlated existence distinguishes the whole document from its uncovered rest.
_has_parts = exists(
    select(DocumentPart.id).where(DocumentPart.document_id == Document.id).correlate(Document)
)
part_id = func.coalesce(
    cast(DocumentPart.id, String),
    case((_has_parts, cast(Document.id, String) + ":rest"), else_=cast(Document.id, String)),
).label("part_id")
part_kind = func.coalesce(DocumentPart.kind, Document.kind).label("part_kind")
part_label = case(
    (DocumentPart.id.is_not(None), DocumentPart.label), (_has_parts, "Rest of document"), else_=None
).label("part_label")


def unit_query(ctx: ToolContext) -> Any:
    return (
        select(Unit.__table__, Document.filename, part_id, part_kind, part_label)
        .select_from(Unit)
        .join(Document, Unit.document_id == Document.id)
        .join(Pack, Document.pack_id == Pack.id)
        .join(TeamMember, visible_pack_clause(ctx.user_id))
        .outerjoin(
            DocumentPart,
            and_(
                DocumentPart.document_id == Document.id,
                Unit.pdf_page.between(DocumentPart.first_pdf_page, DocumentPart.last_pdf_page),
            ),
        )
        .where(Pack.id == ctx.pack_id, Document.deleted_at.is_(None))
    )


async def inventory(ctx: ToolContext) -> list[dict[str, Any]] | None:
    # The outer document join preserves visible empty packs in this same query.
    stmt = (
        select(
            Pack.id.label("pack"),
            Document.__table__,
            DocumentPart.id.label("part_id"),
            DocumentPart.kind.label("part_kind"),
            DocumentPart.label,
            DocumentPart.first_pdf_page,
            DocumentPart.last_pdf_page,
        )
        .select_from(Pack)
        .join(TeamMember, visible_pack_clause(ctx.user_id))
        .outerjoin(Document, and_(Document.pack_id == Pack.id, Document.deleted_at.is_(None)))
        .outerjoin(DocumentPart, DocumentPart.document_id == Document.id)
        .where(Pack.id == ctx.pack_id)
        .order_by(Document.filename, DocumentPart.first_pdf_page)
    )
    rows = (await ctx.conn.execute(stmt)).mappings().all()
    if not rows:
        return None
    docs: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["id"] is None:
            continue
        did = str(row["id"])
        doc = docs.setdefault(
            did,
            {
                "document_id": did,
                "filename": row["filename"],
                "kind": row["kind"],
                "page_count": row["page_count"],
                "parts": [],
            },
        )
        if row["part_id"] is not None:
            doc["parts"].append(
                {
                    "part_id": str(row["part_id"]),
                    "kind": row["part_kind"],
                    "label": row["label"],
                    "ranges": [[row["first_pdf_page"], row["last_pdf_page"]]],
                }
            )
    for did, doc in docs.items():
        parts = doc["parts"]
        if not parts:
            parts.append(
                {
                    "part_id": did,
                    "kind": doc["kind"],
                    "label": None,
                    "ranges": [[1, doc["page_count"]]],
                }
            )
            continue
        ranges = []
        start = 1
        for part in parts:
            first, last = part["ranges"][0]
            if start < first:
                ranges.append([start, first - 1])
            start = max(start, last + 1)
        if start <= doc["page_count"]:
            ranges.append([start, doc["page_count"]])
        if ranges:
            parts.append(
                {
                    "part_id": did + ":rest",
                    "kind": doc["kind"],
                    "label": "Rest of document",
                    "ranges": ranges,
                }
            )
    return list(docs.values())
