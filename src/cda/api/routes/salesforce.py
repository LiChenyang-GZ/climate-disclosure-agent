"""Salesforce connection, pack Account link and follow-up Cases (D85).

Every route except the GETs is lead-only. Credentials are kept in API memory only.
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from cda.api import deps, service
from cda.api.deps import Actor, Conn, audit, lead
from cda.api.errors import APIError
from cda.checks.loader import default_check_set
from cda.db.models import Finding, Pack, Review, Run, SalesforceCase
from cda.integrations.salesforce import (
    ELIGIBLE_STATUSES,
    Credentials,
    SalesforceClient,
    SalesforceConnection,
    SalesforceError,
    SalesforceLimit,
    SalesforceRejected,
    SalesforceRequestFailed,
    case_description,
    case_fields,
)

router = APIRouter()

_REJECTED = "Salesforce rejected the credentials."
_NOT_CONFIGURED = "Connect Salesforce in Settings first."


def connection(request: Request) -> SalesforceConnection:
    sf: SalesforceConnection = request.app.state.salesforce
    return sf


def configured_client(request: Request) -> SalesforceClient:
    client = connection(request).client
    if client is None:
        raise APIError(409, "salesforce_not_configured", _NOT_CONFIGURED)
    return client


def salesforce_error(exc: SalesforceError) -> APIError:
    """Map client errors outside the Case push; extras added by the caller."""
    if isinstance(exc, SalesforceRejected):
        return APIError(409, exc.code, _REJECTED)
    if isinstance(exc, SalesforceLimit):
        return APIError(503, exc.code, exc.message)
    if isinstance(exc, SalesforceRequestFailed):
        return APIError(502, "salesforce_error", "Salesforce returned an error.")
    return APIError(502, "salesforce_unavailable", exc.message)


@router.get("/integrations/salesforce")
async def status(request: Request, actor: Actor) -> dict[str, Any]:
    return connection(request).status()


class ConnectInput(BaseModel):
    instance_url: str = Field(min_length=1, max_length=255)
    client_id: str = Field(min_length=1, max_length=255)
    client_secret: SecretStr = Field(min_length=1, max_length=255)


@router.post("/integrations/salesforce/test")
async def test_connection(
    body: ConnectInput, request: Request, conn: Conn, actor: Actor
) -> dict[str, Any]:
    lead(actor)
    try:
        credentials = Credentials(body.instance_url, body.client_id.strip(), body.client_secret)
    except ValueError:
        raise APIError(
            422,
            "invalid_instance_url",
            "Enter the Salesforce My Domain URL, starting with https://.",
        ) from None
    try:
        edition = await connection(request).connect(credentials)
    except (SalesforceRejected, SalesforceRequestFailed):
        raise APIError(422, "salesforce_rejected", _REJECTED) from None
    except SalesforceError as exc:
        raise salesforce_error(exc) from None
    await audit(conn, actor, "salesforce_connect", credentials.instance_url, edition=edition)
    return {"connected": True, "edition": edition}


@router.get("/integrations/salesforce/accounts")
async def search_accounts(
    request: Request, actor: Actor, q: Annotated[str, Query(max_length=100)] = ""
) -> list[dict[str, str]]:
    lead(actor)
    client = configured_client(request)
    term = " ".join(q.split())
    if not term:
        return []
    try:
        return await client.search_accounts(term)
    except SalesforceError as exc:
        raise salesforce_error(exc) from None


class AccountInput(BaseModel):
    # Account ids start with the 001 key prefix; 15- or 18-character form.
    id: str = Field(pattern=r"^001[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?$")
    name: str = Field(min_length=1, max_length=255)


async def _set_account(
    conn: AsyncConnection, actor: dict[str, Any], pid: UUID, account: dict[str, str] | None
) -> dict[str, Any]:
    await deps.visible_pack(conn, actor, pid)
    lead(actor)
    row = (
        (
            await conn.execute(
                update(Pack)
                .where(Pack.id == pid)
                .values(salesforce_account=account)
                .returning(Pack)
            )
        )
        .mappings()
        .one()
    )
    await audit(
        conn,
        actor,
        "salesforce_link" if account else "salesforce_unlink",
        pid,
        **({"account_id": account["id"]} if account else {}),
    )
    return dict(row)


@router.put("/packs/{pid}/salesforce-account")
async def link_account(pid: UUID, body: AccountInput, conn: Conn, actor: Actor) -> dict[str, Any]:
    name = " ".join(body.name.split())
    if not name:
        raise APIError(422, "invalid_request", "Invalid request")
    return await _set_account(conn, actor, pid, {"id": body.id, "name": name})


@router.delete("/packs/{pid}/salesforce-account")
async def unlink_account(pid: UUID, conn: Conn, actor: Actor) -> dict[str, Any]:
    return await _set_account(conn, actor, pid, None)


async def eligible_findings(
    conn: AsyncConnection, actor: dict[str, Any], rid: UUID
) -> list[dict[str, Any]]:
    """Follow-up findings of a run (latest review applied), in check order, with any Case."""
    rows = (
        (
            await conn.execute(
                deps.visible(
                    select(
                        Finding.id,
                        Finding.check_id,
                        Finding.status,
                        SalesforceCase.case_id,
                        SalesforceCase.case_number,
                    )
                    .join(Run, Run.id == Finding.run_id)
                    .join(Pack, Pack.id == Run.pack_id)
                    .outerjoin(SalesforceCase, SalesforceCase.finding_id == Finding.id),
                    actor["id"],
                ).where(Run.id == rid)
            )
        )
        .mappings()
        .all()
    )
    latest: dict[UUID, Any] = {}
    reviews = await conn.execute(
        select(Review.finding_id, Review.decision, Review.new_status)
        .where(Review.finding_id.in_([r["id"] for r in rows]))
        .order_by(Review.created_at, Review.id)
    )
    for fid, decision, new_status in reviews:
        latest[fid] = (decision, new_status)
    order = {c.id: i for i, c in enumerate(default_check_set().checks)}
    result = []
    for row in rows:
        review = latest.get(row["id"])
        effective = review[1] if review and review[0] == "change_status" else row["status"]
        if effective in ELIGIBLE_STATUSES:
            result.append({**row, "status": effective})
    return sorted(result, key=lambda r: (order.get(r["check_id"], len(order)), r["check_id"]))


def _case_row(sf: SalesforceConnection, row: dict[str, Any]) -> dict[str, Any]:
    case_id = row["case_id"]
    return {
        "finding_id": str(row["id"]),
        "check_id": row["check_id"],
        "status": row["status"],
        "case_id": case_id,
        "case_number": row["case_number"],
        "case_url": sf.case_url(case_id) if case_id else None,
    }


@router.get("/runs/{rid}/salesforce/cases")
async def list_cases(rid: UUID, request: Request, conn: Conn, actor: Actor) -> dict[str, Any]:
    run = await deps.visible_run(conn, actor, rid)
    pack = await deps.visible_pack(conn, actor, run["pack_id"])
    sf = connection(request)
    rows = await eligible_findings(conn, actor, rid)
    reason = None
    if not sf.configured:
        reason = "not_configured"
    elif pack["salesforce_account"] is None:
        reason = "not_linked"
    elif all(r["case_id"] for r in rows):
        reason = "nothing_to_send"
    return {
        "ready": reason is None,
        "reason": reason,
        "findings": [_case_row(sf, r) for r in rows],
    }


def _result(
    sf: SalesforceConnection,
    row: dict[str, Any],
    outcome: str,
    error: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {**_case_row(sf, row), "result": outcome, "error": error}


@router.post("/runs/{rid}/salesforce/cases")
async def push_cases(rid: UUID, request: Request, conn: Conn, actor: Actor) -> dict[str, Any]:
    run = await deps.visible_run(conn, actor, rid)
    lead(actor)
    pack = await deps.visible_pack(conn, actor, run["pack_id"])
    sf = connection(request)
    client = configured_client(request)
    account = pack["salesforce_account"]
    if account is None:
        raise APIError(409, "salesforce_not_linked", "Link the pack to a Salesforce account first.")
    # One push per run at a time, so concurrent pushes cannot create duplicate Cases.
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 2))"),
        {"key": f"salesforce:{rid}"},
    )
    titles = {c.id: c.title for c in default_check_set().checks}
    base = (request.app.state.settings.cda_web_url or "").rstrip("/")
    engine = deps.engine_for(request)
    results: list[dict[str, Any]] = []
    stop: SalesforceError | None = None
    for row in await eligible_findings(conn, actor, rid):
        if row["case_id"]:
            results.append(_result(sf, row, "exists"))
            continue
        if stop is not None:
            results.append(_result(sf, row, "failed", {"code": stop.code, "message": stop.message}))
            continue
        detail = await service.finding_detail(conn, actor, row["id"])
        description = case_description(
            status=row["status"],
            rationale=detail["rationale"],
            missing_aspects=list(detail["missing_aspects"] or []),
            evidence=detail["evidence"],
            report_link=f"{base}/runs/{rid}/report",
        )
        fields = case_fields(
            row["check_id"], titles.get(row["check_id"], ""), account["id"], description
        )
        try:
            case_id = await client.create_case(fields)
        except SalesforceRequestFailed as exc:
            results.append(
                _result(sf, row, "failed", {"code": exc.error_code, "message": exc.message})
            )
            continue
        except SalesforceError as exc:
            stop = exc
            results.append(_result(sf, row, "failed", {"code": exc.code, "message": exc.message}))
            continue
        # Commit each Case at once: it exists in Salesforce even if a later step fails.
        async with engine.begin() as own:
            await own.execute(
                pg_insert(SalesforceCase)
                .values(finding_id=row["id"], case_id=case_id)
                .on_conflict_do_nothing(index_elements=["finding_id"])
            )
            await audit(own, actor, "salesforce_case", row["id"], case_id=case_id)
        number = None
        try:
            number = await client.case_number(case_id)
        except SalesforceRequestFailed:
            pass
        except SalesforceError as exc:
            stop = exc
        if number is not None:
            async with engine.begin() as own:
                await own.execute(
                    update(SalesforceCase)
                    .where(SalesforceCase.finding_id == row["id"])
                    .values(case_number=number)
                )
        results.append(_result(sf, {**row, "case_id": case_id, "case_number": number}, "created"))
    if isinstance(stop, SalesforceRejected):
        raise APIError(409, stop.code, _REJECTED, results=results)
    if stop is not None:
        raise APIError(
            503 if isinstance(stop, SalesforceLimit) else 502,
            stop.code,
            stop.message,
            results=results,
        )
    return {"results": results}
