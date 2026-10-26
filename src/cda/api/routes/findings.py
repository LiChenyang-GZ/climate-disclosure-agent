"""Finding evidence, traces and optimistic human review."""

from typing import Any, Literal, Self
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import exists, func, insert, select, update

from cda.api import deps
from cda.api.deps import Actor, Conn, audit, lead
from cda.api.errors import APIError
from cda.api.routes.runs import wake
from cda.checks.loader import default_check_set
from cda.db.models import (
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
    Unit,
)

router = APIRouter()


async def finding_rows(conn: Conn, actor: Actor, rid: UUID) -> list[dict[str, Any]]:
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
                deps.visible(
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


async def finding_detail(conn: Conn, actor: Actor, fid: UUID) -> dict[str, Any]:
    row = await deps.visible_finding(conn, actor, fid)
    evidence = (
        (
            await conn.execute(
                deps.visible(
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
                deps.visible(
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
                deps.visible(
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
                deps.visible(
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
                deps.visible(
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
                deps.visible(
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


@router.get("/findings/{fid}")
async def get_finding(fid: UUID, conn: Conn, actor: Actor) -> dict[str, Any]:
    return await finding_detail(conn, actor, fid)


class ReviewInput(BaseModel):
    decision: Literal["accept", "change"]
    new_status: (
        Literal[
            "evidence_found",
            "partial_evidence",
            "no_evidence_found",
            "inconsistent",
            "not_applicable",
        ]
        | None
    ) = None
    note: str = Field(min_length=1, max_length=2000)
    version: int = Field(ge=0)

    @model_validator(mode="after")
    def valid_decision(self) -> Self:
        if self.decision == "accept" and "new_status" in self.model_fields_set:
            raise ValueError("new_status forbidden for accept")
        if not self.note.strip():
            raise ValueError("Note required")
        if (self.decision == "change") != (self.new_status is not None):
            raise ValueError("Only change requires a new status")
        return self


@router.post("/findings/{fid}/reviews")
async def review(fid: UUID, body: ReviewInput, conn: Conn, actor: Actor) -> dict[str, Any]:
    # Lock run then finding: serialize last-review notification with all other reviews.
    found = await deps.visible_finding(conn, actor, fid)
    lead(actor)
    run = await deps.visible_run(conn, actor, found["run_id"], lock=True)
    found = await deps.visible_finding(conn, actor, fid, lock=True)
    if found["version"] != body.version:
        raise APIError(
            409,
            "stale_version",
            "Finding changed; reload before reviewing",
            version=found["version"],
        )
    if body.decision == "change" and (
        body.new_status == "not_applicable" or len(body.note.strip()) < 10
    ):
        raise APIError(
            422,
            "unsupported_review",
            "Stored reviews require a supported status and at least 10 note characters for changes",
        )
    await conn.execute(
        insert(Review).values(
            finding_id=fid,
            reviewer_id=actor["id"],
            decision="confirm" if body.decision == "accept" else "change_status",
            new_status=body.new_status,
            note=body.note,
        )
    )
    values: dict[str, Any] = {"version": body.version + 1, "updated_at": func.now()}
    if body.decision == "change":
        values["status"] = body.new_status
    await conn.execute(
        update(Finding).where(Finding.id == fid, Finding.version == body.version).values(**values)
    )
    await audit(conn, actor, "review", fid, decision=body.decision, version=body.version + 1)
    pending = (
        await conn.execute(
            deps.visible(
                select(func.count())
                .select_from(Finding)
                .join(Run, Run.id == Finding.run_id)
                .join(Pack, Pack.id == Run.pack_id),
                actor["id"],
            ).where(
                Finding.run_id == found["run_id"],
                Finding.status == "needs_review",
                ~exists().where(Review.finding_id == Finding.id),
            )
        )
    ).scalar_one()
    if run["status"] == "waiting_review" and not pending:
        await wake(conn)
    return {
        **found,
        **values,
        "updated_at": (
            await conn.execute(select(Finding.updated_at).where(Finding.id == fid))
        ).scalar_one(),
    }
