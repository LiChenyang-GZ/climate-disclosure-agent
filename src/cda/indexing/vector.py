"""Cosine retrieval. Callers supply authorized document IDs; no permission checks here."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from cda.db.models import Document, Unit


@dataclass(frozen=True)
class Hit:
    unit_id: UUID
    score: float


async def vector_units(
    conn: AsyncConnection,
    *,
    document_ids: list[UUID],
    query_embedding: list[float],
    limit: int = 10,
) -> list[Hit]:
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    distance = Unit.embedding.cosine_distance(query_embedding)
    score = (1 - distance).label("score")
    result = await conn.execute(
        select(Unit.id, score)
        .join(Document, Unit.document_id == Document.id)
        .where(
            Document.id.in_(document_ids),
            Document.deleted_at.is_(None),
            Unit.kind.in_(["paragraph", "table_row"]),
            Unit.embedding.is_not(None),
        )
        .order_by(distance, Unit.id)
        .limit(limit)
    )
    return [Hit(row.id, float(row.score)) for row in result]
