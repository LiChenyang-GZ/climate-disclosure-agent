"""Word and Excel uploads through the REST API (X1)."""

import io
from uuid import UUID

import docx
import openpyxl
import pytest
from sqlalchemy import select
from test_api import pack

from cda.db.models import Pack
from cda.parsing.office import OLE_MAGIC
from cda.tools.context import ToolContext
from cda.tools.registry import run_tool

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def docx_bytes():
    document = docx.Document()
    document.add_heading("Governance", 1)
    document.add_paragraph("The board reviews the decarbonisation roadmap each quarter.")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def xlsx_bytes():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Emissions"
    ws.append(["Scope", "tCO2e"])
    ws.append(["Scope 1 hydrogen pilot", 1200])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


async def send(api, pid, name, data, content_type):
    return await api[0].post(
        f"/packs/{pid}/documents",
        files={"file": (name, data, content_type)},
        data={"kind": "other"},
    )


@pytest.mark.parametrize(
    ("name", "data", "content_type", "query", "pages"),
    [
        ("plan.docx", docx_bytes, DOCX, "decarbonisation", 1),
        ("data.xlsx", xlsx_bytes, XLSX, "hydrogen", 1),
    ],
)
async def test_office_upload_parse_search_download(api, name, data, content_type, query, pages):
    pid = await pack(api)
    body = data()
    response = await send(api, pid, name, body, content_type)
    assert response.status_code == 201, response.text
    document = response.json()
    assert document["filename"] == name
    assert document["page_count"] == pages
    assert document["unit_count"] >= 1
    assert document["parser_version"].startswith("cda-office-1/")
    assert (await api[0].post(f"/packs/{pid}/parse")).status_code == 202
    detail = (await api[0].get(f"/packs/{pid}")).json()
    assert detail["status"] == "parsed"

    async with api[1].connect() as conn:
        user = (await conn.execute(select(Pack.created_by).where(Pack.id == UUID(pid)))).scalar()
        hits = (
            await run_tool(ToolContext(conn, user, UUID(pid)), "keyword_search", {"query": query})
        ).data["hits"]
    assert [h["filename"] for h in hits] == [name]

    file = await api[0].get(f"/packs/{pid}/documents/{document['id']}/file")
    assert file.status_code == 200
    assert file.content == body
    assert file.headers["content-type"] == content_type
    assert file.headers["content-disposition"].startswith("attachment")


async def test_unsupported_and_broken_office_files(api):
    pid = await pack(api)
    response = await send(api, pid, "notes.txt", b"plain text", "text/plain")
    assert response.status_code == 415
    assert response.json()["error"] == "unsupported_format"
    response = await send(api, pid, "locked.docx", OLE_MAGIC + b"\0" * 64, DOCX)
    assert response.status_code == 422
    assert response.json()["error"] == "encrypted"
    response = await send(api, pid, "broken.xlsx", b"PK\x03\x04broken", XLSX)
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_file"
    assert (await api[0].get(f"/packs/{pid}")).json()["documents"] == []
