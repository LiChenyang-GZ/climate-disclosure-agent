"""OneDrive / SharePoint import through the REST API against a fake Graph (X2, D84)."""

import json
import logging
import re
from pathlib import Path
from urllib.parse import parse_qs
from uuid import UUID

import httpx
import pytest
from sqlalchemy import insert, select, update
from test_api import finding, pack, pdf, run
from test_office_upload import docx_bytes

import cda.api.routes.packs as packs_routes
from cda.db.models import AuditLog, Document, Evidence, Run, Unit

FIXTURES = Path(__file__).parents[1] / "integrations" / "fixtures" / "graph"
DRIVE = "b!sustainability-documents"
SITE_1 = "wattlebrook.sharepoint.com,11111111-1111-4111-8111-111111111111,22222222-2222-4222-8222-222222222222"
SITE_2 = "wattlebrook.sharepoint.com,33333333-3333-4333-8333-333333333333,44444444-4444-4444-8444-444444444444"
SECRETS = (
    "FAKE-ACCESS-TOKEN",
    "FAKE-REFRESH-TOKEN",
    "FAKE-ID-TOKEN",
    "FAKE-DEVICE-CODE",
    "FAKE-TEMPAUTH",
)


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text("utf-8"))


def reply(status: int, name: str) -> httpx.Response:
    return httpx.Response(status, json=fixture(name))


