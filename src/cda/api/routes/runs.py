"""Queued run registration and reporting."""

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response
from sqlalchemy import update

from cda.api import deps, service
from cda.api.deps import Actor, Conn, audit
from cda.api.errors import APIError
from cda.api.service import RunInput as RunInput  # noqa: PLC0414
from cda.api.service import wake as wake  # noqa: PLC0414
from cda.db.models import Run

router = APIRouter()


@router.post("/runs", status_code=201)
async def create_run(
    body: RunInput,
    response: Response,
    request: Request,
    conn: Conn,
    actor: Actor,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    row, created = await service.create_run(
        conn, actor, body, idempotency_key, request.app.state.index_state
    )
    if not created:
        response.status_code = 200
    return row


@router.get("/runs/{rid}")
async def get_run(rid: UUID, conn: Conn, actor: Actor) -> dict[str, Any]:
    return await service.get_run(conn, actor, rid)


@router.post("/runs/{rid}/cancel")
async def cancel(rid: UUID, conn: Conn, actor: Actor) -> dict[str, Any]:
    row = await deps.visible_run(conn, actor, rid, lock=True)
    if row["status"] not in ("queued", "running", "waiting_review"):
        raise APIError(409, "invalid_run_state", "Run cannot be cancelled")
    await conn.execute(update(Run).where(Run.id == rid).values(status="cancelled"))
    await audit(conn, actor, "cancel_run", rid)
    await wake(conn)
    return {**row, "status": "cancelled"}


@router.get("/runs/{rid}/findings")
async def list_findings(rid: UUID, conn: Conn, actor: Actor) -> list[dict[str, Any]]:
    from cda.api.routes.findings import finding_rows

    await deps.visible_run(conn, actor, rid)
    return await finding_rows(conn, actor, rid)


@router.get("/runs/{rid}/events")
async def events(
    rid: UUID,
    request: Request,
    conn: Conn,
    actor: Actor,
    last_event_id: int = 0,
    last_header: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> Response:
    from cda.api.sse import event_response

    row = await deps.visible_run(conn, actor, rid)
    try:
        last = int(last_header) if last_header is not None else last_event_id
        if last < 0:
            raise ValueError
    except ValueError:
        raise APIError(400, "invalid_event_id", "Invalid event cursor") from None
    return event_response(request, rid, actor, last, row["status"])


@router.get("/runs/{rid}/export")
async def export(
    rid: UUID, conn: Conn, actor: Actor, format: Literal["md", "json", "csv"] = "md"
) -> Response:
    from cda.api.export import export_response

    row = await deps.visible_run(conn, actor, rid)
    return await export_response(conn, actor, row, format)
