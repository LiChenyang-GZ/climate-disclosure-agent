from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import select, true
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.orm import aliased

from cda.db.models import Document, Pack, TeamMember, Unit
from cda.tools.permissions import visible_pack_clause
from cda.tools.rls import tool_scope
from cda.verify.quotes import match_quote

Role = Literal["supports", "side_a", "side_b"]


@dataclass(frozen=True)
class EvidenceItem:
    unit_id: UUID
    quote: str
    role: Role


@dataclass(frozen=True)
class EvidenceCheck(EvidenceItem):
    ok: bool
    reason: str
    spans_next: bool
    pdf_page: int | None
    document_id: UUID | None


async def verify_evidence(
    conn: AsyncConnection,
    *,
    user_id: UUID,
    pack_id: UUID,
    items: Sequence[EvidenceItem],
    timeout_s: float = 10.0,
) -> list[EvidenceCheck]:
    async with tool_scope(conn, user_id, timeout_s):
        base = (
            select(Unit.id, Unit.text, Unit.pdf_page, Unit.document_id)
            .select_from(Unit)
            .join(Document, Document.id == Unit.document_id)
            .join(Pack, Pack.id == Document.pack_id)
            .join(TeamMember, visible_pack_clause(user_id))
            .where(Pack.id == pack_id, Document.deleted_at.is_(None))
        )
        cited = {
            row["id"]: row
            for row in (
                await conn.execute(base.where(Unit.id.in_({i.unit_id for i in items})))
            ).mappings()
        }
        anchor = aliased(Unit)
        next_unit = (
            base.where(
                Unit.document_id == anchor.document_id,
                Unit.seq > anchor.seq,
                Unit.kind.in_(("paragraph", "table_row")),
            )
            .order_by(Unit.seq)
            .limit(1)
            .correlate(anchor)
            .lateral()
        )
        next_query = (
            select(anchor.id.label("anchor_id"), next_unit.c.text)
            .select_from(anchor)
            .join(next_unit, true())
            .where(anchor.id.in_(cited))
        )
        following = {
            row["anchor_id"]: row["text"] for row in (await conn.execute(next_query)).mappings()
        }
        results = []
        for item in items:
            row = cited.get(item.unit_id)
            if row is None:
                results.append(
                    EvidenceCheck(
                        item.unit_id,
                        item.quote,
                        item.role,
                        False,
                        "unit_not_found",
                        False,
                        None,
                        None,
                    )
                )
                continue
            match = match_quote(item.quote, row["text"], following.get(item.unit_id))
            results.append(
                EvidenceCheck(
                    item.unit_id,
                    item.quote,
                    item.role,
                    match.ok,
                    match.reason,
                    match.spans_next,
                    row["pdf_page"],
                    row["document_id"],
                )
            )
        return results
