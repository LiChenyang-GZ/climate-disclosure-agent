"""New check support for the web UI (U3, D83): skipped files, check subset, guards."""

from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select, update
from test_api import indexed, pack, pdf, upload

from cda.checks.loader import default_check_set
from cda.db.models import Run
from cda.tools.context import ToolContext
from cda.tools.registry import run_tool


async def start(api, pid, key=None, **extra):
    return await api[0].post(
        "/runs",
        json={"pack_id": pid, "method": "A", "model_key": "luna", **extra},
        headers={"Idempotency-Key": key or str(uuid4())},
    )


async def parsed_with_issues(api):
    pid = await pack(api)
    ids = []
    for pages in (1, 2):
        response = await upload(api, pid, pdf(pages))
        assert response.status_code == 201, response.text
        ids.append(response.json()["id"])
    assert (await api[0].post(f"/packs/{pid}/parse")).status_code == 202
    api[2].state.index_state.index_errors[UUID(ids[0])] = "Indexing failed"
    assert (await api[0].get(f"/packs/{pid}")).json()["status"] == "parsed_with_issues"
    return pid, ids


async def test_parsed_with_issues_skips_failed_document(api):
    pid, (failed, kept) = await parsed_with_issues(api)
    response = await start(api, pid)
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["skipped_documents"] == [{"id": failed, "filename": "report.pdf"}]
    assert (await api[0].get(f"/runs/{run['id']}")).json()["skipped_documents"] == [
        {"id": failed, "filename": "report.pdf"}
    ]

    async with api[1].connect() as conn:
        uid = (await conn.execute(select(Run.created_by).where(Run.id == UUID(run["id"])))).scalar()
        plain = ToolContext(conn, uid, UUID(pid))
        hits = (await run_tool(plain, "keyword_search", {"query": "climate"})).data["hits"]
        assert {h["document_id"] for h in hits} == {failed, kept}
        scoped = ToolContext(conn, uid, UUID(pid), excluded_documents=frozenset({UUID(failed)}))
        hits = (await run_tool(scoped, "keyword_search", {"query": "climate"})).data["hits"]
        assert {h["document_id"] for h in hits} == {kept}
        listed = (await run_tool(scoped, "list_documents", {})).data["documents"]
        assert [d["document_id"] for d in listed] == [kept]


async def test_all_failed_is_not_indexed(api):
    pid, ids = await parsed_with_issues(api)
    api[2].state.index_state.index_errors[UUID(ids[1])] = "Indexing failed"
    response = await start(api, pid)
    assert response.status_code == 409
    assert response.json()["error"] == "pack_not_indexed"


async def test_pending_document_is_not_indexed(api):
    pid = await indexed(api)
    assert (await upload(api, pid, pdf(3))).status_code == 201
    response = await start(api, pid)
    assert response.status_code == 409
    assert response.json()["error"] == "pack_not_indexed"


async def test_check_subset(api):
    pid = await indexed(api)
    response = await start(api, pid, key="subset", check_ids=["S1", "G1"])
    assert response.status_code == 201, response.text
    # Stored in check-set order, so the same selection in another order is the same request.
    assert response.json()["check_ids"] == ["G1", "S1"]
    again = await start(api, pid, key="subset", check_ids=["G1", "S1"])
    assert again.status_code == 200
    other = await start(api, pid, key="subset", check_ids=["G1"])
    assert other.status_code == 409
    assert other.json()["error"] == "idempotency_conflict"


@pytest.mark.parametrize("check_ids", [[], ["G1", "G1"], ["Z9"]])
async def test_invalid_check_subset(api, check_ids):
    pid = await indexed(api)
    response = await start(api, pid, check_ids=check_ids)
    assert response.status_code == 422


async def test_second_active_run_refused(api):
    pid = await indexed(api)
    first = (await start(api, pid)).json()
    response = await start(api, pid)
    assert response.status_code == 409
    assert response.json() == {
        "error": "run_active",
        "message": "A check is already running on this pack",
        "run_id": first["id"],
    }
    async with api[1].begin() as conn:
        await conn.execute(update(Run).where(Run.id == UUID(first["id"])).values(status="final"))
    assert (await start(api, pid)).status_code == 201


@pytest.mark.parametrize(
    ("key", "fake", "status"),
    [(None, False, 409), ("  ", False, 409), ("sk-x", False, 201), (None, True, 201)],
)
async def test_api_key_missing(api, key, fake, status):
    pid = await indexed(api)
    settings = api[2].state.settings
    settings.openai_api_key = None if key is None else SecretStr(key)
    settings.cda_fake_llm = fake
    response = await start(api, pid)
    assert response.status_code == status, response.text
    if status == 409:
        assert response.json()["error"] == "llm_key_missing"
        async with api[1].connect() as conn:
            assert (
                await conn.execute(select(Run.id).where(Run.pack_id == UUID(pid)))
            ).first() is None


async def test_run_options(api):
    body = (await api[0].get("/run-options", headers={"X-Demo-User": "alex"})).json()
    assert [c["id"] for c in body["checks"]] == [c.id for c in default_check_set().checks]
    assert len(body["checks"]) == 15
    assert {m["key"] for m in body["models"]} == {"luna", "sol"}
    assert body["methods"] == ["A", "B", "C"]
    for method in body["methods"]:
        for model in ("luna", "sol"):
            low, high = body["estimate_per_check_usd"][method][model]
            assert 0 < low <= high
    assert (await api[0].get("/run-options", headers={"X-Demo-User": "nobody"})).status_code == 401
