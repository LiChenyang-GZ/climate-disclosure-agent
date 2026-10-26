import json
from uuid import UUID, uuid4

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy import delete, insert, select, text

from cda.db.models import AuditLog, Document, Evidence, Finding, Team, Unit, User
from cda.indexing.embed import HashEmbedder
from cda.mcp_server.server import build_server


async def setup_pack(api, team="Energy & Resources", indexed=True):
    import pymupdf

    async with api[1].connect() as conn:
        tid = (await conn.execute(select(Team.id).where(Team.name == team))).scalar_one()
    pid = (
        await api[0].post(
            "/packs", json={"company": "MCP", "reporting_year": 2026, "team_id": str(tid)}
        )
    ).json()["id"]
    if indexed:
        doc = pymupdf.open()
        doc.new_page().insert_text((72, 72), "Climate oversight evidence.")
        await api[0].post(
            f"/packs/{pid}/documents",
            files={"file": ("report.pdf", doc.tobytes(), "application/pdf")},
            data={"kind": "annual"},
        )
        doc.close()
        await api[0].post(f"/packs/{pid}/parse")
    return pid


def data(result):
    return json.loads(result.content[0].text)


@pytest.mark.parametrize(
    "resource", ["list_packs", "get_run", "list_findings", "detail", "search_pack", "start_check"]
)
async def test_visibility(api, monkeypatch, resource):
    pid = await setup_pack(api, "Financial Services")
    run = (
        await api[0].post(
            "/runs",
            json={"pack_id": pid, "method": "A", "model_key": "luna"},
            headers={"Idempotency-Key": str(uuid4())},
        )
    ).json()
    async with api[1].begin() as conn:
        fid = (
            await conn.execute(
                insert(Finding)
                .values(
                    run_id=UUID(run["id"]),
                    check_id="G1",
                    status="needs_review",
                    rationale="Evidence",
                    steps=0,
                    cost_usd=0,
                )
                .returning(Finding.id)
            )
        ).scalar_one()
    monkeypatch.setenv("CDA_MCP_USER", "alex")
    name, args = {
        "list_packs": ("list_packs", {}),
        "get_run": ("get_run", {"run_id": run["id"]}),
        "list_findings": ("get_findings", {"run_id": run["id"]}),
        "detail": ("get_findings", {"run_id": run["id"], "finding_id": str(fid)}),
        "search_pack": ("search_pack", {"pack_id": pid, "query": "climate"}),
        "start_check": (
            "start_check",
            {"pack_id": pid, "method": "A", "model_key": "luna", "idempotency_key": str(uuid4())},
        ),
    }[resource]
    async with create_connected_server_and_client_session(build_server(engine=api[1])) as session:
        result = await session.call_tool(name, args)
        if name == "list_packs":
            assert pid not in [p["id"] for p in data(result)["packs"]]
        else:
            assert result.isError
            assert data(result)["error"] == "not_found"


async def test_missing_seed(api, monkeypatch):
    async with api[1].begin() as conn:
        await conn.execute(delete(User).where(User.handle == "alex"))
    monkeypatch.setenv("CDA_MCP_USER", "alex")
    async with create_connected_server_and_client_session(build_server(engine=api[1])) as session:
        result = await session.call_tool("list_packs", {})
        assert result.isError
        assert data(result)["error"] == "unauthorized"


@pytest.mark.parametrize("mode", ["keyword", "semantic"])
async def test_search(api, monkeypatch, mode):
    pid = await setup_pack(api)
    monkeypatch.setenv("CDA_MCP_USER", "alex")
    from dataclasses import replace

    from cda.tools import registry

    original = registry.TOOLS[mode + "_search"]

    async def handler(ctx, args):
        assert (await ctx.conn.execute(text("select current_user"))).scalar_one() == "cda_tool"
        return await original.handler(ctx, args)

    monkeypatch.setitem(registry.TOOLS, original.name, replace(original, handler=handler))
    async with create_connected_server_and_client_session(
        build_server(engine=api[1], embedder=HashEmbedder())
    ) as session:
        result = await session.call_tool(
            "search_pack", {"pack_id": pid, "query": "climate", "mode": mode}
        )
        assert not result.isError, data(result)
        assert data(result)["category"] == "found"


