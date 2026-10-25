"""Extension points for comparison and run events."""

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

Comparer = Callable[[AsyncConnection, UUID], Awaitable[None]]
EventSink = Callable[[UUID, str | None, str, dict[str, Any]], Awaitable[None]]


async def noop_comparer(conn: AsyncConnection, run_id: UUID) -> None:
    pass


async def noop_sink(run_id: UUID, check_id: str | None, type: str, payload: dict[str, Any]) -> None:
    pass
