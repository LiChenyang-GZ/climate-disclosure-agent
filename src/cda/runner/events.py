"""Committed progress rows and transactional notifications."""

from typing import Any
from uuid import UUID

from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncEngine

from cda.db.models import RunEvent


class EventSink:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def emit(
        self, run_id: UUID, check_id: str | None, type: str, payload: dict[str, Any]
    ) -> None:
        async with self.engine.begin() as conn:
            event_id = (
                await conn.execute(
                    insert(RunEvent)
                    .values(run_id=run_id, check_id=check_id, type=type, payload=payload)
                    .returning(RunEvent.id)
                )
            ).scalar_one()
            await conn.execute(
                text("SELECT pg_notify(:channel, :payload)"),
                {"channel": "run_" + run_id.hex, "payload": str(event_id)},
            )
