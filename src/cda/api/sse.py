"""Durable SSE replay and LISTEN/NOTIFY wakeups."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from fastapi import Request
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse

from cda.api import deps
from cda.db.models import Pack, Run, RunEvent

TERMINAL_EVENTS = {"run_final", "run_failed", "run_cancelled"}


def event_response(
    request: Request, rid: UUID, actor: dict[str, Any], last: int, status: str
) -> EventSourceResponse:
    async def stream() -> AsyncIterator[dict[str, str]]:
        engine = deps.engine_for(request)
        raw = await engine.raw_connection()
        listener = raw.driver_connection
        if listener is None:
            raw.close()
            return
        wake = asyncio.Event()

        def notified(*args: Any) -> None:
            wake.set()

        channel = "run_" + rid.hex
        cursor = last
        try:
            # Listen before replay, closing the query/subscribe race.
            await listener.add_listener(channel, notified)
            while not await request.is_disconnected():
                wake.clear()
                async with engine.connect() as conn:
                    rows = (
                        (
                            await conn.execute(
                                deps.visible(
                                    select(RunEvent)
                                    .join(Run, Run.id == RunEvent.run_id)
                                    .join(Pack, Pack.id == Run.pack_id),
                                    actor["id"],
                                )
                                .where(RunEvent.run_id == rid, RunEvent.id > cursor)
                                .order_by(RunEvent.id)
                            )
                        )
                        .mappings()
                        .all()
                    )
                    current = await deps.visible_run(conn, actor, rid)
                for row in rows:
                    cursor = row["id"]
                    yield {
                        "id": str(cursor),
                        "event": row["type"],
                        "data": json.dumps(row["payload"], ensure_ascii=False),
                    }
                    if row["type"] in TERMINAL_EVENTS:
                        return
                if current["status"] in ("final", "failed", "cancelled"):
                    return
                try:
                    await asyncio.wait_for(wake.wait(), timeout=15)
                except TimeoutError:
                    pass
        finally:
            try:
                await listener.remove_listener(channel, notified)
            finally:
                raw.close()

    return EventSourceResponse(stream(), ping=15)
