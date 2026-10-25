"""Keyword retrieval. Callers supply authorized document IDs; no permission checks here."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cda.db.models import Document, Unit


@dataclass(frozen=True)
class Hit:
    unit_id: UUID
    rank: float


async def keyword_units(
    conn: AsyncConnection, *, document_ids: list[UUID], query: str, limit: int = 10
) -> list[Hit]:
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    q = func.websearch_to_tsquery(literal_column("'english'"), query)
    rank = func.ts_rank_cd(Unit.tsv, q).label("rank")
    result = await conn.execute(
        select(Unit.id, rank)
        .join(Document, Unit.document_id == Document.id)
        .where(
            Document.id.in_(document_ids),
            Document.deleted_at.is_(None),
            Unit.kind.in_(["paragraph", "table_row"]),
            Unit.tsv.op("@@")(q),
        )
        .order_by(rank.desc(), Unit.id)
        .limit(limit)
    )
    return [Hit(row.id, float(row.rank)) for row in result]
