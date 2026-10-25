"""Lease heartbeat and owner-conditional cleanup."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncEngine

from cda.db.models import Run

logger = logging.getLogger(__name__)


@dataclass
class Lease:
    lost: asyncio.Event = field(default_factory=asyncio.Event)


@asynccontextmanager
async def hold_lease(
    engine: AsyncEngine, run_id: UUID, worker_id: str, lease_seconds: int, heartbeat_seconds: float
) -> AsyncIterator[Lease]:
    lease = Lease()
    owner_task = asyncio.current_task()
    owned = (Run.id == run_id, Run.lease_owner == worker_id)

    async def heartbeat() -> None:
        try:
            while True:
                await asyncio.sleep(heartbeat_seconds)
                async with engine.begin() as conn:
                    result = await conn.execute(
                        update(Run)
                        .where(*owned)
                        .values(lease_expires_at=func.now() + timedelta(seconds=lease_seconds))
                    )
                if result.rowcount == 0:
                    break
        except Exception as exc:  # noqa: BLE001 - retain worker safety with safe type-only logs
            logger.error("heartbeat failed %s", type(exc).__name__)
        lease.lost.set()
        logger.warning("lost lease %s", run_id)
        if owner_task is not None:
            owner_task.cancel()

    task = asyncio.create_task(heartbeat())
    try:
        yield lease
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await release_lease(engine, run_id, worker_id)


async def release_lease(engine: AsyncEngine, run_id: UUID, worker_id: str) -> None:
    """Also used when shutdown cancels a claimed task before it starts."""
    async with engine.begin() as conn:
        await conn.execute(
            update(Run)
            .where(Run.id == run_id, Run.lease_owner == worker_id)
            .values(lease_owner=None, lease_expires_at=None)
        )
