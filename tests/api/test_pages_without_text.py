"""D92 / T33: pages with no readable text are shown on the pack and in exports."""

import io

import docx
import pymupdf
from test_api import pack, run, upload
from test_office_upload import DOCX, send


def pdf_with_gaps():
    """Four pages: text, blank, text, image only."""
    doc = pymupdf.open()
    for index in range(4):
        page = doc.new_page()
        if index in (0, 2):
            page.insert_text((72, 72), f"Climate oversight evidence on page {index + 1}.")
        if index == 3:
            pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
            pixmap.clear_with(200)
            page.insert_image(pymupdf.Rect(72, 72, 272, 272), pixmap=pixmap)
    data = doc.tobytes()
    doc.close()
    return data


def full_pdf():
    doc = pymupdf.open()
    texts = ["Board oversight of climate risk.", "Scenario analysis results.", "Scope 1 emissions."]
    for index, text in enumerate(texts):
        # Different text and position per page: the same line on every page is a margin.
        doc.new_page().insert_text((72, 300 + 40 * index), text)
    data = doc.tobytes()
    doc.close()
    return data


def docx_with_empty_section():
    document = docx.Document()
    document.add_heading("Governance", 1)
    document.add_paragraph("The board reviews climate risk each quarter.")
    document.add_heading("Strategy", 1)
    document.add_heading("Metrics", 1)
    document.add_paragraph("Scope 1 emissions were 1,200 tCO2e.")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


async def documents(api, pid):
    response = await api[0].get(f"/packs/{pid}")
    assert response.status_code == 200, response.text
    return {d["filename"]: d for d in response.json()["documents"]}


async def test_pdf_pages_without_text(api):
    pid = await pack(api)
    response = await upload(api, pid, pdf_with_gaps())
    assert response.status_code == 201, response.text
    assert response.json()["pages_without_text"] == [2, 4]
    assert (await documents(api, pid))["report.pdf"]["pages_without_text"] == [2, 4]


async def test_full_text_pdf_has_none(api):
    pid = await pack(api)
    assert (await upload(api, pid, full_pdf())).status_code == 201
    assert (await documents(api, pid))["report.pdf"]["pages_without_text"] == []


async def test_word_section_without_text(api):
    pid = await pack(api)
    response = await send(api, pid, "plan.docx", docx_with_empty_section(), DOCX)
    assert response.status_code == 201, response.text
    info = (await documents(api, pid))["plan.docx"]
    assert info["page_count"] == 3
    assert info["pages_without_text"] == [2]


async def test_exports_list_pages_and_skip_deleted_documents(api):
    pid = await pack(api)
    kept = await upload(api, pid, pdf_with_gaps())
    gone = await send(api, pid, "plan.docx", docx_with_empty_section(), DOCX)
    assert kept.status_code == gone.status_code == 201
    deleted = await api[0].delete(f"/packs/{pid}/documents/{gone.json()['id']}")
    assert deleted.status_code == 200, deleted.text
    assert (await api[0].post(f"/packs/{pid}/parse")).status_code == 202
    rid = (await run(api, pid))["id"]
    body = (await api[0].get(f"/runs/{rid}/export", params={"format": "json"})).json()
    assert body["documents"] == [{"document": "report.pdf", "pages_without_text": [2, 4]}]
    text = (await api[0].get(f"/runs/{rid}/export", params={"format": "md"})).text
    assert "Pages with no readable text in report\\.pdf: 2, 4\n" in text
    assert "plan.docx" not in text