async def test_creation_and_rest_parity(api, monkeypatch):
    pid = await setup_pack(api)
    monkeypatch.setenv("CDA_MCP_USER", "alex")
    args = {"pack_id": pid, "method": "A", "model_key": "luna", "idempotency_key": str(uuid4())}
    from cda.api import service

    original = service.wake
    notifications = []

    async def wake(conn):
        notifications.append(True)
        await original(conn)

    monkeypatch.setattr(service, "wake", wake)
    async with create_connected_server_and_client_session(build_server(engine=api[1])) as session:
        created = data(await session.call_tool("start_check", args))
        assert created["created"] is True, created
        row = created["run"]
        assert row["status"] == "queued"
        assert row["check_set_version"] and row["prompt_sha256"]
        assert notifications == [True]
        retry = data(await session.call_tool("start_check", args))
        assert retry["created"] is False and retry["run"]["id"] == row["id"]
        async with api[1].begin() as conn:
            audit = (
                await conn.execute(select(AuditLog.detail).where(AuditLog.target == row["id"]))
            ).scalar_one()
            assert audit == {"via": "mcp"}
            fid = (
                await conn.execute(
                    insert(Finding)
                    .values(
                        run_id=UUID(row["id"]),
                        check_id="G1",
                        status="needs_review",
                        rationale="Plain <quote>",
                        steps=0,
                        cost_usd=0,
                    )
                    .returning(Finding.id)
                )
            ).scalar_one()
        async with api[1].begin() as conn:
            uid = (
                await conn.execute(
                    select(Unit.id)
                    .join(Document, Document.id == Unit.document_id)
                    .where(Document.pack_id == UUID(pid), Unit.kind == "paragraph")
                    .limit(1)
                )
            ).scalar_one()
            await conn.execute(
                insert(Evidence).values(
                    finding_id=fid,
                    unit_id=uid,
                    quote="Climate oversight evidence.",
                    role="supports",
                    verified=True,
                )
            )
        for name, arguments, path, wrapper in [
            ("get_run", {"run_id": row["id"]}, f"/runs/{row['id']}", None),
            ("get_findings", {"run_id": row["id"]}, f"/runs/{row['id']}/findings", "findings"),
            (
                "get_findings",
                {"run_id": row["id"], "finding_id": str(fid)},
                f"/findings/{fid}",
                None,
            ),
        ]:
            result = await session.call_tool(name, arguments)
            assert not result.isError, data(result)
            actual = data(result)
            expected = (await api[0].get(path, headers={"X-Demo-User": "alex"})).json()
            assert (actual[wrapper] if wrapper else actual) == expected


@pytest.mark.parametrize(
    "case,code",
    [
        ("conflict", "idempotency_conflict"),
        ("model", "invalid_model"),
        ("unindexed", "pack_not_indexed"),
        ("wrong_run", "not_found"),
    ],
)
async def test_failures(api, monkeypatch, case, code):
    pid = await setup_pack(api, indexed=case != "unindexed")
    monkeypatch.setenv("CDA_MCP_USER", "alex")
    args = {"pack_id": pid, "method": "A", "model_key": "luna", "idempotency_key": str(uuid4())}
    async with create_connected_server_and_client_session(build_server(engine=api[1])) as session:
        if case in ("conflict", "wrong_run"):
            row = data(await session.call_tool("start_check", args))["run"]
        if case == "conflict":
            args["method"] = "B"
        if case == "model":
            args["model_key"] = "unknown"
        if case == "wrong_run":
            async with api[1].begin() as conn:
                fid = (
                    await conn.execute(
                        insert(Finding)
                        .values(
                            run_id=UUID(row["id"]),
                            check_id="G1",
                            status="needs_review",
                            rationale="Evidence",
                            steps=0,
                            cost_usd=0,
                        )
                        .returning(Finding.id)
                    )
                ).scalar_one()
            other = data(
                await session.call_tool("start_check", {**args, "idempotency_key": str(uuid4())})
            )["run"]
            result = await session.call_tool(
                "get_findings", {"run_id": other["id"], "finding_id": str(fid)}
            )
        else:
            result = await session.call_tool("start_check", args)
        assert result.isError
        assert data(result)["error"] == code
