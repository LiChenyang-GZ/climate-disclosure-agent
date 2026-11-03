"""OneDrive / SharePoint sign-in, browse, import and re-sync (X2, D84)."""

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from cda.api import deps, service
from cda.api.deps import Actor, Conn, audit, lead
from cda.api.errors import APIError
from cda.api.routes.packs import MAX_DOCUMENTS, document, store_document, upload_limit
from cda.db.enums import DocumentKind
from cda.db.models import Document, DocumentPart, Pack
from cda.integrations.graph import (
    Connection,
    DeniedError,
    ExpiredError,
    GraphClient,
    GraphError,
    LoginError,
    MicrosoftLogin,
    MicrosoftState,
    NotFoundError,
    TooLargeError,
    item_path,
    supported,
)
from cda.parsing.parts import Part, replace_parts

router = APIRouter()

# Graph ids: drive ids like `b!…`, site ids `host,guid,guid`, item ids `01ABC…`. No `/` or `:`
# (path-based addressing), and no leading `.`, so never a `.` / `..` path segment.
GRAPH_ID = r"^[A-Za-z0-9!_,-][A-Za-z0-9!_.,-]{0,399}$"
GraphId = Annotated[str, Field(pattern=GRAPH_ID)]
ONEDRIVE = "me"
DENIED_MESSAGE = "Access denied in SharePoint"


def expired() -> APIError:
    return APIError(409, "microsoft_expired", "Your Microsoft connection expired. Reconnect.")


def microsoft(request: Request) -> MicrosoftState:
    state: MicrosoftState = request.app.state.microsoft
    return state


def login_for(request: Request, http: Any) -> MicrosoftLogin:
    settings = request.app.state.settings
    if not settings.cda_ms_client_id:
        raise APIError(
            409,
            "microsoft_not_configured",
            "Microsoft sign-in isn't set up. Set CDA_MS_CLIENT_ID and restart the backend.",
        )
    return MicrosoftLogin(
        http,
        tenant=settings.cda_ms_tenant,
        client_id=settings.cda_ms_client_id,
        clock=microsoft(request).clock,
    )


@asynccontextmanager
async def graph_session(request: Request, actor: dict[str, Any]) -> AsyncIterator[GraphClient]:
    """Graph client for the actor's in-memory connection; expiry ends the connection."""
    state = microsoft(request)
    handle = actor["handle"]
    async with state.http() as http:
        login = login_for(request, http)
        connection = state.connections.get(handle)
        if connection is None:
            raise expired()

        async def token(force: bool) -> str:
            return await connection.access_token(login, state.clock, force)

        try:
            yield GraphClient(http, token, sleep=state.sleep)
        except ExpiredError:
            if state.connections.get(handle) is connection:
                del state.connections[handle]
            raise expired() from None


def failure(error: GraphError) -> tuple[str, str, str]:
    """(status, error code, message) for one file whose Graph call failed."""
    if isinstance(error, DeniedError):
        return "denied", "access_denied", DENIED_MESSAGE
    if isinstance(error, NotFoundError):
        return "failed", "not_found", "Not found in SharePoint"
    if isinstance(error, TooLargeError):
        return "rejected", "upload_too_large", "Upload exceeds size limit"
    return "failed", "graph_error", "Couldn't download from SharePoint"


def source_of(drive_id: str, item: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": "microsoft",
        "drive_id": drive_id,
        "item_id": item.get("id"),
        "path": item_path(item),
        "etag": item.get("eTag"),
        "web_url": item.get("webUrl"),
    }


# --- connection --------------------------------------------------------------------------------


@router.get("/integrations/microsoft")
async def status(request: Request, actor: Actor) -> dict[str, Any]:
    connection = microsoft(request).connections.get(actor["handle"])
    return {
        "configured": bool(request.app.state.settings.cda_ms_client_id),
        "connected": connection is not None,
        "account": connection.account if connection else None,
    }


@router.post("/integrations/microsoft/device-code")
async def device_code(request: Request, actor: Actor) -> dict[str, Any]:
    lead(actor)
    state = microsoft(request)
    async with state.http() as http:
        try:
            flow = await login_for(request, http).device_code()
        except LoginError:
            raise APIError(502, "microsoft_error", "Microsoft sign-in failed. Try again.") from None
    state.pending[actor["handle"]] = flow
    return {
        "user_code": flow.user_code,
        "verification_uri": flow.verification_uri,
        "expires_in": flow.expires_in,
        "interval": flow.interval,
    }


