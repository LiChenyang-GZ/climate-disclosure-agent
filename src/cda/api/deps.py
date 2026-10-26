"""Connection, demo identity and membership dependencies."""

from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from cda.api.errors import APIError
from cda.api.service import audit as audit  # noqa: PLC0414
from cda.api.service import lead as lead  # noqa: PLC0414
from cda.api.service import load_actor
from cda.api.service import visible as visible  # noqa: PLC0414
from cda.api.service import visible_finding as visible_finding  # noqa: PLC0414
from cda.api.service import visible_pack as visible_pack  # noqa: PLC0414
from cda.api.service import visible_run as visible_run  # noqa: PLC0414
from cda.db.session import make_engine


def engine_for(request: Request) -> AsyncEngine:
    engine: AsyncEngine | None = request.app.state.engine
    if engine is None:
        engine = make_engine(request.app.state.settings.database_url)
        request.app.state.engine = engine
    return engine


async def identity(
    request: Request, x_demo_user: Annotated[str | None, Header()] = None
) -> dict[str, Any]:
    if x_demo_user not in ("sarah", "alex"):
        raise APIError(401, "unauthorized", "Select a demo user")
    async with engine_for(request).connect() as conn:
        return await load_actor(conn, x_demo_user)


async def connection(
    request: Request, actor: Annotated[dict[str, Any], Depends(identity)]
) -> AsyncIterator[AsyncConnection]:
    async with engine_for(request).begin() as conn:
        yield conn


Conn = Annotated[AsyncConnection, Depends(connection, scope="function")]
Actor = Annotated[dict[str, Any], Depends(identity)]
