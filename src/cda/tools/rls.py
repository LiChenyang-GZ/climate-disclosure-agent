"""Transaction-local database permissions for read-only tools."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


@asynccontextmanager
async def tool_scope(conn: AsyncConnection, user_id: UUID, timeout_s: float) -> AsyncIterator[None]:
    savepoint = await conn.begin_nested()
    try:
        await conn.execute(text("SET LOCAL ROLE cda_tool"))
        await conn.execute(
            text("SELECT set_config('cda.user_id', :uid, true)"), {"uid": str(user_id)}
        )
        # PostgreSQL interprets zero as unlimited; keep sub-millisecond limits bounded.
        milliseconds = max(1, int(timeout_s * 1000))
        await conn.execute(text(f"SET LOCAL statement_timeout = {milliseconds}"))
        yield
    finally:
        await savepoint.rollback()