@router.post("/integrations/microsoft/poll")
async def poll(request: Request, actor: Actor) -> dict[str, Any]:
    lead(actor)
    state = microsoft(request)
    handle = actor["handle"]
    flow = state.pending.get(handle)
    if flow is None:
        connection = state.connections.get(handle)
        if connection is not None:
            return {"state": "connected", "account": connection.account}
        return {"state": "expired"}
    if state.clock() >= flow.expires_at:
        state.pending.pop(handle, None)
        return {"state": "expired"}
    async with state.http() as http:
        try:
            result = await login_for(request, http).poll(flow.device_code)
        except LoginError:
            state.pending.pop(handle, None)
            raise APIError(502, "microsoft_error", "Microsoft sign-in failed. Try again.") from None
        if isinstance(result, str):
            if result != "pending":
                state.pending.pop(handle, None)
            return {"state": result}
        tokens = result

        async def token(force: bool) -> str:
            return tokens.access_token

        try:
            me = await GraphClient(http, token, sleep=state.sleep).me()
        except (GraphError, ExpiredError):
            state.pending.pop(handle, None)
            raise APIError(502, "microsoft_error", "Microsoft sign-in failed. Try again.") from None
    account = str(me.get("userPrincipalName") or me.get("mail") or "")
    state.connections[handle] = Connection(tokens=tokens, account=account)
    state.pending.pop(handle, None)
    return {"state": "connected", "account": account}


@router.delete("/integrations/microsoft")
async def disconnect(request: Request, actor: Actor) -> dict[str, Any]:
    lead(actor)
    state = microsoft(request)
    state.pending.pop(actor["handle"], None)
    state.connections.pop(actor["handle"], None)
    return {"connected": False}


# --- browse ------------------------------------------------------------------------------------


def listing(items: list[dict[str, Any]], drive_id: str | None) -> dict[str, Any]:
    folders, files = [], []
    for item in items:
        drive = (item.get("parentReference") or {}).get("driveId") or drive_id
        if "folder" in item:
            folders.append(
                {
                    "drive_id": drive,
                    "item_id": item.get("id"),
                    "name": item.get("name"),
                    "child_count": (item.get("folder") or {}).get("childCount"),
                }
            )
        elif "file" in item and supported(item.get("name")):
            files.append(
                {
                    "drive_id": drive,
                    "item_id": item.get("id"),
                    "name": item.get("name"),
                    "path": item_path(item),
                    "size": item.get("size"),
                    "last_modified": item.get("lastModifiedDateTime"),
                }
            )
    return {"sites": [], "drives": [], "folders": folders, "files": files}


@router.get("/integrations/microsoft/browse")
async def browse(
    request: Request,
    actor: Actor,
    site_id: Annotated[str | None, Query(pattern=GRAPH_ID)] = None,
    drive_id: Annotated[str | None, Query(pattern=GRAPH_ID)] = None,
    item_id: Annotated[str | None, Query(pattern=GRAPH_ID)] = None,
) -> dict[str, Any]:
    lead(actor)
    async with graph_session(request, actor) as graph:
        try:
            if drive_id:
                return listing(await graph.children(drive_id, item_id or "root"), drive_id)
            if site_id == ONEDRIVE:
                return listing(await graph.my_root_children(), None)
            if site_id:
                drives = [
                    {"id": d.get("id"), "name": d.get("name"), "drive_type": d.get("driveType")}
                    for d in await graph.site_drives(site_id)
                ]
                return {"sites": [], "drives": drives, "folders": [], "files": []}
            sites = [
                {
                    "id": s.get("id"),
                    "name": s.get("displayName") or s.get("name"),
                    "kind": "sharepoint",
                    "web_url": s.get("webUrl"),
                }
                for s in await graph.sites()
            ]
        except DeniedError:
            raise APIError(403, "microsoft_denied", DENIED_MESSAGE) from None
        except NotFoundError:
            raise APIError(404, "not_found", "Not found in SharePoint") from None
        except GraphError:
            raise APIError(
                502, "microsoft_unavailable", "Couldn't reach SharePoint. Try again."
            ) from None
    onedrive = {"id": ONEDRIVE, "name": "My files", "kind": "onedrive", "web_url": None}
    return {"sites": [onedrive, *sites], "drives": [], "folders": [], "files": []}