class FakeMicrosoft:
    """login.microsoftonline.com, graph.microsoft.com and a download host, in memory."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.polls = [reply(200, "token")]
        self.refresh = reply(200, "token_refreshed")
        self.accepted = {"FAKE-ACCESS-TOKEN-1", "FAKE-ACCESS-TOKEN-2"}
        self.items: dict[str, dict] = {}
        self.contents: dict[str, bytes] = {}
        self.status: dict[str, int] = {}

    def add(self, item_id: str, name: str, content: bytes, version: int = 1) -> None:
        self.items[item_id] = {
            "id": item_id,
            "name": name,
            "eTag": f'"{{{item_id}}},{version}"',
            "cTag": f'"c:{{{item_id}}},{version}"',
            "size": len(content),
            "webUrl": f"https://wattlebrook.sharepoint.com/sites/Sustainability/Shared%20Documents/Drafts/{name}",
            "lastModifiedDateTime": "2026-10-20T00:00:00Z",
            "parentReference": {
                "driveType": "documentLibrary",
                "driveId": DRIVE,
                "id": "01DRAFTSFOLDER",
                "path": f"/drives/{DRIVE}/root:/Drafts",
            },
            "file": {"mimeType": "application/octet-stream"},
        }
        self.contents[item_id] = content

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "login.microsoftonline.com":
            if path.endswith("/devicecode"):
                return reply(200, "devicecode")
            grant = parse_qs(request.content.decode())["grant_type"][0]
            return self.refresh if grant == "refresh_token" else self.polls.pop(0)
        if host == "download.example.test":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=self.contents[path.strip("/")])
        assert host == "graph.microsoft.com", request.url
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token not in self.accepted:
            return reply(401, "error_401")
        path = path.removeprefix("/v1.0")
        skip = "skiptoken" in str(request.url)
        if path == "/me":
            return reply(200, "me")
        if path == "/me/drive/root/children":
            return reply(200, "my_drive_root_children")
        if path == "/sites":
            return reply(200, "sites_page2" if skip else "sites_page1")
        if re.fullmatch(r"/sites/[^/]+/drives", path):
            return reply(200, "site_drives")
        match = re.fullmatch(r"/drives/([^/]+)/items/([^/]+)(/children|/content)?", path)
        assert match, path
        item_id, tail = match.group(2), match.group(3)
        if item_id in self.status:
            status = self.status[item_id]
            return reply(status, f"error_{status}")
        if tail == "/children":
            if item_id == "01DRAFTSFOLDER":
                return reply(200, "drafts_children_page2" if skip else "drafts_children_page1")
            return reply(200, "empty_children")
        if item_id not in self.items:
            return reply(404, "error_404")
        if tail == "/content":
            return httpx.Response(
                302,
                headers={
                    "Location": f"https://download.example.test/{item_id}?tempauth=FAKE-TEMPAUTH-{item_id}"
                },
            )
        return httpx.Response(200, json=self.items[item_id])


@pytest.fixture
def ms(api):
    app = api[2]
    app.state.settings = app.state.settings.model_copy(
        update={"cda_ms_client_id": "00000000-test-client-id"}
    )
    fake = FakeMicrosoft()
    app.state.microsoft.transport = httpx.MockTransport(fake)

    async def no_sleep(seconds: float) -> None:
        return None

    app.state.microsoft.sleep = no_sleep
    return fake


async def connect(api) -> None:
    client = api[0]
    assert (await client.post("/integrations/microsoft/device-code")).status_code == 200
    response = await client.post("/integrations/microsoft/poll")
    assert response.json()["state"] == "connected", response.text


async def import_items(api, pid, *items, kind="other"):
    return await api[0].post(
        f"/packs/{pid}/import",
        json={"items": [{"drive_id": DRIVE, "item_id": i, "kind": kind} for i in items]},
    )


def by_item(results):
    return {r["item_id"]: r for r in results}


# --- 1. device code ----------------------------------------------------------------------------


async def test_device_code_connect_and_no_secrets_leak(api, ms, caplog):
    caplog.set_level(logging.DEBUG)
    ms.polls = [reply(400, "error_authorization_pending"), reply(200, "token")]
    client = api[0]
    texts = []

    status = await client.get("/integrations/microsoft")
    assert status.json() == {"configured": True, "connected": False, "account": None}
    start = await client.post("/integrations/microsoft/device-code")
    assert start.status_code == 200, start.text
    assert start.json() == {
        "user_code": "FAKE7Q2K",
        "verification_uri": "https://microsoft.com/devicelogin",
        "expires_in": 900,
        "interval": 5,
    }
    pending = await client.post("/integrations/microsoft/poll")
    assert pending.json() == {"state": "pending"}
    connected = await client.post("/integrations/microsoft/poll")
    assert connected.json() == {"state": "connected", "account": "sarah.chen@wattlebrook.example"}
    status = await client.get("/integrations/microsoft")
    assert status.json() == {
        "configured": True,
        "connected": True,
        "account": "sarah.chen@wattlebrook.example",
    }
    me = [r for r in ms.requests if r.url.path == "/v1.0/me"]
    assert me[0].headers["authorization"] == "Bearer FAKE-ACCESS-TOKEN-1"

    # Use the connection end to end so every code path has a chance to log.
    pid = await pack(api)
    ms.add("01CLIMATEREPORTPDF", "climate-report-2026.pdf", pdf())
    texts += [r.text for r in (start, pending, connected, status)]
    texts.append((await client.get("/integrations/microsoft/browse")).text)
    texts.append((await import_items(api, pid, "01CLIMATEREPORTPDF")).text)
    texts.append((await client.get(f"/packs/{pid}")).text)
    texts.append((await client.get(f"/packs/{pid}/sync")).text)
    async with api[1].connect() as conn:
        audit = (await conn.execute(select(AuditLog.target, AuditLog.detail))).all()
    texts.append(json.dumps([[a.target, a.detail] for a in audit]))
    for secret in SECRETS:
        assert secret not in caplog.text
        assert all(secret not in text for text in texts), secret


@pytest.mark.parametrize(
    ("error", "state"),
    [("error_authorization_declined", "cancelled"), ("error_expired_token", "expired")],
)
async def test_device_code_declined_or_expired(api, ms, error, state):
    ms.polls = [reply(400, error)]
    client = api[0]
    assert (await client.post("/integrations/microsoft/device-code")).status_code == 200
    assert (await client.post("/integrations/microsoft/poll")).json() == {"state": state}
    assert (await client.get("/integrations/microsoft")).json()["connected"] is False
    # The flow is over: polling again does not call Microsoft and reports it expired.
    count = len(ms.requests)
    assert (await client.post("/integrations/microsoft/poll")).json() == {"state": "expired"}
    assert len(ms.requests) == count


async def test_device_code_runs_out_locally(api, ms):
    now = [1000.0]
    api[2].state.microsoft.clock = lambda: now[0]
    assert (await api[0].post("/integrations/microsoft/device-code")).status_code == 200
    now[0] += 901
    count = len(ms.requests)
    assert (await api[0].post("/integrations/microsoft/poll")).json() == {"state": "expired"}
    assert len(ms.requests) == count


async def test_not_configured(api):
    client = api[0]
    assert (await client.get("/integrations/microsoft")).json() == {
        "configured": False,
        "connected": False,
        "account": None,
    }
    response = await client.post("/integrations/microsoft/device-code")
    assert response.status_code == 409
    assert response.json()["error"] == "microsoft_not_configured"


async def test_disconnect(api, ms):
    await connect(api)
    response = await api[0].delete("/integrations/microsoft")
    assert response.status_code == 200
    assert (await api[0].get("/integrations/microsoft")).json()["connected"] is False
    response = await api[0].get("/integrations/microsoft/browse")
    assert response.status_code == 409
    assert response.json()["error"] == "microsoft_expired"


# --- 2. browse ---------------------------------------------------------------------------------


async def test_browse_root_sites_and_folders(api, ms):
    await connect(api)
    client = api[0]
    root = (await client.get("/integrations/microsoft/browse")).json()
    assert [(s["id"], s["name"], s["kind"]) for s in root["sites"]] == [
        ("me", "My files", "onedrive"),
        (SITE_1, "Sustainability Reporting", "sharepoint"),
        (SITE_2, "Finance", "sharepoint"),
    ]

    mine = (await client.get("/integrations/microsoft/browse", params={"site_id": "me"})).json()
    assert [f["name"] for f in mine["folders"]] == ["Board papers"]
    assert mine["folders"][0]["drive_id"] == "b!onedrive-sarah"
    assert [f["name"] for f in mine["files"]] == ["Climate notes.docx"]

    site = (
        await client.get(
            "/integrations/microsoft/browse", params={"site_id": root["sites"][1]["id"]}
        )
    ).json()
    assert [(d["id"], d["name"]) for d in site["drives"]] == [
        (DRIVE, "Documents"),
        ("b!sustainability-archive", "Archive"),
    ]

    folder = (
        await client.get(
            "/integrations/microsoft/browse",
            params={"drive_id": DRIVE, "item_id": "01DRAFTSFOLDER"},
        )
    ).json()
    assert [f["name"] for f in folder["folders"]] == ["Old versions"]
    # Both pages, supported files only (no .pptx, no OneNote package).
    assert [f["name"] for f in folder["files"]] == [
        "climate-report-2026.pdf",
        "Emissions data.XLSX",
        "transition-plan.docx",
    ]
    assert folder["files"][0] == {
        "drive_id": DRIVE,
        "item_id": "01CLIMATEREPORTPDF",
        "name": "climate-report-2026.pdf",
        "path": "/Drafts/climate-report-2026.pdf",
        "size": 2048,
        "last_modified": "2026-10-20T00:00:00Z",
    }

    empty = (
        await client.get(
            "/integrations/microsoft/browse",
            params={"drive_id": DRIVE, "item_id": "01SUBFOLDER0000001"},
        )
    ).json()
    assert empty["folders"] == [] and empty["files"] == []


async def test_browse_rejects_odd_ids(api, ms):
    await connect(api)
    response = await api[0].get(
        "/integrations/microsoft/browse", params={"drive_id": "..", "item_id": "x"}
    )
    assert response.status_code == 422
    response = await api[0].get(
        "/integrations/microsoft/browse", params={"drive_id": "a/b", "item_id": "x"}
    )
    assert response.status_code == 422


async def test_browse_denied(api, ms):
    await connect(api)
    ms.status["01SECRETFOLDER"] = 403
    response = await api[0].get(
        "/integrations/microsoft/browse", params={"drive_id": DRIVE, "item_id": "01SECRETFOLDER"}
    )
    assert response.status_code == 403
    assert response.json() == {
        "error": "microsoft_denied",
        "message": "Access denied in SharePoint",
    }


# --- 3. import ---------------------------------------------------------------------------------


async def test_import_two_files_parsed_like_uploads(api, ms):
    await connect(api)
    pid = await pack(api)
    report, plan = pdf(), docx_bytes()
    ms.add("01CLIMATEREPORTPDF", "climate-report-2026.pdf", report)
    ms.add("01TRANSITIONDOCX", "transition-plan.docx", plan)
    response = await import_items(api, pid, "01CLIMATEREPORTPDF", "01TRANSITIONDOCX")
    assert response.status_code == 200, response.text
    results = by_item(response.json())
    assert {k: v["status"] for k, v in results.items()} == {
        "01CLIMATEREPORTPDF": "imported",
        "01TRANSITIONDOCX": "imported",
    }
    document = results["01CLIMATEREPORTPDF"]["document"]
    assert document["filename"] == "climate-report-2026.pdf"
    assert document["kind"] == "other"
    assert document["index_status"] == "pending"
    assert document["source"] == {
        "provider": "microsoft",
        "drive_id": DRIVE,
        "item_id": "01CLIMATEREPORTPDF",
        "path": "/Drafts/climate-report-2026.pdf",
        "etag": '"{01CLIMATEREPORTPDF},1"',
        "web_url": "https://wattlebrook.sharepoint.com/sites/Sustainability/Shared%20Documents/Drafts/climate-report-2026.pdf",
    }
    assert results["01TRANSITIONDOCX"]["document"]["parser_version"].startswith("cda-office-1/")

    # Stored like an upload: downloadable, parsed by the same parse step.
    file = await api[0].get(f"/packs/{pid}/documents/{document['id']}/file")
    assert file.content == report
    assert (await api[0].post(f"/packs/{pid}/parse")).status_code == 202
    detail = (await api[0].get(f"/packs/{pid}")).json()
    assert detail["status"] == "parsed"
    assert sorted(d["source"]["path"] for d in detail["documents"]) == [
        "/Drafts/climate-report-2026.pdf",
        "/Drafts/transition-plan.docx",
    ]
    async with api[1].connect() as conn:
        actions = (await conn.execute(select(AuditLog.action))).scalars().all()
    assert actions.count("import_document") == 2


async def test_import_denied_duplicate_unsupported_and_parse_errors(api, ms):
    await connect(api)
    pid = await pack(api)
    report = pdf()
    ms.add("01CLIMATEREPORTPDF", "climate-report-2026.pdf", report)
    ms.add("01COPYOFREPORT", "copy of report.pdf", report)
    ms.add("01SECRETPDF", "board-only.pdf", pdf(2))
    ms.status["01SECRETPDF"] = 403
    ms.add("01BOARDDECKPPTX", "board-deck.pptx", b"PK\x03\x04deck")
    ms.add("01BROKENXLSX", "broken.xlsx", b"PK\x03\x04broken")
    ms.add("01MISSING", "gone.pdf", pdf(3))
    del ms.contents["01MISSING"]
    ms.status["01MISSING"] = 404
    response = await import_items(
        api,
        pid,
        "01CLIMATEREPORTPDF",
        "01SECRETPDF",
        "01COPYOFREPORT",
        "01BOARDDECKPPTX",
        "01BROKENXLSX",
        "01MISSING",
    )
    assert response.status_code == 200, response.text
    results = by_item(response.json())
    assert results["01CLIMATEREPORTPDF"]["status"] == "imported"
    assert results["01SECRETPDF"] == {
        "item_id": "01SECRETPDF",
        "status": "denied",
        "error": "access_denied",
        "message": "Access denied in SharePoint",
    }
    assert results["01COPYOFREPORT"]["status"] == "duplicate"
    assert results["01COPYOFREPORT"]["error"] == "duplicate_document"
    assert results["01BOARDDECKPPTX"]["status"] == "rejected"
    assert results["01BOARDDECKPPTX"]["error"] == "unsupported_format"
    assert results["01BROKENXLSX"]["status"] == "rejected"
    assert results["01BROKENXLSX"]["error"] == "invalid_file"
    assert results["01MISSING"]["status"] == "failed"
    assert results["01MISSING"]["error"] == "not_found"
    # The .pptx was refused before downloading it.
    assert not any(r.url.path.endswith("01BOARDDECKPPTX/content") for r in ms.requests)
    documents = (await api[0].get(f"/packs/{pid}")).json()["documents"]
    assert [d["filename"] for d in documents] == ["climate-report-2026.pdf"]


async def test_import_size_limit_from_metadata(api, ms):
    await connect(api)
    pid = await pack(api)
    ms.add("01HUGEPDF", "huge.pdf", b"%PDF-" + b"x" * (1024 * 1024))
    results = by_item((await import_items(api, pid, "01HUGEPDF")).json())
    assert results["01HUGEPDF"]["status"] == "rejected"
    assert results["01HUGEPDF"]["error"] == "upload_too_large"
    assert not any(r.url.host == "download.example.test" for r in ms.requests)


async def test_import_respects_pack_limit(api, ms, monkeypatch):
    monkeypatch.setattr(packs_routes, "MAX_DOCUMENTS", 1)
    await connect(api)
    pid = await pack(api)
    ms.add("01CLIMATEREPORTPDF", "climate-report-2026.pdf", pdf())
    ms.add("01TRANSITIONDOCX", "transition-plan.docx", docx_bytes())
    results = by_item(
        (await import_items(api, pid, "01CLIMATEREPORTPDF", "01TRANSITIONDOCX")).json()
    )
    assert results["01CLIMATEREPORTPDF"]["status"] == "imported"
    assert results["01TRANSITIONDOCX"]["status"] == "rejected"
    assert results["01TRANSITIONDOCX"]["error"] == "pack_full"


async def test_import_while_parsing(api, ms):
    await connect(api)
    pid = await pack(api)
    ms.add("01CLIMATEREPORTPDF", "climate-report-2026.pdf", pdf())
    api[2].state.indexing.add(UUID(pid))
    response = await import_items(api, pid, "01CLIMATEREPORTPDF")
    assert response.status_code == 409
    assert response.json()["error"] == "pack_parsing"
    api[2].state.indexing.discard(UUID(pid))


async def test_import_body_limits(api, ms):
    await connect(api)
    pid = await pack(api)
    too_many = [f"01ITEM{i:04d}" for i in range(21)]
    assert (await import_items(api, pid, *too_many)).status_code == 422
    assert (await api[0].post(f"/packs/{pid}/import", json={"items": []})).status_code == 422


# --- 4. expired --------------------------------------------------------------------------------


async def test_graph_401_is_microsoft_expired(api, ms):
    await connect(api)
    pid = await pack(api)
    ms.add("01CLIMATEREPORTPDF", "climate-report-2026.pdf", pdf())
    ms.accepted = set()
    response = await import_items(api, pid, "01CLIMATEREPORTPDF")
    assert response.status_code == 409
    assert response.json() == {
        "error": "microsoft_expired",
        "message": "Your Microsoft connection expired. Reconnect.",
    }
    assert (await api[0].get("/integrations/microsoft")).json()["connected"] is False
    assert (await api[0].get(f"/packs/{pid}")).json()["documents"] == []


async def test_refresh_failure_is_microsoft_expired(api, ms):
    now = [0.0]
    api[2].state.microsoft.clock = lambda: now[0]
    await connect(api)
    now[0] += 4000  # access token expired; refresh is attempted and refused
    ms.refresh = reply(400, "error_invalid_grant")
    response = await api[0].get("/integrations/microsoft/browse")
    assert response.status_code == 409
    assert response.json()["error"] == "microsoft_expired"
    assert any(
        parse_qs(r.content.decode()).get("grant_type") == ["refresh_token"] for r in ms.requests
    )


async def test_refresh_success_is_transparent(api, ms):
    now = [0.0]
    api[2].state.microsoft.clock = lambda: now[0]
    await connect(api)
    now[0] += 4000
    ms.accepted = {"FAKE-ACCESS-TOKEN-2"}
    assert (await api[0].get("/integrations/microsoft/browse")).status_code == 200


async def test_not_connected_is_microsoft_expired(api, ms):
    pid = await pack(api)
    for response in (
        await api[0].get("/integrations/microsoft/browse"),
        await import_items(api, pid, "01CLIMATEREPORTPDF"),
        await api[0].get(f"/packs/{pid}/sync"),
    ):
        assert response.status_code == 409
        assert response.json()["error"] == "microsoft_expired"


# --- 5. sync -----------------------------------------------------------------------------------


async def imported_pack(api, ms):
    await connect(api)
    pid = await pack(api)
    ms.add("01CLIMATEREPORTPDF", "climate-report-2026.pdf", pdf())
    response = await import_items(api, pid, "01CLIMATEREPORTPDF", kind="annual")
    document = by_item(response.json())["01CLIMATEREPORTPDF"]["document"]
    assert (await api[0].post(f"/packs/{pid}/parse")).status_code == 202
    return pid, document


async def test_sync_unchanged(api, ms):
    pid, _ = await imported_pack(api, ms)
    # An uploaded file is not part of re-sync.
    await api[0].post(
        f"/packs/{pid}/documents",
        files={"file": ("upload.pdf", pdf(2), "application/pdf")},
        data={"kind": "other"},
    )
    response = await api[0].get(f"/packs/{pid}/sync")
    assert response.status_code == 200, response.text
    assert response.json() == {"changed": [], "removed": [], "failed": [], "unchanged": 1}


async def test_sync_changed_creates_new_document_and_keeps_old_evidence(api, ms):
    pid, old = await imported_pack(api, ms)
    rid = (await run(api, pid))["id"]
    fid = await finding(api, rid)
    async with api[1].begin() as conn:
        unit = (
            await conn.execute(select(Unit.id).where(Unit.document_id == UUID(old["id"])).limit(1))
        ).scalar_one()
        await conn.execute(
            insert(Evidence).values(
                finding_id=fid, unit_id=unit, quote="Climate oversight evidence.", role="supports"
            )
        )
        await conn.execute(update(Run).where(Run.id == UUID(rid)).values(status="final"))

    ms.add("01CLIMATEREPORTPDF", "climate-report-2026 v2.pdf", pdf(2), version=2)
    listed = (await api[0].get(f"/packs/{pid}/sync")).json()
    assert listed["unchanged"] == 0 and listed["removed"] == []
    assert listed["changed"] == [
        {
            "document_id": old["id"],
            "filename": "climate-report-2026.pdf",
            "path": "/Drafts/climate-report-2026.pdf",
        }
    ]

    response = await api[0].post(f"/packs/{pid}/sync", json={"document_ids": [old["id"]]})
    assert response.status_code == 200, response.text
    (result,) = response.json()
    assert result["status"] == "updated"
    assert result["replaced"] == old["id"]
    new = result["document"]
    assert new["id"] != old["id"]
    assert new["filename"] == "climate-report-2026 v2.pdf"
    assert new["kind"] == "annual"
    assert new["source"]["etag"] == '"{01CLIMATEREPORTPDF},2"'
    assert new["index_status"] == "pending"

    detail = (await api[0].get(f"/packs/{pid}")).json()
    assert [d["id"] for d in detail["documents"]] == [new["id"]]
    assert detail["status"] == "uploaded"
    async with api[1].connect() as conn:
        deleted_at = (
            await conn.execute(select(Document.deleted_at).where(Document.id == UUID(old["id"])))
        ).scalar_one()
        assert deleted_at is not None
        # The past run's evidence still points at the old, immutable unit and its text.
        text = (
            await conn.execute(
                select(Unit.text)
                .join(Evidence, Evidence.unit_id == Unit.id)
                .where(Evidence.finding_id == fid)
            )
        ).scalar_one()
        assert "Climate oversight evidence." in text
    assert (await api[0].get(f"/packs/{pid}/sync")).json()["unchanged"] == 1


async def test_sync_metadata_only_change_keeps_document(api, ms):
    pid, old = await imported_pack(api, ms)
    ms.items["01CLIMATEREPORTPDF"]["eTag"] = '"{01CLIMATEREPORTPDF},3"'
    assert len((await api[0].get(f"/packs/{pid}/sync")).json()["changed"]) == 1
    (result,) = (await api[0].post(f"/packs/{pid}/sync", json={"document_ids": [old["id"]]})).json()
    assert result["status"] == "unchanged"
    assert result["document"]["id"] == old["id"]
    assert result["document"]["source"]["etag"] == '"{01CLIMATEREPORTPDF},3"'
    assert (await api[0].get(f"/packs/{pid}/sync")).json()["unchanged"] == 1
    assert (await api[0].get(f"/packs/{pid}")).json()["status"] == "parsed"


async def test_sync_removed_is_marked_not_deleted(api, ms):
    pid, old = await imported_pack(api, ms)
    ms.status["01CLIMATEREPORTPDF"] = 404
    listed = (await api[0].get(f"/packs/{pid}/sync")).json()
    assert listed["removed"] == [
        {
            "document_id": old["id"],
            "filename": "climate-report-2026.pdf",
            "path": "/Drafts/climate-report-2026.pdf",
        }
    ]
    assert listed["changed"] == [] and listed["unchanged"] == 0
    (result,) = (await api[0].post(f"/packs/{pid}/sync", json={"document_ids": [old["id"]]})).json()
    assert result["status"] == "removed"
    assert [d["id"] for d in (await api[0].get(f"/packs/{pid}")).json()["documents"]] == [old["id"]]


async def test_sync_denied_is_reported(api, ms):
    pid, old = await imported_pack(api, ms)
    ms.status["01CLIMATEREPORTPDF"] = 403
    listed = (await api[0].get(f"/packs/{pid}/sync")).json()
    assert listed["failed"] == [
        {
            "document_id": old["id"],
            "filename": "climate-report-2026.pdf",
            "path": "/Drafts/climate-report-2026.pdf",
            "error": "access_denied",
            "message": "Access denied in SharePoint",
        }
    ]


async def test_sync_blocked_while_run_active_or_parsing(api, ms):
    pid, old = await imported_pack(api, ms)
    await run(api, pid)
    response = await api[0].post(f"/packs/{pid}/sync", json={"document_ids": [old["id"]]})
    assert response.status_code == 409
    assert response.json()["error"] == "run_active"


async def test_sync_unknown_document(api, ms):
    pid, _ = await imported_pack(api, ms)
    other = "00000000-0000-0000-0000-000000000000"
    (result,) = (await api[0].post(f"/packs/{pid}/sync", json={"document_ids": [other]})).json()
    assert result == {
        "document_id": other,
        "status": "failed",
        "error": "not_found",
        "message": "Not an imported file in this pack",
    }


# --- 6. alex -----------------------------------------------------------------------------------


async def test_analyst_is_forbidden_but_sees_source(api, ms):
    pid, old = await imported_pack(api, ms)
    client = api[0]
    client.headers["X-Demo-User"] = "alex"
    assert (await client.get("/integrations/microsoft")).json() == {
        "configured": True,
        "connected": False,
        "account": None,
    }
    for response in (
        await client.post("/integrations/microsoft/device-code"),
        await client.post("/integrations/microsoft/poll"),
        await client.delete("/integrations/microsoft"),
        await client.get("/integrations/microsoft/browse"),
        await import_items(api, pid, "01CLIMATEREPORTPDF"),
        await client.get(f"/packs/{pid}/sync"),
        await client.post(f"/packs/{pid}/sync", json={"document_ids": [old["id"]]}),
    ):
        assert response.status_code == 403, response.request.url
    documents = (await client.get(f"/packs/{pid}")).json()["documents"]
    assert documents[0]["source"]["path"] == "/Drafts/climate-report-2026.pdf"
    assert documents[0]["source"]["provider"] == "microsoft"
