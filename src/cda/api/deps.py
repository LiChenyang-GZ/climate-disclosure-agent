"""Connection, demo identity and membership dependencies."""

from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Header, Request
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql import Select

from cda.api.errors import APIError, run_errors
from cda.db.models import AuditLog, Finding, Pack, Run, TeamMember, User
from cda.db.session import make_engine
from cda.tools.permissions import visible_pack_clause


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
        user = (
            (await conn.execute(select(User).where(User.handle == x_demo_user))).mappings().first()
        )
    if user is None:
        raise APIError(401, "unauthorized", "Select a demo user")
    return dict(user)


async def connection(
    request: Request, actor: Annotated[dict[str, Any], Depends(identity)]
) -> AsyncIterator[AsyncConnection]:
    async with engine_for(request).begin() as conn:
        yield conn


Conn = Annotated[AsyncConnection, Depends(connection, scope="function")]
Actor = Annotated[dict[str, Any], Depends(identity)]


def lead(actor: dict[str, Any]) -> None:
    if actor["role"] != "lead":
        raise APIError(403, "forbidden", "Lead role required")


def visible[*Ts](stmt: Select[*Ts], user_id: UUID) -> Select[*Ts]:
    return stmt.join(TeamMember, visible_pack_clause(user_id))


async def visible_pack(conn: AsyncConnection, actor: dict[str, Any], pid: UUID) -> dict[str, Any]:
    row = (
        (await conn.execute(visible(select(Pack), actor["id"]).where(Pack.id == pid)))
        .mappings()
        .first()
    )
    if row is None:
        raise APIError(404, "not_found", "Resource not found")
    return dict(row)


async def visible_run(
    conn: AsyncConnection, actor: dict[str, Any], rid: UUID, *, lock: bool = False
) -> dict[str, Any]:
    stmt = visible(select(Run).join(Pack, Pack.id == Run.pack_id), actor["id"]).where(Run.id == rid)
    if lock:
        stmt = stmt.with_for_update(of=Run)
    row = (await conn.execute(stmt)).mappings().first()
    if row is None:
        raise APIError(404, "not_found", "Resource not found")
    return run_errors(dict(row))


async def visible_finding(
    conn: AsyncConnection, actor: dict[str, Any], fid: UUID, *, lock: bool = False
) -> dict[str, Any]:
    stmt = visible(
        select(Finding).join(Run, Run.id == Finding.run_id).join(Pack, Pack.id == Run.pack_id),
        actor["id"],
    ).where(Finding.id == fid)
    if lock:
        stmt = stmt.with_for_update(of=Finding)
    row = (await conn.execute(stmt)).mappings().first()
    if row is None:
        raise APIError(404, "not_found", "Resource not found")
    return dict(row)


async def audit(
    conn: AsyncConnection, actor: dict[str, Any], action: str, target: object, **detail: Any
) -> None:
    await conn.execute(
        insert(AuditLog).values(
            actor=actor["handle"], action=action, target=str(target), detail=detail
        )
    )
