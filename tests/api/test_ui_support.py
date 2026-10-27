"""API additions the web UI needs (U1): identity, teams, pack summaries, duplicates."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from cda.api.app import create_app
from cda.db.models import CheckSet, Finding, Run, Team, User
from cda.indexing.embed import HashEmbedder


async def team_id(api, name="Energy & Resources"):
    async with api[1].connect() as conn:
        return (await conn.execute(select(Team.id).where(Team.name == name))).scalar_one()


async def create(api, company="Wattlebrook Energy", year=2026, team="Energy & Resources"):
    return await api[0].post(
        "/packs",
        json={"company": company, "reporting_year": year, "team_id": str(await team_id(api, team))},
    )


async def add_run(api, pid, statuses, created_at):
    async with api[1].begin() as conn:
        await conn.execute(
            pg_insert(CheckSet)
            .values(version="v1", yaml_sha256="0" * 64)
            .on_conflict_do_nothing(index_elements=["version"])
        )
        rid = (
            await conn.execute(
                insert(Run)
                .values(
                    pack_id=UUID(pid),
                    method="A",
                    model_key="luna",
                    check_set_version="v1",
                    prompt_sha256="0" * 64,
                    budget_usd=2,
                    status="final",
                    idempotency_key=f"key-{created_at.isoformat()}",
                    created_by=await creator(conn),
                    created_at=created_at,
                )
                .returning(Run.id)
            )
        ).scalar_one()
        for i, status in enumerate(statuses):
            await conn.execute(
                insert(Finding).values(
                    run_id=rid, check_id=f"C{i}", status=status, rationale="", steps=0, cost_usd=0
                )
            )
    return str(rid)


async def creator(conn):
    return (await conn.execute(select(User.id).where(User.handle == "sarah"))).scalar_one()


async def test_me(api):
    alex = await api[0].get("/me", headers={"X-Demo-User": "alex"})
    assert alex.status_code == 200
    assert alex.json() == {"handle": "alex", "role": "analyst", "display_name": "Alex Nguyen"}
    async with AsyncClient(
        transport=ASGITransport(app=create_app(embedder=HashEmbedder())), base_url="http://test"
    ) as anonymous:
        assert (await anonymous.get("/me")).status_code == 401


async def test_teams(api):
    alex = (await api[0].get("/teams", headers={"X-Demo-User": "alex"})).json()
    assert [t["name"] for t in alex] == ["Energy & Resources"]
    assert {m["handle"] for m in alex[0]["members"]} == {"sarah", "alex"}
    assert {"id", "name", "members"} <= set(alex[0])
    sarah = (await api[0].get("/teams")).json()
    assert [t["name"] for t in sarah] == ["Energy & Resources", "Financial Services"]
    assert [m["handle"] for m in sarah[1]["members"]] == ["sarah"]


async def test_pack_summary(api):
    empty = (await create(api, "No Runs Ltd")).json()["id"]
    pid = (await create(api)).json()["id"]
    await add_run(api, pid, ["error"], datetime(2026, 10, 1, tzinfo=UTC))
    latest = await add_run(
        api,
        pid,
        ["evidence_found", "evidence_found", "inconsistent", "needs_review"],
        datetime(2026, 10, 2, tzinfo=UTC),
    )
    rows = {r["id"]: r for r in (await api[0].get("/packs")).json()}
    assert rows[empty]["latest_run"] is None
    assert rows[empty]["team_name"] == "Energy & Resources"
    summary = rows[pid]["latest_run"]
    assert summary["id"] == latest
    assert summary["status"] == "final"
    assert summary["counts"] == {"evidence_found": 2, "inconsistent": 1, "needs_review": 1}


async def test_duplicate_pack(api):
    first = await create(api)
    assert first.status_code == 201
    duplicate = await create(api, "  wattlebrook ENERGY ")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"] == "duplicate_pack"
    assert duplicate.json()["pack_id"] == first.json()["id"]
    assert (await create(api, year=2025)).status_code == 201


async def test_duplicate_pack_concurrent(api):
    responses = await asyncio.gather(*(create(api, "Race Pty") for _ in range(4)))
    assert sorted(r.status_code for r in responses) == [201, 409, 409, 409]
