"""Index units. Callers supply authorized document IDs; no permission checks here."""

import asyncio
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from sqlalchemy import Table, bindparam, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cda.db.models import Document, Unit
from cda.indexing.context_header import context_header
from cda.indexing.embed import Embedder

_BATCH_SIZE = 32


@dataclass(frozen=True)
class IndexStats:
    headers_set: int
    embedded: int


async def index_document(
    conn: AsyncConnection, document_id: UUID, embedder: Embedder
) -> IndexStats:
    if embedder.dim != 384:
        raise ValueError("unit embeddings must have 384 dimensions")
    table = cast(Table, Unit.__table__)
    rows = (
        (
            await conn.execute(
                select(table, Document.filename)
                .join(Document, Unit.document_id == Document.id)
                .where(
                    Unit.document_id == document_id,
                    or_(
                        Unit.context_header.is_(None),
                        Unit.kind.in_(["paragraph", "table_row"]) & Unit.embedding.is_(None),
                    ),
                )
                .order_by(Unit.id)
            )
        )
        .mappings()
        .all()
    )
    headers_set = embedded = 0
    for start in range(0, len(rows), _BATCH_SIZE):
        batch = rows[start : start + _BATCH_SIZE]
        headers = {
            row["id"]: row["context_header"]
            if row["context_header"] is not None
            else context_header(row["filename"], row["section_path"])
            for row in batch
        }
        children = [
            row
            for row in batch
            if row["kind"] in ("paragraph", "table_row") and row["embedding"] is None
        ]
        vectors = (
            await asyncio.to_thread(
                embedder.embed_passages,
                [f"{headers[row['id']]}\n{row['text']}" for row in children],
            )
            if children
            else []
        )
        by_id = dict(zip([row["id"] for row in children], vectors, strict=True))
        values = [
            {
                "unit_id": row["id"],
                "new_header": headers[row["id"]],
                "new_embedding": by_id.get(row["id"], row["embedding"]),
            }
            for row in batch
        ]
        await conn.execute(
            update(table)
            .where(table.c.id == bindparam("unit_id"))
            .values(context_header=bindparam("new_header"), embedding=bindparam("new_embedding")),
            values,
        )
        headers_set += sum(row["context_header"] is None for row in batch)
        embedded += len(children)
    return IndexStats(headers_set, embedded)
