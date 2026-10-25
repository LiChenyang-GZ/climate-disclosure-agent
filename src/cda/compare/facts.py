"""Load verified facts with their current logical part membership."""

from typing import Any
from uuid import UUID

from sqlalchemy import and_, exists, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cda.db.models import Document, DocumentPart, Evidence, Fact, Finding, Unit
from cda.tools.parts_sql import part_id, part_kind


def location_join() -> Any:
    return and_(
        DocumentPart.document_id == Document.id,
        Unit.pdf_page.between(DocumentPart.first_pdf_page, DocumentPart.last_pdf_page),
    )


async def load_facts(conn: AsyncConnection, run_id: UUID) -> list[dict[str, Any]]:
    verified = exists(
        select(Evidence.id).where(
            Evidence.finding_id == Fact.finding_id,
            Evidence.unit_id == Fact.unit_id,
            Evidence.verified.is_(True),
        )
    ).correlate(Fact)
    stmt = (
        select(
            Fact.__table__,
            Finding.check_id,
            Finding.status,
            Unit.document_id,
            Unit.pdf_page,
            part_id,
            part_kind,
        )
        .select_from(Fact)
        .join(Finding, Fact.finding_id == Finding.id)
        .join(Unit, Fact.unit_id == Unit.id)
        .join(Document, Unit.document_id == Document.id)
        .outerjoin(DocumentPart, location_join())
        .where(Finding.run_id == run_id, verified)
        .order_by(Finding.check_id, Fact.id)
    )
    return [dict(row) for row in (await conn.execute(stmt)).mappings()]


async def load_evidence(conn: AsyncConnection, finding_id: UUID) -> list[dict[str, Any]]:
    stmt = (
        select(Evidence.__table__, Unit.document_id, Unit.pdf_page, part_id, part_kind)
        .select_from(Evidence)
        .join(Unit, Evidence.unit_id == Unit.id)
        .join(Document, Unit.document_id == Document.id)
        .outerjoin(DocumentPart, location_join())
        .where(Evidence.finding_id == finding_id, Evidence.verified.is_(True))
        .order_by(Evidence.role, Evidence.id)
    )
    return [dict(row) for row in (await conn.execute(stmt)).mappings()]
