"""Framework-free services shared by REST and MCP."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import func, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import Select

from cda.agent.prompts import prompt_sha256
from cda.api.errors import APIError, run_errors
from cda.checks.loader import default_check_set
from cda.db.models import (
    AuditLog,
    CheckSet,
    Document,
    DocumentPart,
    Evidence,
    Fact,
    Finding,
    Inconsistency,
    LLMCall,
    Pack,
    Review,
    Run,
    RunEvent,
    Team,
    TeamMember,
    Unit,
    User,
)
from cda.llm.cost import ModelTable
from cda.tools.permissions import visible_pack_clause


@dataclass
class IndexState:
    indexing: set[UUID] = field(default_factory=set)
    index_errors: dict[UUID, str] = field(default_factory=dict)
    document_states: dict[UUID, str] = field(default_factory=dict)


async def load_actor(conn: AsyncConnection, handle: str | None) -> dict[str, Any]:
    if handle not in ("sarah", "alex"):
        raise APIError(401, "unauthorized", "Select a demo user")
    user = (await conn.execute(select(User).where(User.handle == handle))).mappings().first()
    if user is None:
        raise APIError(401, "unauthorized", "Select a demo user")
    return dict(user)


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


class RunInput(BaseModel):
    pack_id: UUID
    method: Literal["A", "B", "C"]
    model_key: str
    budget_usd: Decimal = Field(
        default=Decimal("2.0"), gt=0, le=10, decimal_places=6, allow_inf_nan=False
    )


async def document_info(
    index_state: IndexState, conn: AsyncConnection, row: dict[str, Any], actor: dict[str, Any]
) -> dict[str, Any]:
    did = row["id"]
    parts = (
        (
            await conn.execute(
                visible(
                    select(DocumentPart)
                    .join(Document, Document.id == DocumentPart.document_id)
                    .join(Pack, Pack.id == Document.pack_id),
                    actor["id"],
                )
                .where(Document.id == did, Document.deleted_at.is_(None))
                .order_by(DocumentPart.first_pdf_page)
            )
        )
        .mappings()
        .all()
    )
    missing = (
        await conn.execute(
            visible(
                select(func.count())
                .select_from(Unit)
                .join(Document, Document.id == Unit.document_id)
                .join(Pack, Pack.id == Document.pack_id),
                actor["id"],
            ).where(
                Document.id == did,
                Document.deleted_at.is_(None),
                Unit.kind.in_(["paragraph", "table_row"]),
                Unit.embedding.is_(None),
            )
        )
    ).scalar_one()
    count = (
        await conn.execute(
            visible(
                select(func.count())
                .select_from(Unit)
                .join(Document, Document.id == Unit.document_id)
                .join(Pack, Pack.id == Document.pack_id),
                actor["id"],
            ).where(
                Document.id == did, Document.deleted_at.is_(None), Unit.context_header.is_not(None)
            )
        )
    ).scalar_one()
    state = index_state.document_states.get(did)
    if row["pack_id"] in index_state.indexing:
        status = "indexing"
    elif did in index_state.index_errors:
        status = "failed"
    elif not missing and (count or state == "done"):
        status = "done"
    else:
        status = "pending"
    return {**row, "parts": [dict(p) for p in parts], "index_status": status}


async def list_packs(conn: AsyncConnection, actor: dict[str, Any]) -> list[dict[str, Any]]:
    count = (
        select(func.count())
        .select_from(Document)
        .where(Document.pack_id == Pack.id, Document.deleted_at.is_(None))
        .correlate(Pack)
        .scalar_subquery()
    )
    rows = (
        (
            await conn.execute(
                visible(
                    select(Pack, count.label("document_count"), Team.name.label("team_name")).join(
                        Team, Team.id == Pack.team_id
                    ),
                    actor["id"],
                ).order_by(Pack.created_at, Pack.id)
            )
        )
        .mappings()
        .all()
    )
    latest = (
        select(Run.id, Run.pack_id, Run.status, Run.created_at)
        .distinct(Run.pack_id)
        .where(Run.pack_id.in_([r["id"] for r in rows]))
        .order_by(Run.pack_id, Run.created_at.desc(), Run.id.desc())
        .subquery()
    )
    runs = {r["pack_id"]: dict(r) for r in (await conn.execute(select(latest))).mappings()}
    counts: dict[UUID, dict[str, int]] = {}
    for rid, status, n in await conn.execute(
        select(Finding.run_id, Finding.status, func.count())
        .where(Finding.run_id.in_([r["id"] for r in runs.values()]))
        .group_by(Finding.run_id, Finding.status)
    ):
        counts.setdefault(rid, {})[status] = n
    result = []
    for r in rows:
        run = runs.get(r["id"])
        summary = None
        if run is not None:
            summary = {
                "id": run["id"],
                "status": run["status"],
                "created_at": run["created_at"],
                "counts": counts.get(run["id"], {}),
            }
        result.append({**r, "latest_run": summary})
    return result


async def list_teams(conn: AsyncConnection, actor: dict[str, Any]) -> list[dict[str, Any]]:
    mine = select(TeamMember.team_id).where(TeamMember.user_id == actor["id"])
    rows = (
        await conn.execute(
            select(Team.id, Team.name, User.handle, User.display_name)
            .join(TeamMember, TeamMember.team_id == Team.id)
            .join(User, User.id == TeamMember.user_id)
            .where(Team.id.in_(mine))
            .order_by(Team.name, User.role.desc(), User.handle)
        )
    ).all()
    teams: dict[UUID, dict[str, Any]] = {}
    for tid, name, handle, display_name in rows:
        team = teams.setdefault(tid, {"id": tid, "name": name, "members": []})
        team["members"].append({"handle": handle, "display_name": display_name})
    return list(teams.values())


async def wake(conn: AsyncConnection) -> None:
    await conn.execute(text("SELECT pg_notify('cda_runs', '')"))


async def create_run(
    conn: AsyncConnection,
    actor: dict[str, Any],
    body: RunInput,
    idempotency_key: str | None,
    index_state: IndexState,
    **audit_detail: Any,
) -> tuple[dict[str, Any], bool]:
    if idempotency_key is None or not 1 <= len(idempotency_key) <= 200:
        raise APIError(
            400, "invalid_idempotency_key", "Idempotency-Key is required (1-200 characters)"
        )
    if body.model_key not in ModelTable.load().root:
        raise APIError(422, "invalid_model", "Unknown model key")
    await visible_pack(conn, actor, body.pack_id)
    # Serialize equal keys before checking, so concurrent retries cannot race the unique index.
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": idempotency_key}
    )
    old = (
        (
            await conn.execute(
                visible(select(Run).join(Pack, Pack.id == Run.pack_id), actor["id"]).where(
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
        return run_errors(dict(old)), False
    await conn.execute(
        visible(select(Pack.id), actor["id"])
        .where(Pack.id == body.pack_id)
        .with_for_update(of=Pack)
    )
    docs = (
        (
            await conn.execute(
                visible(
                    select(Document).join(Pack, Pack.id == Document.pack_id), actor["id"]
                ).where(Document.pack_id == body.pack_id, Document.deleted_at.is_(None))
            )
        )
        .mappings()
        .all()
    )
    if not docs or any(
        [
            (await document_info(index_state, conn, dict(d), actor))["index_status"] != "done"
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
    await audit(conn, actor, "create_run", row["id"], **audit_detail)
    await wake(conn)
    return dict(row), True


async def get_run(conn: AsyncConnection, actor: dict[str, Any], rid: UUID) -> dict[str, Any]:
    row = await visible_run(conn, actor, rid)
    counts = (
        (
            await conn.execute(
                visible(
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
                visible(
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


async def finding_rows(
    conn: AsyncConnection, actor: dict[str, Any], rid: UUID
) -> list[dict[str, Any]]:
    await visible_run(conn, actor, rid)
    evidence_count = (
        select(func.count())
        .select_from(Evidence)
        .where(Evidence.finding_id == Finding.id)
        .correlate(Finding)
        .scalar_subquery()
    )
    reviewed = select(Review.id).where(Review.finding_id == Finding.id).correlate(Finding).exists()
    rows = (
        (
            await conn.execute(
                visible(
                    select(
                        Finding, evidence_count.label("evidence_count"), reviewed.label("reviewed")
                    )
                    .join(Run, Run.id == Finding.run_id)
                    .join(Pack, Pack.id == Run.pack_id),
                    actor["id"],
                ).where(Run.id == rid)
            )
        )
        .mappings()
        .all()
    )
    by_check = {row["check_id"]: dict(row) for row in rows}
    result = []
    for check in default_check_set().checks:
        row = by_check.get(check.id)
        if row is None:
            result.append(
                {
                    "id": None,
                    "check_id": check.id,
                    "title": check.title,
                    "status": "not_checked",
                    "confidence": None,
                    "needs_review_reason": None,
                    "relief_applied": False,
                    "evidence_count": 0,
                    "review_state": "none",
                    "version": None,
                }
            )
        else:
            row["title"] = check.title
            row["review_state"] = "reviewed" if row.pop("reviewed") else "none"
            result.append(row)
    return result


async def finding_detail(conn: AsyncConnection, actor: dict[str, Any], fid: UUID) -> dict[str, Any]:
    row = await visible_finding(conn, actor, fid)
    evidence = (
        (
            await conn.execute(
                visible(
                    select(
                        Evidence,
                        Document.filename,
                        Document.id.label("document_id"),
                        Document.kind.label("document_kind"),
                        Unit.pdf_page,
                        Unit.printed_page,
                        Pack.id.label("pack_id"),
                        DocumentPart.kind.label("part_kind"),
                        DocumentPart.label.label("part_label"),
                    )
                    .join(Finding, Finding.id == Evidence.finding_id)
                    .join(Run, Run.id == Finding.run_id)
                    .join(Unit, Unit.id == Evidence.unit_id)
                    .join(Document, Document.id == Unit.document_id)
                    .join(Pack, Pack.id == Document.pack_id)
                    .outerjoin(
                        DocumentPart,
                        (DocumentPart.document_id == Document.id)
                        & (DocumentPart.first_pdf_page <= Unit.pdf_page)
                        & (DocumentPart.last_pdf_page >= Unit.pdf_page),
                    ),
                    actor["id"],
                )
                .where(
                    Finding.id == fid,
                    Document.pack_id == Run.pack_id,
                    Document.deleted_at.is_(None),
                )
                .order_by(Unit.seq, Evidence.id)
            )
        )
        .mappings()
        .all()
    )
    cards = []
    for e in evidence:
        card = dict(e)
        card["part_kind"] = card["part_kind"] or card.pop("document_kind")
        card["link"] = (
            f"/packs/{card['pack_id']}/documents/{card['document_id']}/file#page={card['pdf_page']}"
        )
        cards.append(card)
    facts = (
        (
            await conn.execute(
                visible(
                    select(Fact)
                    .join(Finding, Finding.id == Fact.finding_id)
                    .join(Run, Run.id == Finding.run_id)
                    .join(Pack, Pack.id == Run.pack_id),
                    actor["id"],
                ).where(Finding.id == fid)
            )
        )
        .mappings()
        .all()
    )
    inconsistencies = (
        (
            await conn.execute(
                visible(
                    select(Inconsistency)
                    .join(Run, Run.id == Inconsistency.run_id)
                    .join(Pack, Pack.id == Run.pack_id),
                    actor["id"],
                ).where(Inconsistency.run_id == row["run_id"])
            )
        )
        .mappings()
        .all()
    )
    # Compare can reference another finding in its structured detail (T3/T4/T6).
    related = [
        dict(i)
        for i in inconsistencies
        if i["finding_id"] == fid or str(fid) in _references(i["detail"])
    ]
    reviews = (
        (
            await conn.execute(
                visible(
                    select(Review)
                    .join(Finding, Finding.id == Review.finding_id)
                    .join(Run, Run.id == Finding.run_id)
                    .join(Pack, Pack.id == Run.pack_id),
                    actor["id"],
                )
                .where(Finding.id == fid)
                .order_by(Review.created_at, Review.id)
            )
        )
        .mappings()
        .all()
    )
    calls = (
        (
            await conn.execute(
                visible(
                    select(LLMCall)
                    .join(Run, Run.id == LLMCall.run_id)
                    .join(Pack, Pack.id == Run.pack_id),
                    actor["id"],
                )
                .where(LLMCall.run_id == row["run_id"], LLMCall.check_id == row["check_id"])
                .order_by(LLMCall.step, LLMCall.created_at)
            )
        )
        .mappings()
        .all()
    )
    events = (
        (
            await conn.execute(
                visible(
                    select(RunEvent)
                    .join(Run, Run.id == RunEvent.run_id)
                    .join(Pack, Pack.id == Run.pack_id),
                    actor["id"],
                )
                .where(
                    RunEvent.run_id == row["run_id"],
                    RunEvent.check_id == row["check_id"],
                    RunEvent.type == "tool_called",
                )
                .order_by(RunEvent.id)
            )
        )
        .mappings()
        .all()
    )
    return {
        **row,
        "evidence": cards,
        "facts": [dict(f) for f in facts],
        "inconsistencies": related,
        "reviews": [
            {**dict(r), "decision": "accept" if r["decision"] == "confirm" else "change"}
            for r in reviews
        ],
        "trace": {"llm_calls": [dict(c) for c in calls], "tool_called": [dict(e) for e in events]},
    }


def _references(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [ref for v in value.values() for ref in _references(v)]
    if isinstance(value, list):
        return [ref for v in value for ref in _references(v)]
    return [str(value)]
