"""Pack detail additions for the web UI (U2): status, team, limits, guards."""

from uuid import UUID

import pytest
from sqlalchemy import update
from test_api import indexed, pack, pdf, run, upload

from cda.db.models import Run


def statuses(app):
    return app.state.index_state


async def detail(api, pid):
    response = await api[0].get(f"/packs/{pid}")
    assert response.status_code == 200, response.text
    return response.json()


async def listed(api, pid):
    return next(p for p in (await api[0].get("/packs")).json() if p["id"] == pid)


async def two_documents(api):
    pid = await pack(api)
    ids = []
    for pages in (1, 2):
        response = await upload(api, pid, pdf(pages))
        assert response.status_code == 201, response.text
        ids.append(UUID(response.json()["id"]))
    return pid, ids


async def test_status_empty_and_uploaded(api):
    pid = await pack(api)
    assert (await detail(api, pid))["status"] == "empty"
    assert (await listed(api, pid))["status"] == "empty"
    assert (await upload(api, pid)).status_code == 201
    assert (await detail(api, pid))["status"] == "uploaded"
    assert (await listed(api, pid))["status"] == "uploaded"


async def test_status_parsing(api):
    pid, _ = await two_documents(api)
    statuses(api[2]).indexing.add(UUID(pid))
    assert (await detail(api, pid))["status"] == "parsing"
    assert (await listed(api, pid))["status"] == "parsing"


@pytest.mark.parametrize(
    ("failed", "expected"), [(0, "parsed"), (1, "parsed_with_issues"), (2, "parse_failed")]
)
async def test_status_after_parse(api, failed, expected):
    pid, ids = await two_documents(api)
    assert (await api[0].post(f"/packs/{pid}/parse")).status_code == 202
    for did in ids[:failed]:
        statuses(api[2]).index_errors[did] = "Indexing failed"
    assert (await detail(api, pid))["status"] == expected
    assert (await listed(api, pid))["status"] == expected


async def test_detail_team_limits_active_run(api):
    pid = await indexed(api)
    body = await detail(api, pid)
    assert body["team"]["name"] == "Energy & Resources"
    assert {m["handle"] for m in body["team"]["members"]} == {"sarah", "alex"}
    assert body["limits"] == {"max_upload_mb": 1, "max_documents": 20}
    assert body["active_run_id"] is None
    assert body["documents"][0]["unit_count"] >= 1
    rid = (await run(api, pid))["id"]
    assert (await detail(api, pid))["active_run_id"] == rid
    async with api[1].begin() as conn:
        await conn.execute(update(Run).where(Run.id == UUID(rid)).values(status="final"))
    assert (await detail(api, pid))["active_run_id"] is None


async def test_pack_full(api, monkeypatch):
    monkeypatch.setattr("cda.api.routes.packs.MAX_DOCUMENTS", 2)
    pid, _ = await two_documents(api)
    response = await upload(api, pid, pdf(3))
    assert response.status_code == 409
    assert response.json()["error"] == "pack_full"


async def test_upload_while_parsing(api):
    pid = await pack(api)
    statuses(api[2]).indexing.add(UUID(pid))
    response = await upload(api, pid)
    assert response.status_code == 409
    assert response.json()["error"] == "pack_parsing"


async def test_delete_blocked_by_active_run(api):
    pid = await indexed(api)
    did = (await detail(api, pid))["documents"][0]["id"]
    rid = (await run(api, pid))["id"]
    response = await api[0].delete(f"/packs/{pid}/documents/{did}")
    assert response.status_code == 409
    assert response.json()["error"] == "run_active"
    async with api[1].begin() as conn:
        await conn.execute(update(Run).where(Run.id == UUID(rid)).values(status="cancelled"))
    assert (await api[0].delete(f"/packs/{pid}/documents/{did}")).status_code == 200
    assert (await detail(api, pid))["documents"] == []
