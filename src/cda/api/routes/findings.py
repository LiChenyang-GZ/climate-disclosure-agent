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
from cda.api.service import finding_detail as finding_detail  # noqa: PLC0414
from cda.api.service import finding_rows as finding_rows  # noqa: PLC0414
from cda.db.models import (
    Finding,
    Pack,
    Review,
    Run,
)

router = APIRouter()


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
