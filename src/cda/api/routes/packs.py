"""Pack, PDF and indexing routes."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from cda.api import deps, service
from cda.api.deps import Actor, Conn, audit, lead
from cda.api.errors import APIError, run_errors
from cda.api.storage import document_path, safe_filename
from cda.db.enums import DocumentKind
from cda.db.models import Document, Pack, Run, TeamMember
from cda.indexing.index import index_document
from cda.parsing.parts import Part, replace_parts
from cda.parsing.pdf import ParseError
from cda.parsing.store import ingest_pdf

router = APIRouter()

MAX_DOCUMENTS = 20
ACTIVE_RUN = ("queued", "running")


class PackInput(BaseModel):
    company: str = Field(min_length=1, max_length=500)
    reporting_year: int = Field(ge=2000, le=2100)
    team_id: UUID


async def document(conn: Conn, actor: Actor, pid: UUID, did: UUID) -> dict[str, Any]:
    row = (
        (
            await conn.execute(
                deps.visible(
                    select(Document).join(Pack, Pack.id == Document.pack_id), actor["id"]
                ).where(Document.id == did, Document.pack_id == pid, Document.deleted_at.is_(None))
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise APIError(404, "not_found", "Resource not found")
    return dict(row)


async def document_info(
    request: Request, conn: Conn, row: dict[str, Any], actor: Actor
) -> dict[str, Any]:
    return await service.document_info(request.app.state.index_state, conn, row, actor)


@router.post("/packs", status_code=201)
async def create_pack(body: PackInput, conn: Conn, actor: Actor) -> dict[str, Any]:
    lead(actor)
    membership = (
        await conn.execute(
            select(TeamMember.team_id).where(
                TeamMember.team_id == body.team_id, TeamMember.user_id == actor["id"]
            )
        )
    ).first()
    if membership is None:
        raise APIError(404, "not_found", "Team not found")
    company = " ".join(body.company.split())
    # Serialise creation per company and year so concurrent duplicates cannot both pass.
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 1))"),
        {"key": f"pack:{company.lower()}:{body.reporting_year}"},
    )
    existing = (
        await conn.execute(
            select(Pack.id).where(
                func.lower(func.regexp_replace(func.btrim(Pack.company), r"\s+", " ", "g"))
                == company.lower(),
                Pack.reporting_year == body.reporting_year,
            )
        )
    ).first()
    if existing is not None:
        raise APIError(
            409,
            "duplicate_pack",
            "A pack for this company and year already exists",
            pack_id=str(existing[0]),
        )
    row = (
        (
            await conn.execute(
                insert(Pack)
                .values(**(body.model_dump() | {"company": company}), created_by=actor["id"])
                .returning(Pack)
            )
        )
        .mappings()
        .one()
    )
    await audit(conn, actor, "create_pack", row["id"])
    return dict(row)


@router.get("/packs")
async def list_packs(request: Request, conn: Conn, actor: Actor) -> list[dict[str, Any]]:
    return await service.list_packs(conn, actor, request.app.state.index_state)


async def active_run(conn: Conn, actor: Actor, pid: UUID) -> UUID | None:
    return (
        await conn.execute(
            deps.visible(select(Run.id).join(Pack, Pack.id == Run.pack_id), actor["id"])
            .where(Run.pack_id == pid, Run.status.in_(ACTIVE_RUN))
            .order_by(Run.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


@router.get("/packs/{pid}")
async def get_pack(pid: UUID, request: Request, conn: Conn, actor: Actor) -> dict[str, Any]:
    row = await deps.visible_pack(conn, actor, pid)
    documents = await service.pack_documents(request.app.state.index_state, conn, actor, pid)
    runs = (
        (
            await conn.execute(
                deps.visible(select(Run).join(Pack, Pack.id == Run.pack_id), actor["id"])
                .where(Run.pack_id == pid)
                .order_by(Run.created_at.desc())
                .limit(10)
            )
        )
        .mappings()
        .all()
    )
    return {
        **row,
        "documents": documents,
        "latest_runs": [run_errors(dict(r)) for r in runs],
        "status": service.pack_status([d["index_status"] for d in documents]),
        "team": (await service.list_teams(conn, actor, row["team_id"]))[0],
        "limits": {
            "max_upload_mb": request.app.state.settings.cda_max_upload_mb,
            "max_documents": MAX_DOCUMENTS,
        },
        "active_run_id": await active_run(conn, actor, pid),
    }


@router.post("/packs/{pid}/documents", status_code=201)
async def upload_document(
    pid: UUID,
    request: Request,
    conn: Conn,
    actor: Actor,
    file: Annotated[UploadFile, File()],
    kind: Annotated[DocumentKind, Form()],
    parts: Annotated[str, Form()] = "[]",
) -> dict[str, Any]:
    await deps.visible_pack(conn, actor, pid)
    lead(actor)
    limit = request.app.state.settings.cda_max_upload_mb * 1024 * 1024
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise APIError(413, "upload_too_large", "Upload exceeds size limit")
    if not content.startswith(b"%PDF-"):
        raise APIError(415, "invalid_pdf", "A PDF file is required")
    try:
        validated = TypeAdapter(list[Part]).validate_python(json.loads(parts))
    except (ValueError, ValidationError):
        raise APIError(422, "invalid_parts", "Invalid document parts") from None
    digest = hashlib.sha256(content).hexdigest()
    # Serialise uploads against duplicate digest and soft deletion in this pack.
    await conn.execute(
        deps.visible(select(Pack.id), actor["id"]).where(Pack.id == pid).with_for_update(of=Pack)
    )
    duplicate = (
        await conn.execute(
            deps.visible(
                select(Document.id).join(Pack, Pack.id == Document.pack_id), actor["id"]
            ).where(
                Document.pack_id == pid, Document.sha256 == digest, Document.deleted_at.is_(None)
            )
        )
    ).first()
    if duplicate:
        raise APIError(409, "duplicate_document", "Document already exists in pack")
    if pid in request.app.state.indexing:
        raise APIError(409, "pack_parsing", "Wait for parsing to finish before uploading")
    held = (
        await conn.execute(
            select(func.count())
            .select_from(Document)
            .where(Document.pack_id == pid, Document.deleted_at.is_(None))
        )
    ).scalar_one()
    if held >= MAX_DOCUMENTS:
        raise APIError(409, "pack_full", f"A pack can hold up to {MAX_DOCUMENTS} files")
    path = document_path(request.app.state.settings.cda_upload_dir, pid, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(content)
    try:
        did = await ingest_pdf(conn, pack_id=pid, kind=kind, path=path)
        await replace_parts(conn, did, validated)
    except ParseError as exc:
        raise APIError(422, exc.code, "PDF could not be parsed") from None
    except ValueError:
        raise APIError(422, "invalid_parts", "Invalid document parts") from None
    except IntegrityError:
        raise APIError(409, "duplicate_document", "Document already exists in pack") from None
    await conn.execute(
        update(Document).where(Document.id == did).values(filename=safe_filename(file.filename))
    )
    await audit(conn, actor, "upload_document", did, pack_id=str(pid))
    return await document_info(request, conn, await document(conn, actor, pid, did), actor)


@router.put("/packs/{pid}/documents/{did}/parts")
async def set_parts(
    pid: UUID, did: UUID, body: list[Part], request: Request, conn: Conn, actor: Actor
) -> dict[str, Any]:
    await document(conn, actor, pid, did)
    lead(actor)
    try:
        await replace_parts(conn, did, body)
    except ValueError:
        raise APIError(422, "invalid_parts", "Invalid document parts") from None
    await audit(conn, actor, "set_parts", did)
    return await document_info(request, conn, await document(conn, actor, pid, did), actor)


@router.delete("/packs/{pid}/documents/{did}")
async def delete_document(pid: UUID, did: UUID, conn: Conn, actor: Actor) -> dict[str, str]:
    await document(conn, actor, pid, did)
    lead(actor)
    if await active_run(conn, actor, pid) is not None:
        raise APIError(409, "run_active", "A check is running on this pack")
    await conn.execute(
        update(Document).where(Document.id == did).values(deleted_at=datetime.now(UTC))
    )
    await audit(conn, actor, "delete_document", did)
    return {"status": "deleted"}


@router.get("/packs/{pid}/documents/{did}/file")
async def get_file(
    pid: UUID, did: UUID, request: Request, conn: Conn, actor: Actor
) -> FileResponse:
    row = await document(conn, actor, pid, did)
    path = document_path(request.app.state.settings.cda_upload_dir, pid, row["sha256"])
    if not path.is_file():
        raise APIError(404, "not_found", "File not found")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=row["filename"],
        content_disposition_type="inline",
    )


async def index_pack(request: Request, pid: UUID, actor: dict[str, Any]) -> None:
    engine = deps.engine_for(request)
    try:
        async with engine.connect() as conn:
            # Cross-process exclusion as well as the in-process state below.
            key = int.from_bytes(pid.bytes[:8], "big", signed=True)
            locked = (
                await conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
            ).scalar_one()
            await conn.commit()
            if not locked:
                return
            try:
                async with conn.begin():
                    docs = (
                        (
                            await conn.execute(
                                deps.visible(
                                    select(Document).join(Pack, Pack.id == Document.pack_id),
                                    actor["id"],
                                ).where(Document.pack_id == pid, Document.deleted_at.is_(None))
                            )
                        )
                        .mappings()
                        .all()
                    )
                    for doc in docs:
                        did = doc["id"]
                        request.app.state.index_errors.pop(did, None)
                        try:
                            async with conn.begin_nested():
                                await index_document(conn, did, request.app.state.embedder)
                            request.app.state.document_states[did] = "done"
                        except Exception:  # noqa: BLE001 - isolate per-document background failures
                            request.app.state.index_errors[did] = "Indexing failed"
                    await audit(conn, actor, "index_complete", pid)
            finally:
                await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                await conn.commit()
    finally:
        request.app.state.indexing.discard(pid)


@router.post("/packs/{pid}/parse", status_code=202)
async def parse(
    pid: UUID, request: Request, background: BackgroundTasks, conn: Conn, actor: Actor
) -> dict[str, str]:
    await deps.visible_pack(conn, actor, pid)
    lead(actor)
    if pid not in request.app.state.indexing:
        request.app.state.indexing.add(pid)
        await audit(conn, actor, "index", pid)
        # Commit registration before the background task obtains its own connection.
        await conn.commit()
        background.add_task(index_pack, request, pid, actor)
    return {"status": "indexing"}
