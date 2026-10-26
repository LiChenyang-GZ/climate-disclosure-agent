import asyncio
import json
from uuid import UUID, uuid4

import pymupdf
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, insert, select, update

from cda.api.app import create_app
from cda.api.seed import seed
from cda.db.models import AuditLog, Finding, Run, RunEvent, Team, User
from cda.indexing.embed import HashEmbedder
from cda.runner.events import EventSink


def pdf(pages=1):
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page().insert_text((72, 72), "Climate oversight evidence.")
    data = doc.tobytes()
    doc.close()
    return data


async def pack(api, team="Energy & Resources"):
    async with api[1].connect() as conn:
        tid = (await conn.execute(select(Team.id).where(Team.name == team))).scalar_one()
    response = await api[0].post(
        "/packs", json={"company": "=Example", "reporting_year": 2026, "team_id": str(tid)}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def upload(api, pid, data=None, parts=None):
    return await api[0].post(
        f"/packs/{pid}/documents",
        files={"file": ("report.pdf", data if data is not None else pdf(), "application/pdf")},
        data={"kind": "annual", "parts": json.dumps(parts or [])},
    )


async def indexed(api, team="Energy & Resources"):
    pid = await pack(api, team)
    response = await upload(api, pid)
    assert response.status_code == 201, response.text
    assert response.json()["index_status"] == "pending"
    assert (await api[0].post(f"/packs/{pid}/parse")).status_code == 202
    assert (await api[0].get(f"/packs/{pid}")).json()["documents"][0]["index_status"] == "done"
    return pid


async def run(api, pid=None, key=None):
    pid = pid or await indexed(api)
    response = await api[0].post(
        "/runs",
        json={"pack_id": pid, "method": "A", "model_key": "luna"},
        headers={"Idempotency-Key": key or str(uuid4())},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def finding(api, rid):
    async with api[1].begin() as conn:
        return (
            await conn.execute(
                insert(Finding)
                .values(
                    run_id=UUID(rid),
                    check_id="G1",
                    status="needs_review",
                    rationale="* | [x](y) <script>",
                    steps=0,
                    cost_usd=0,
                )
                .returning(Finding.id)
            )
        ).scalar_one()


@pytest.mark.parametrize("handle", [None, "unknown"])
async def test_auth(handle):
    async with AsyncClient(
        transport=ASGITransport(app=create_app(embedder=HashEmbedder())), base_url="http://test"
    ) as client:
        response = await client.get(
            "/packs", headers={} if handle is None else {"X-Demo-User": handle}
        )
    assert response.status_code == 401
    assert set(response.json()) == {"error", "message"}


async def test_seed_idempotent(api):
    await seed(api[1])
    async with api[1].connect() as conn:
        for model in (User, Team):
            assert (await conn.execute(select(func.count()).select_from(model))).scalar_one() == 2


@pytest.mark.parametrize("resource", ["pack", "run", "findings", "export", "events", "finding"])
async def test_hidden_resources(api, resource):
    pid = await indexed(api, "Financial Services")
    rid = (await run(api, pid))["id"]
    fid = await finding(api, rid)
    paths = {
        "pack": f"/packs/{pid}",
        "run": f"/runs/{rid}",
        "findings": f"/runs/{rid}/findings",
        "export": f"/runs/{rid}/export",
        "events": f"/runs/{rid}/events",
        "finding": f"/findings/{fid}",
    }
    assert (await api[0].get(paths[resource], headers={"X-Demo-User": "alex"})).status_code == 404


@pytest.mark.parametrize("operation", ["create", "upload", "parts", "delete", "parse", "review"])
async def test_roles(api, operation):
    pid = await indexed(api)
    did = (await api[0].get(f"/packs/{pid}")).json()["documents"][0]["id"]
    rid = (await run(api, pid))["id"]
    fid = await finding(api, rid)
    api[0].headers["X-Demo-User"] = "alex"
    if operation == "create":
        response = await api[0].post(
            "/packs", json={"company": "X", "reporting_year": 2026, "team_id": str(uuid4())}
        )
    elif operation == "upload":
        response = await upload(api, pid)
    elif operation == "parts":
        response = await api[0].put(f"/packs/{pid}/documents/{did}/parts", json=[])
    elif operation == "delete":
        response = await api[0].delete(f"/packs/{pid}/documents/{did}")
    elif operation == "parse":
        response = await api[0].post(f"/packs/{pid}/parse")
    else:
        response = await api[0].post(
            f"/findings/{fid}/reviews", json={"decision": "accept", "note": "ok", "version": 1}
        )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "case,expected",
    [
        ("non_pdf", 415),
        ("oversize", 413),
        ("duplicate", 409),
        ("overlap", 422),
        ("range", 422),
        ("parse_error", 422),
        ("pages", 422),
    ],
)
async def test_upload_failures(api, case, expected):
    pid = await pack(api)
    data = pdf()
    parts = []
    if case == "non_pdf":
        data = b"text"
    elif case == "oversize":
        data = b"%PDF-" + b"x" * (1024 * 1024)
    elif case == "parse_error":
        data = b"%PDF-invalid"
    elif case == "pages":
        data = pdf(601)
    elif case == "overlap":
        parts = [{"kind": "annual", "first_pdf_page": 1, "last_pdf_page": 1}] * 2
    elif case == "range":
        parts = [{"kind": "annual", "first_pdf_page": 1, "last_pdf_page": 2}]
    elif case == "duplicate":
        assert (await upload(api, pid, data)).status_code == 201
    response = await upload(api, pid, data, parts)
    assert response.status_code == expected, response.text


async def test_parts_file_and_delete(api):
    pid = await pack(api)
    data = pdf()
    parts = [
        {"kind": "sustainability", "first_pdf_page": 1, "last_pdf_page": 1, "label": "Climate"}
    ]
    response = await upload(api, pid, data, parts)
    assert response.status_code == 201
    doc = response.json()
    assert doc["parts"][0]["label"] == "Climate"
    path = f"/packs/{pid}/documents/{doc['id']}"
    assert (await api[0].get(path + "/file")).content == data
    assert (await api[0].put(path + "/parts", json=[])).status_code == 200
    assert (await api[0].delete(path)).status_code == 200
    assert (await api[0].get(path + "/file")).status_code == 404


@pytest.mark.parametrize("pending_document", [False, True])
async def test_not_indexed(api, pending_document):
    pid = await pack(api)
    if pending_document:
        await upload(api, pid)
    response = await api[0].post(
        "/runs",
        json={"pack_id": pid, "method": "A", "model_key": "luna"},
        headers={"Idempotency-Key": "x"},
    )
    assert response.status_code == 409
    assert response.json()["error"] == "pack_not_indexed"


@pytest.mark.parametrize(
    "case,status",
    [
        ("missing", 400),
        ("LC", 422),
        ("zero", 422),
        ("eleven", 422),
        ("model", 422),
        ("different", 409),
    ],
)
async def test_run_failures(api, case, status):
    pid = await indexed(api)
    body = {
        "pack_id": pid,
        "method": "LC" if case == "LC" else "A",
        "model_key": "bad" if case == "model" else "luna",
    }
    if case in ("zero", "eleven"):
        body["budget_usd"] = 0 if case == "zero" else 11
    if case == "different":
        await run(api, pid, "key")
        body["method"] = "B"
    response = await api[0].post(
        "/runs", json=body, headers={} if case == "missing" else {"Idempotency-Key": "key"}
    )
    assert response.status_code == status, response.text


async def test_idempotency_analyst_cancel(api):
    pid = await indexed(api)
    api[0].headers["X-Demo-User"] = "alex"
    created = await run(api, pid, "key")
    assert created["status"] == "queued"
    assert len(created["prompt_sha256"]) == 64
    assert created["check_set_version"]
    repeat = await api[0].post(
        "/runs",
        json={"pack_id": pid, "method": "A", "model_key": "luna"},
        headers={"Idempotency-Key": "key"},
    )
    assert repeat.status_code == 200
    assert repeat.json()["id"] == created["id"]
    assert (await api[0].post(f"/runs/{created['id']}/cancel")).json()["status"] == "cancelled"
    assert (await api[0].get(f"/runs/{created['id']}/export")).status_code == 200


async def test_cancel_final(api):
    created = await run(api)
    async with api[1].begin() as conn:
        await conn.execute(update(Run).where(Run.id == UUID(created["id"])).values(status="final"))
    assert (await api[0].post(f"/runs/{created['id']}/cancel")).status_code == 409


@pytest.mark.parametrize(
    "case,status",
    [
        ("accept", 200),
        ("change", 200),
        ("missing_note", 422),
        ("accept_status", 422),
        ("stale", 409),
    ],
)
async def test_review(api, case, status):
    created = await run(api)
    fid = await finding(api, created["id"])
    async with api[1].begin() as conn:
        await conn.execute(
            update(Run).where(Run.id == UUID(created["id"])).values(status="waiting_review")
        )
    body = {
        "decision": "change" if case == "change" else "accept",
        "note": "Reviewed evidence",
        "version": 0 if case == "stale" else 1,
    }
    if case in ("change", "accept_status"):
        body["new_status"] = "partial_evidence"
    if case == "missing_note":
        del body["note"]
    response = await api[0].post(f"/findings/{fid}/reviews", json=body)
    assert response.status_code == status, response.text
    if status == 200:
        assert response.json()["version"] == 2
        async with api[1].connect() as conn:
            assert (
                await conn.execute(
                    select(func.count()).select_from(AuditLog).where(AuditLog.action == "review")
                )
            ).scalar_one() == 1
    if case == "stale":
        assert response.json()["version"] == 1


async def test_sse_replay_and_live(api):
    rid = UUID((await run(api))["id"])
    sink = EventSink(api[1])
    await sink.emit(rid, None, "old", {})
    async with api[1].connect() as conn:
        last = (await conn.execute(select(func.max(RunEvent.id)))).scalar_one()
    await sink.emit(rid, None, "progress", {"value": 1})

    async def emit():
        await asyncio.sleep(0.1)
        await sink.emit(rid, None, "run_final", {"done": True})

    task = asyncio.create_task(emit())
    response = await asyncio.wait_for(
        api[0].get(f"/runs/{rid}/events", headers={"Last-Event-ID": str(last)}), 5
    )
    await task
    assert "event: old" not in response.text
    assert response.text.index("event: progress") < response.text.index("event: run_final")


async def test_export_and_findings(api):
    rid = (await run(api))["id"]
    fid = await finding(api, rid)
    rows = (await api[0].get(f"/runs/{rid}/findings")).json()
    from cda.checks.loader import REQUIRED_CHECK_IDS

    assert [row["check_id"] for row in rows] == list(REQUIRED_CHECK_IDS)
    detail = (await api[0].get(f"/findings/{fid}")).json()
    assert detail["rationale"] == "* | [x](y) <script>"
    md = (await api[0].get(f"/runs/{rid}/export?format=md")).text
    assert "Draft" in md
    csv = (await api[0].get(f"/runs/{rid}/export?format=csv")).text
    assert "'=Example" in csv
    data = (await api[0].get(f"/runs/{rid}/export?format=json")).json()
    assert data["header"]["company"] == "=Example"
    async with api[1].begin() as conn:
        await conn.execute(update(Run).where(Run.id == UUID(rid)).values(status="final"))
    assert "Final" in (await api[0].get(f"/runs/{rid}/export?format=md")).text


async def test_safe_server_error(api, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("SELECT secret FROM /private/file")

    from cda.api import deps

    monkeypatch.setattr(deps, "visible_pack", fail)
    response = await api[0].get(f"/packs/{uuid4()}")
    assert response.status_code == 500
    assert response.json() == {"error": "internal_error", "message": "Request failed"}


async def test_notifications_for_enqueue_and_last_review(api):
    pid = await indexed(api)
    notified = asyncio.Queue()
    raw = await api[1].raw_connection()
    listener = raw.driver_connection

    def receive(*args):
        notified.put_nowait(args[-1])

    await listener.add_listener("cda_runs", receive)
    try:
        rid = (await run(api, pid))["id"]
        await asyncio.wait_for(notified.get(), 3)
        fid = await finding(api, rid)
        async with api[1].begin() as conn:
            await conn.execute(
                update(Run).where(Run.id == UUID(rid)).values(status="waiting_review")
            )
        response = await api[0].post(
            f"/findings/{fid}/reviews",
            json={"decision": "accept", "note": "Reviewed", "version": 1},
        )
        assert response.status_code == 200
        await asyncio.wait_for(notified.get(), 3)
    finally:
        await listener.remove_listener("cda_runs", receive)
        raw.close()


async def test_evidence_facts_inconsistencies_and_trace(api):
    from cda.db.models import Document, Evidence, Fact, Inconsistency, LLMCall, Unit

    rid = (await run(api))["id"]
    fid = await finding(api, rid)
    quote = "* | [x](y) <script>"
    async with api[1].begin() as conn:
        unit = (
            (
                await conn.execute(
                    select(Unit.id, Unit.document_id, Unit.pdf_page).where(Unit.kind == "paragraph")
                )
            )
            .mappings()
            .first()
        )
        await conn.execute(
            insert(Evidence).values(
                finding_id=fid, unit_id=unit["id"], quote=quote, role="supports", verified=True
            )
        )
        await conn.execute(
            insert(Fact).values(
                finding_id=fid,
                unit_id=unit["id"],
                name="emissions",
                value_num=10,
                unit="tCO2e",
                scope="group",
                raw="10 tCO2e",
                year=2026,
            )
        )
        await conn.execute(
            insert(Inconsistency).values(
                run_id=UUID(rid), finding_id=fid, type="T3", detail={"message": "Compare figures"}
            )
        )
        await conn.execute(
            insert(LLMCall).values(
                run_id=UUID(rid),
                check_id="G1",
                step=1,
                model="fake",
                prompt_sha256="a" * 64,
                tools_sha256="b" * 64,
                tokens_in=2,
                tokens_out=3,
                cost_usd=0,
                latency_ms=10,
            )
        )
        did = str(unit["document_id"])
        pid = str(
            (
                await conn.execute(
                    select(Document.pack_id).where(Document.id == unit["document_id"])
                )
            ).scalar_one()
        )
    await EventSink(api[1]).emit(UUID(rid), "G1", "tool_called", {"tool": "read_unit"})
    detail = (await api[0].get(f"/findings/{fid}")).json()
    assert detail["evidence"][0]["quote"] == quote
    assert detail["evidence"][0]["verified"] is True
    assert (
        detail["evidence"][0]["link"]
        == f"/packs/{pid}/documents/{did}/file#page={unit['pdf_page']}"
    )
    assert detail["facts"][0]["name"] == "emissions"
    assert detail["inconsistencies"][0]["type"] == "T3"
    assert detail["trace"]["llm_calls"][0]["latency_ms"] == 10
    assert detail["trace"]["tool_called"][0]["payload"]["tool"] == "read_unit"
    md = (await api[0].get(f"/runs/{rid}/export?format=md")).text
    assert r"\* \| \[x\]\(y\) \<script\>" in md
    rows = (await api[0].get(f"/runs/{rid}/findings")).json()
    assert rows[0]["evidence_count"] == 1
    assert rows[0]["version"] == 1


async def test_index_failure_and_retry(api):
    class FailingEmbedder(HashEmbedder):
        def embed_passages(self, texts):
            raise RuntimeError("internal details")

    pid = await pack(api)
    await upload(api, pid)
    api[2].state.embedder = FailingEmbedder()
    assert (await api[0].post(f"/packs/{pid}/parse")).status_code == 202
    doc = (await api[0].get(f"/packs/{pid}")).json()["documents"][0]
    assert doc["index_status"] == "failed"
    api[2].state.embedder = HashEmbedder()
    assert (await api[0].post(f"/packs/{pid}/parse")).status_code == 202
    assert (await api[0].get(f"/packs/{pid}")).json()["documents"][0]["index_status"] == "done"


async def test_index_repeated_request(api):
    pid = await pack(api)
    await upload(api, pid)
    api[2].state.indexing.add(UUID(pid))
    assert (await api[0].post(f"/packs/{pid}/parse")).json() == {"status": "indexing"}
    assert UUID(pid) in api[2].state.indexing


async def test_review_state_is_per_finding(api):
    rid = (await run(api))["id"]
    fid = await finding(api, rid)
    async with api[1].begin() as conn:
        await conn.execute(
            insert(Finding).values(
                run_id=UUID(rid),
                check_id="G2",
                status="needs_review",
                rationale="Review separately",
                steps=0,
                cost_usd=0,
            )
        )
    response = await api[0].post(
        f"/findings/{fid}/reviews", json={"decision": "accept", "note": "Reviewed", "version": 1}
    )
    assert response.status_code == 200
    rows = (await api[0].get(f"/runs/{rid}/findings")).json()
    assert rows[0]["review_state"] == "reviewed"
    assert rows[1]["review_state"] == "none"


@pytest.mark.parametrize(
    "stored,code,message",
    [
        ("llm_auth", "llm_auth", "The OpenAI API key is missing or was rejected."),
        (
            "IntegrityError",
            "internal_error",
            "An internal error prevented the run from completing.",
        ),
        ("future_code", "future_code", "The run could not be completed."),
        ("bad/code", "internal_error", "An internal error prevented the run from completing."),
    ],
)
async def test_failed_run_errors_all_responses(api, stored, code, message):
    key = str(uuid4())
    created = await run(api, key=key)
    async with api[1].begin() as conn:
        await conn.execute(
            update(Run).where(Run.id == UUID(created["id"])).values(status="failed", error=stored)
        )
    detail = (await api[0].get(f"/runs/{created['id']}")).json()
    pack_detail = (await api[0].get(f"/packs/{created['pack_id']}")).json()
    replay = await api[0].post(
        "/runs",
        json={"pack_id": created["pack_id"], "method": "A", "model_key": "luna"},
        headers={"Idempotency-Key": key},
    )
    assert replay.status_code == 200
    for result in (detail, pack_detail["latest_runs"][0], replay.json()):
        assert result["error"] == code
        assert result["error_message"] == message
