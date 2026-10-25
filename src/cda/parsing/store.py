"""Atomic insertion of parsed documents and immutable units."""

import asyncio
from dataclasses import asdict
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from cda.db.enums import DocumentKind
from cda.db.models import Document, Unit
from cda.parsing.pdf import parse_pdf


async def ingest_pdf(
    conn: AsyncConnection, *, pack_id: UUID, kind: DocumentKind, path: str | Path
) -> UUID:
    parsed = await asyncio.to_thread(parse_pdf, path)
    async with conn.begin_nested():
        document_id = (
            await conn.execute(
                insert(Document)
                .values(
                    pack_id=pack_id,
                    kind=kind,
                    filename=Path(path).name,
                    sha256=parsed.sha256,
                    page_count=parsed.page_count,
                    parser_version=parsed.parser_version,
                )
                .returning(Document.id)
            )
        ).scalar_one()
        unit_ids = {unit.key: uuid4() for unit in parsed.units}
        rows = []
        for seq, unit in enumerate(parsed.units):
            values = asdict(unit)
            key = values.pop("key")
            parent = values.pop("parent_key")
            rows.append(
                dict(
                    **values,
                    seq=seq,
                    id=unit_ids[key],
                    document_id=document_id,
                    parent_id=unit_ids[parent] if parent else None,
                    parser_version=parsed.parser_version,
                )
            )
        if rows:
            await conn.execute(insert(Unit), rows)
    return document_id
