"""Serialize queue claims across all worker processes."""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from cda.db.models import Finding, Review, Run

# Stable signed bigint shared by every worker; transaction scoped.
_CLAIM_LOCK = 0x4344415F434C4149


async def claim_next(engine: AsyncEngine, worker_id: str, lease_seconds: int) -> UUID | None:
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _CLAIM_LOCK})
        live = and_(Run.lease_owner.is_not(None), Run.lease_expires_at > func.now())
        if await conn.scalar(select(Run.id).where(live).limit(1)) is not None:
            return None
        pending = (
            select(Finding.id)
            .where(
                Finding.run_id == Run.id,
                Finding.status == "needs_review",
                ~select(Review.id).where(Review.finding_id == Finding.id).exists(),
            )
            .exists()
        )
        rid = await conn.scalar(
            select(Run.id)
            .where(
                or_(
                    Run.status == "queued",
                    and_(
                        Run.status == "running",
                        or_(Run.lease_expires_at.is_(None), Run.lease_expires_at <= func.now()),
                    ),
                    and_(Run.status == "waiting_review", ~pending),
                )
            )
            .order_by(Run.created_at, Run.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if rid is not None:
            await conn.execute(
                update(Run)
                .where(Run.id == rid)
                .values(
                    lease_owner=worker_id,
                    lease_expires_at=func.now() + timedelta(seconds=lease_seconds),
                )
            )
        return rid
