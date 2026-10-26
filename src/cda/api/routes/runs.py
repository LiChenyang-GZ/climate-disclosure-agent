"""Queued run registration and reporting."""

from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from cda.agent.prompts import prompt_sha256
from cda.api import deps
from cda.api.deps import Actor, Conn, audit
from cda.api.errors import APIError, run_errors
from cda.api.routes.packs import document_info
from cda.checks.loader import default_check_set
from cda.db.models import CheckSet, Document, Finding, LLMCall, Pack, Run
from cda.llm.cost import ModelTable

router = APIRouter()


class RunInput(BaseModel):
    pack_id: UUID
    method: Literal["A", "B", "C"]
    model_key: str
    budget_usd: Decimal = Field(
        default=Decimal("2.0"), gt=0, le=10, decimal_places=6, allow_inf_nan=False
    )


async def wake(conn: Conn) -> None:
    await conn.execute(text("SELECT pg_notify('cda_runs', '')"))


@router.post("/runs", status_code=201)
async def create_run(
    body: RunInput,
    response: Response,
    request: Request,
    conn: Conn,
    actor: Actor,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    if idempotency_key is None or not 1 <= len(idempotency_key) <= 200:
        raise APIError(
            400, "invalid_idempotency_key", "Idempotency-Key is required (1-200 characters)"
        )
    if body.model_key not in ModelTable.load().root:
        raise APIError(422, "invalid_model", "Unknown model key")
    await deps.visible_pack(conn, actor, body.pack_id)
    # Serialize equal keys before checking, so concurrent retries cannot race the unique index.
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": idempotency_key}
    )
    old = (
        (
            await conn.execute(
                deps.visible(select(Run).join(Pack, Pack.id == Run.pack_id), actor["id"]).where(
                    Run.idempotency_key == idempotency_key
                )
            )
        )
        .mappings()
        .first()
    )
    if old:
        if any(old[k] != value for k, value in body.model_dump().items()):
            raise APIError(409, "idempotency_conflict", "Key already used with a different request")
        response.status_code = 200
        return run_errors(dict(old))
    await conn.execute(
        deps.visible(select(Pack.id), actor["id"])
        .where(Pack.id == body.pack_id)
        .with_for_update(of=Pack)
    )
    docs = (
        (
            await conn.execute(
                deps.visible(
                    select(Document).join(Pack, Pack.id == Document.pack_id), actor["id"]
                ).where(Document.pack_id == body.pack_id, Document.deleted_at.is_(None))
            )
        )
        .mappings()
        .all()
    )
    if not docs or any(
        [
            (await document_info(request, conn, dict(d), actor))["index_status"] != "done"
            for d in docs
        ]
    ):
        raise APIError(409, "pack_not_indexed", "Index every document before starting a run")
    checks = default_check_set()
    await conn.execute(
        pg_insert(CheckSet)
        .values(version=checks.version, yaml_sha256=checks.sha256)
        .on_conflict_do_nothing(index_elements=["version"])
    )
    row = (
        (
            await conn.execute(
                pg_insert(Run)
                .values(
                    **body.model_dump(),
                    status="queued",
                    check_set_version=checks.version,
                    prompt_sha256=prompt_sha256(),
                    created_by=actor["id"],
                    idempotency_key=idempotency_key,
                )
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
                .returning(Run)
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise APIError(409, "idempotency_conflict", "Idempotency key unavailable")
    await audit(conn, actor, "create_run", row["id"])
    await wake(conn)
    return dict(row)


@router.get("/runs/{rid}")
async def get_run(rid: UUID, conn: Conn, actor: Actor) -> dict[str, Any]:
    row = await deps.visible_run(conn, actor, rid)
    counts = (
        (
            await conn.execute(
                deps.visible(
                    select(Finding.status, func.count().label("count"))
                    .join(Run, Run.id == Finding.run_id)
                    .join(Pack, Pack.id == Run.pack_id),
                    actor["id"],
                )
                .where(Run.id == rid)
                .group_by(Finding.status)
            )
        )
        .mappings()
        .all()
    )
    totals = (
        (
            await conn.execute(
                deps.visible(
                    select(
                        func.coalesce(func.sum(LLMCall.tokens_in), 0).label("tokens_in"),
                        func.coalesce(func.sum(LLMCall.tokens_out), 0).label("tokens_out"),
                        func.coalesce(func.sum(LLMCall.latency_ms), 0).label("latency_ms"),
                    )
                    .join(Run, Run.id == LLMCall.run_id)
                    .join(Pack, Pack.id == Run.pack_id),
                    actor["id"],
                ).where(Run.id == rid)
            )
        )
        .mappings()
        .one()
    )
    return {
        **row,
        **dict(totals),
        "counts": {c["status"]: c["count"] for c in counts},
        "timings": {"started_at": row["started_at"], "finished_at": row["finished_at"]},
    }


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
