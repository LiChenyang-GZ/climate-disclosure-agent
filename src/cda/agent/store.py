"""Store a finding and its children atomically in a savepoint."""

from uuid import UUID

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from cda.agent.state import WorkerResult
from cda.db.models import Evidence, Fact, Finding


async def store_finding(conn: AsyncConnection, run_id: UUID, result: WorkerResult) -> UUID:
    async with conn.begin_nested():
        fid: UUID = (
            await conn.execute(
                insert(Finding)
                .values(
                    run_id=run_id,
                    check_id=result.check_id,
                    status=result.status,
                    confidence=result.confidence,
                    rationale=result.rationale,
                    missing_aspects=result.missing_aspects,
                    relief_applied=result.relief_applied,
                    needs_review_reason=result.needs_review_reason,
                    steps=result.steps,
                    tokens_in=result.tokens_in,
                    tokens_out=result.tokens_out,
                    cost_usd=result.cost_usd,
                )
                .returning(Finding.id)
            )
        ).scalar_one()
        visible_ids = {e.unit_id for e in result.evidence if e.reason != "unit_not_found"}
        for e in result.evidence:
            if e.unit_id in visible_ids:
                await conn.execute(
                    insert(Evidence).values(
                        finding_id=fid, unit_id=e.unit_id, quote=e.quote, role=e.role, verified=e.ok
                    )
                )
        for f in result.facts:
            if UUID(f.unit_id) in visible_ids:
                await conn.execute(
                    insert(Fact).values(
                        finding_id=fid,
                        unit_id=UUID(f.unit_id),
                        name=f.name,
                        value_num=f.value,
                        unit=f.unit or "",
                        scope=f.scope or "",
                        year=f.year,
                        raw=f.raw,
                    )
                )
        return fid