# --- import ------------------------------------------------------------------------------------


class ImportItem(BaseModel):
    drive_id: GraphId
    item_id: GraphId
    kind: DocumentKind


class ImportInput(BaseModel):
    items: list[ImportItem] = Field(min_length=1, max_length=MAX_DOCUMENTS)


async def info(request: Request, conn: Conn, actor: Actor, pid: UUID, did: UUID) -> dict[str, Any]:
    row = await document(conn, actor, pid, did)
    return await service.document_info(request.app.state.index_state, conn, row, actor)


async def download(
    request: Request, graph: GraphClient, drive_id: str, meta: dict[str, Any]
) -> bytes:
    """Bytes of a supported file within the upload limit; raises APIError or GraphError."""
    name = meta.get("name")
    if "file" not in meta or not supported(name):
        raise APIError(415, "unsupported_format", "Upload a PDF, .docx or .xlsx file")
    limit = upload_limit(request)
    if isinstance(meta.get("size"), int) and meta["size"] > limit:
        raise APIError(413, "upload_too_large", "Upload exceeds size limit")
    return await graph.content(drive_id, str(meta.get("id")), limit=limit)


async def import_one(
    request: Request, conn: Conn, actor: Actor, pid: UUID, graph: GraphClient, item: ImportItem
) -> dict[str, Any]:
    result: dict[str, Any] = {"item_id": item.item_id}
    try:
        meta = await graph.item(item.drive_id, item.item_id)
        content = await download(request, graph, item.drive_id, meta)
        async with conn.begin_nested():
            did = await store_document(
                request,
                conn,
                actor,
                pid,
                content=content,
                filename=meta.get("name"),
                kind=item.kind,
                parts=[],
                source=source_of(item.drive_id, meta),
                action="import_document",
            )
    except GraphError as exc:
        state, code, message = failure(exc)
        return result | {"status": state, "error": code, "message": message}
    except APIError as exc:
        state = "duplicate" if exc.code == "duplicate_document" else "rejected"
        return result | {"status": state, "error": exc.code, "message": exc.message}
    return result | {"status": "imported", "document": await info(request, conn, actor, pid, did)}


@router.post("/packs/{pid}/import")
async def import_documents(
    pid: UUID, body: ImportInput, request: Request, conn: Conn, actor: Actor
) -> list[dict[str, Any]]:
    await deps.visible_pack(conn, actor, pid)
    lead(actor)
    if pid in request.app.state.indexing:
        raise APIError(409, "pack_parsing", "Wait for parsing to finish before uploading")
    async with graph_session(request, actor) as graph:
        return [await import_one(request, conn, actor, pid, graph, item) for item in body.items]


# --- re-sync -----------------------------------------------------------------------------------


async def imported(
    conn: Conn, actor: Actor, pid: UUID, ids: list[UUID] | None = None
) -> list[dict[str, Any]]:
    stmt = deps.visible(
        select(Document).join(Pack, Pack.id == Document.pack_id), actor["id"]
    ).where(
        Document.pack_id == pid,
        Document.deleted_at.is_(None),
        Document.source["provider"].astext == "microsoft",
    )
    if ids is not None:
        stmt = stmt.where(Document.id.in_(ids))
    rows = (await conn.execute(stmt.order_by(Document.filename, Document.id))).mappings().all()
    return [dict(r) for r in rows]


def summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "document_id": str(row["id"]),
        "filename": row["filename"],
        "path": row["source"]["path"],
    }


@router.get("/packs/{pid}/sync")
async def sync_status(pid: UUID, request: Request, conn: Conn, actor: Actor) -> dict[str, Any]:
    await deps.visible_pack(conn, actor, pid)
    lead(actor)
    changed, removed, failed = [], [], []
    unchanged = 0
    async with graph_session(request, actor) as graph:
        for row in await imported(conn, actor, pid):
            source = row["source"]
            try:
                meta = await graph.item(source["drive_id"], source["item_id"])
            except NotFoundError:
                removed.append(summary(row))
                continue
            except GraphError as exc:
                _, code, message = failure(exc)
                failed.append(summary(row) | {"error": code, "message": message})
                continue
            if "deleted" in meta:
                removed.append(summary(row))
            elif meta.get("eTag") != source.get("etag"):
                changed.append(summary(row))
            else:
                unchanged += 1
    return {"changed": changed, "removed": removed, "failed": failed, "unchanged": unchanged}


