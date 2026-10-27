"""Demo identity and team routes used by the web UI."""

from typing import Any

from fastapi import APIRouter

from cda.api import service
from cda.api.deps import Actor, Conn

router = APIRouter()


@router.get("/me")
async def me(actor: Actor) -> dict[str, Any]:
    return {key: actor[key] for key in ("handle", "role", "display_name")}


@router.get("/teams")
async def teams(conn: Conn, actor: Actor) -> list[dict[str, Any]]:
    return await service.list_teams(conn, actor)
