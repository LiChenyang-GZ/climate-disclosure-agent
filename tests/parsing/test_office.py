"""Word and Excel parsing into PDF-shaped units (X1)."""

from datetime import datetime

import docx
import openpyxl
import pytest

from cda.parsing.office import OLE_MAGIC, parse_docx, parse_xlsx
from cda.parsing.pdf import ParseError
from cda.parsing.store import parse_document


def word(path, build):
    document = docx.Document()
    build(document)
    document.save(path)
    return path


def sample_docx(tmp_path):
    def build(d):
        d.add_heading("Climate report", 0)
        d.add_heading("Governance", 1)
        d.add_paragraph("The board oversees climate-related risks. It meets quarterly.")
        d.add_heading("Committees", 2)
        d.add_paragraph("The risk committee reviews the climate risk register.")
        d.add_heading("Metrics", 1)
        table = d.add_table(rows=3, cols=3)
        for r, values in enumerate(
            [["Scope", "FY2025", "FY2026"], ["Scope 1", "1,200", "1,100"], ["Scope 2", "800", ""]]
        ):
            for c, value in enumerate(values):
                table.cell(r, c).text = value
        d.add_paragraph("Emissions fell in FY2026.")

    return word(tmp_path / "report.docx", build)


def test_docx_sections_paragraphs_and_tables(tmp_path):
    parsed = parse_docx(sample_docx(tmp_path))
    assert parsed.parser_version.startswith("cda-office-1/python-docx-")
    kinds = [(u.kind, u.pdf_page, u.text) for u in parsed.units]
    assert ("section", 1, "Climate report") in kinds
    governance = next(u for u in parsed.units if u.text == "Governance")
    assert governance.kind == "section" and governance.pdf_page == 1
    committee = next(u for u in parsed.units if u.text.startswith("The risk committee"))
    assert committee.kind == "paragraph"
    assert committee.section_path == "Climate report > Governance > Committees"
    # Each top-level heading after content starts a new locator "page".
    metrics = next(u for u in parsed.units if u.text == "Metrics")
    assert metrics.pdf_page == 2
    assert parsed.page_count == 2
    rows = [u.text for u in parsed.units if u.kind == "table_row"]
    assert rows == ["Scope: Scope 1; FY2025: 1,200; FY2026: 1,100", "Scope: Scope 2; FY2025: 800"]
    table = next(u for u in parsed.units if u.kind == "table")
    assert all(u.parent_key == table.key for u in parsed.units if u.kind == "table_row")
    assert all(u.printed_page is None for u in parsed.units)


def test_docx_merged_cells_counted_once(tmp_path):
    def build(d):
        d.add_paragraph("Targets")
        table = d.add_table(rows=2, cols=3)
        for c, value in enumerate(["Target", "Year", "Status"]):
            table.cell(0, c).text = value
        merged = table.cell(1, 0).merge(table.cell(1, 1))
        merged.text = "Net zero by 2050"
        table.cell(1, 2).text = "On track"

    parsed = parse_docx(word(tmp_path / "t.docx", build))
    table = next(u for u in parsed.units if u.kind == "table")
    assert "Net zero by 2050" in table.text
    row = next(u for u in parsed.units if u.kind == "table_row")
    assert row.text.count("Net zero by 2050") == 1


def test_docx_small_table_becomes_text(tmp_path):
    def build(d):
        table = d.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "Only"
        table.cell(0, 1).text = "row"

    parsed = parse_docx(word(tmp_path / "s.docx", build))
    assert [(u.kind, u.text) for u in parsed.units] == [("paragraph", "Only row")]


def sample_xlsx(tmp_path, rows=3):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Emissions"
    ws.append([None, None])
    ws.append(["Scope", "tCO2e", "Measured"])
    for i in range(rows):
        ws.append([f"Scope {i + 1}", 1200.0 + i, datetime(2026, 6, 30)])
    hidden = wb.create_sheet("Hidden")
    hidden.append(["secret", "x"])
    hidden.sheet_state = "hidden"
    targets = wb.create_sheet("Targets")
    targets.append(["Target", "Value"])
    targets.append(["Reduction", 0.42])
    path = tmp_path / "data.xlsx"
    wb.save(path)
    return path


def test_xlsx_sheets_tables_and_values(tmp_path):
    parsed = parse_xlsx(sample_xlsx(tmp_path))
    assert parsed.parser_version.startswith("cda-office-1/openpyxl-")
    assert parsed.page_count == 2
    sections = [(u.text, u.pdf_page, u.printed_page) for u in parsed.units if u.kind == "section"]
    assert sections == [("Emissions", 1, "Emissions"), ("Targets", 2, "Targets")]
    rows = [u.text for u in parsed.units if u.kind == "table_row"]
    assert rows[0] == "Scope: Scope 1; tCO2e: 1200; Measured: 2026-06-30"
    assert "Target: Reduction; Value: 0.42" in rows
    assert not any("secret" in u.text for u in parsed.units)
    assert all(
        u.section_path in ("Emissions", "Targets") for u in parsed.units if u.kind != "section"
    )


def test_xlsx_long_sheet_split_into_tables(tmp_path):
    parsed = parse_xlsx(sample_xlsx(tmp_path, rows=120))
    tables = [u for u in parsed.units if u.kind == "table" and u.pdf_page == 1]
    assert len(tables) == 3
    assert all(t.text.startswith("| Scope | tCO2e | Measured |") for t in tables)
    assert sum(u.kind == "table_row" and u.pdf_page == 1 for u in parsed.units) == 120


def test_xlsx_cell_limit(tmp_path):
    with pytest.raises(ParseError) as exc:
        parse_xlsx(sample_xlsx(tmp_path, rows=40), max_cells=50)
    assert exc.value.code == "too_large"


@pytest.mark.parametrize("name", ["bad.docx", "bad.xlsx"])
def test_invalid_and_encrypted(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"not an office file")
    with pytest.raises(ParseError) as exc:
        parse_document(path)
    assert exc.value.code == "invalid_file"
    path.write_bytes(OLE_MAGIC + b"\0" * 64)
    with pytest.raises(ParseError) as exc:
        parse_document(path)
    assert exc.value.code == "encrypted"


def test_empty_documents(tmp_path):
    with pytest.raises(ParseError) as exc:
        parse_docx(word(tmp_path / "e.docx", lambda d: d.add_paragraph("   ")))
    assert exc.value.code == "empty_document"
    wb = openpyxl.Workbook()
    wb.save(tmp_path / "e.xlsx")
    with pytest.raises(ParseError) as exc:
        parse_xlsx(tmp_path / "e.xlsx")
    assert exc.value.code == "empty_document"


def test_dispatch_by_suffix(tmp_path):
    assert parse_document(sample_docx(tmp_path)).parser_version.startswith("cda-office-1/python")
    assert parse_document(sample_xlsx(tmp_path)).parser_version.startswith("cda-office-1/openpyxl")