class SyncInput(BaseModel):
    document_ids: list[UUID] = Field(min_length=1, max_length=MAX_DOCUMENTS)


async def resync_one(
    request: Request,
    conn: Conn,
    actor: Actor,
    pid: UUID,
    graph: GraphClient,
    row: dict[str, Any],
) -> dict[str, Any]:
    source = row["source"]
    result: dict[str, Any] = {"document_id": str(row["id"])}
    try:
        meta = await graph.item(source["drive_id"], source["item_id"])
        if "deleted" in meta:
            raise NotFoundError(404)
        if meta.get("eTag") == source.get("etag"):
            return result | {
                "status": "unchanged",
                "document": await info(request, conn, actor, pid, row["id"]),
            }
        content = await download(request, graph, source["drive_id"], meta)
        fresh = source_of(source["drive_id"], meta)
        if hashlib.sha256(content).hexdigest() == row["sha256"]:
            # Only metadata changed (rename, properties): keep the document, record the etag.
            await conn.execute(
                update(Document).where(Document.id == row["id"]).values(source=fresh)
            )
            await audit(conn, actor, "sync_document", row["id"], pack_id=str(pid))
            return result | {
                "status": "unchanged",
                "document": await info(request, conn, actor, pid, row["id"]),
            }
        parts = (
            (await conn.execute(select(DocumentPart).where(DocumentPart.document_id == row["id"])))
            .mappings()
            .all()
        )
        async with conn.begin_nested():
            # Units are immutable (D52): the new version is a new document; the old one is
            # soft-deleted so past runs keep their evidence.
            await conn.execute(
                update(Document)
                .where(Document.id == row["id"])
                .values(deleted_at=datetime.now(UTC))
            )
            did = await store_document(
                request,
                conn,
                actor,
                pid,
                content=content,
                filename=meta.get("name"),
                kind=row["kind"],
                parts=[],
                source=fresh,
                action="sync_document",
            )
        await carry_parts(conn, did, parts)
    except NotFoundError:
        return result | {"status": "removed"}
    except GraphError as exc:
        state, code, message = failure(exc)
        return result | {"status": state, "error": code, "message": message}
    except APIError as exc:
        state = "duplicate" if exc.code == "duplicate_document" else "rejected"
        return result | {"status": state, "error": exc.code, "message": exc.message}
    return result | {
        "status": "updated",
        "replaced": str(row["id"]),
        "document": await info(request, conn, actor, pid, did),
    }


async def carry_parts(conn: Conn, did: UUID, rows: Any) -> None:
    """Keep the old version's page ranges when they still fit the new file."""
    if not rows:
        return
    parts = [
        Part(
            kind=r["kind"],
            first_pdf_page=r["first_pdf_page"],
            last_pdf_page=r["last_pdf_page"],
            label=r["label"],
        )
        for r in rows
    ]
    try:
        async with conn.begin_nested():
            await replace_parts(conn, did, parts)
    except ValueError:
        pass


@router.post("/packs/{pid}/sync")
async def sync(
    pid: UUID, body: SyncInput, request: Request, conn: Conn, actor: Actor
) -> list[dict[str, Any]]:
    await deps.visible_pack(conn, actor, pid)
    lead(actor)
    if pid in request.app.state.indexing:
        raise APIError(409, "pack_parsing", "Wait for parsing to finish before uploading")
    if await service.active_run(conn, actor, pid) is not None:
        raise APIError(409, "run_active", "A check is running on this pack")
    rows = {row["id"]: row for row in await imported(conn, actor, pid, body.document_ids)}
    results = []
    async with graph_session(request, actor) as graph:
        for did in dict.fromkeys(body.document_ids):
            row = rows.get(did)
            if row is None:
                results.append(
                    {
                        "document_id": str(did),
                        "status": "failed",
                        "error": "not_found",
                        "message": "Not an imported file in this pack",
                    }
                )
                continue
            results.append(await resync_one(request, conn, actor, pid, graph, row))
    return results
