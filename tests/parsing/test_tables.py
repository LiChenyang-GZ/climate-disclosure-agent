from types import SimpleNamespace

import pymupdf
import pytest
from sqlalchemy import select

from cda.db.models import Unit
from cda.parsing.pdf import parse_pdf
from cda.parsing.store import ingest_pdf
from cda.parsing.units import count_tokens

DATA = [
    ["Metric", "FY2026", "FY2025", "Unit"],
    ["Scope 1", "1204", "1310", "tCO2e"],
    ["Scope 2", "200", "210", "tCO2e"],
    ["Scope 3", "300", "310", "tCO2e"],
]
MARKDOWN = "| Metric | FY2026 | FY2025 | Unit |\n| --- | --- | --- | --- |\n| Scope 1 | 1204 | 1310 | tCO2e |\n| Scope 2 | 200 | 210 | tCO2e |\n| Scope 3 | 300 | 310 | tCO2e |"
ROW_TEXTS = [
    f"Metric: Scope {i}; FY2026: {a}; FY2025: {b}; Unit: tCO2e"
    for i, a, b in [(1, 1204, 1310), (2, 200, 210), (3, 300, 310)]
]


def draw_table(page, data, x=40, y=200):
    width, height = 100, 30
    for r in range(len(data) + 1):
        page.draw_line((x, y + r * height), (x + len(data[0]) * width, y + r * height))
    for c in range(len(data[0]) + 1):
        page.draw_line((x + c * width, y), (x + c * width, y + len(data) * height))
    for r, cells in enumerate(data):
        for c, cell in enumerate(cells):
            if cell:
                page.insert_text((x + c * width + 5, y + r * height + 20), cell, fontsize=10)


def make_pdf(tmp_path, data=DATA, pages=1, spread=False, headings=False, y=200):
    path = tmp_path / "tables.pdf"
    with pymupdf.open() as doc:
        for _ in range(pages):
            page = doc.new_page(width=1200 if spread else 600, height=750)
            x = 650 if spread else 40
            if headings:
                page.insert_text((x, 90), "Climate", fontsize=20)
                page.insert_text((x, 120), "Metrics", fontsize=14)
            page.insert_text(
                (x, 160), "Paragraph above with enough ordinary body words.", fontsize=10
            )
            draw_table(page, data, x=x, y=y)
            page.insert_text(
                (x, 450), "Paragraph below with enough ordinary body words.", fontsize=10
            )
            page.insert_text((x, 735), "24", fontsize=10)
        doc.save(path)
    return path


def test_exact_table_and_rows(tmp_path):
    result = parse_pdf(make_pdf(tmp_path))
    table = next(u for u in result.units if u.kind == "table")
    assert table.text == MARKDOWN
    assert table.token_count == count_tokens(MARKDOWN)
    assert [u.text for u in result.units if u.kind == "table_row"] == ROW_TEXTS
    assert result.parser_version.startswith("cda-parse-2/")


def test_covered_text_and_order(tmp_path):
    units = parse_pdf(make_pdf(tmp_path)).units
    assert [u.kind for u in units] == [
        "paragraph",
        "table",
        "table_row",
        "table_row",
        "table_row",
        "paragraph",
        "paragraph",
    ]
    assert [u.text for u in units if u.kind == "paragraph"] == [
        "Paragraph above with enough ordinary body words.",
        "Paragraph below with enough ordinary body words.",
        "24",
    ]


def test_section_parents(tmp_path):
    units = parse_pdf(make_pdf(tmp_path, headings=True)).units
    table = next(u for u in units if u.kind == "table")
    section = next(u for u in units if u.text == "Metrics")
    assert table.parent_key == section.key
    assert table.section_path == "Climate > Metrics"
    for row in [u for u in units if u.kind == "table_row"]:
        assert row.parent_key == table.key
        assert row.section_path == table.section_path
        assert (row.pdf_page, row.page_half, row.printed_page) == (1, None, "24")


def test_empty_cells_and_escaping(tmp_path):
    data = [["Metric", "Value"], ["A|B", ""], ["", ""], ["C", "2"]]
    units = parse_pdf(make_pdf(tmp_path, data=data)).units
    assert next(u.text for u in units if u.kind == "table") == (
        "| Metric | Value |\n| --- | --- |\n| A\\|B |  |\n| C | 2 |"
    )
    assert [u.text for u in units if u.kind == "table_row"] == [
        "Metric: A|B",
        "Metric: C; Value: 2",
    ]


def test_no_detected_header(tmp_path, monkeypatch):
    original = pymupdf.Page.find_tables

    def find(page):
        result = original(page)
        for table in result.tables:
            table.header = SimpleNamespace(names=[], external=False)
        return result

    monkeypatch.setattr(pymupdf.Page, "find_tables", find)
    units = parse_pdf(make_pdf(tmp_path)).units
    assert next(u.text for u in units if u.kind == "table").startswith(
        "| Column 1 | Column 2 | Column 3 | Column 4 |"
    )
    assert next(u for u in units if u.kind == "table_row").text.startswith("Column 1: Metric;")


@pytest.mark.parametrize("data", [[["A", "B"]], [["A"], ["B"], ["C"]]])
def test_degenerate_table(tmp_path, data):
    units = parse_pdf(make_pdf(tmp_path, data=data)).units
    assert all(u.kind == "paragraph" for u in units)
    assert all(cell in " ".join(u.text for u in units) for row in data for cell in row)


def test_spread_table(tmp_path):
    units = parse_pdf(make_pdf(tmp_path, spread=True)).units
    assert len([u for u in units if u.kind == "table"]) == 1
    assert all(u.page_half == "right" and u.printed_page == "24" for u in units)


def test_separate_pages(tmp_path):
    units = parse_pdf(make_pdf(tmp_path, pages=2)).units
    tables = [u for u in units if u.kind == "table"]
    assert [u.pdf_page for u in tables] == [1, 2]
    for table in tables:
        children = [u for u in units if u.parent_key == table.key]
        assert [u.text for u in children] == ROW_TEXTS
        assert all(u.pdf_page == table.pdf_page for u in children)


@pytest.mark.db
async def test_ingest_tables(db, rows, tmp_path):
    document = await ingest_pdf(
        db, pack_id=rows["pack"], kind="annual", path=make_pdf(tmp_path, headings=True)
    )
    stored = (await db.execute(select(Unit.__table__).where(Unit.document_id == document))).all()
    table = next(u for u in stored if u.kind == "table")
    section = next(u for u in stored if u.text == "Metrics")
    assert table.parent_id == section.id
    children = [u for u in stored if u.kind == "table_row"]
    assert len(children) == 3
    assert all(u.parent_id == table.id for u in children)


@pytest.fixture
async def db(pg_url):
    import asyncio

    from alembic import command
    from alembic.config import Config
    from sqlalchemy.ext.asyncio import create_async_engine

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", pg_url.replace("%", "%%"))
    await asyncio.to_thread(command.upgrade, config, "head")
    engine = create_async_engine(pg_url)
    async with engine.connect() as conn:
        transaction = await conn.begin()
        yield conn
        await transaction.rollback()
    await engine.dispose()


@pytest.fixture
async def rows(db):
    from sqlalchemy import text

    user = (
        await db.execute(
            text(
                "INSERT INTO users(handle,role,display_name) VALUES ('tables','lead','Tables') RETURNING id"
            )
        )
    ).scalar_one()
    team = (
        await db.execute(text("INSERT INTO teams(name) VALUES ('Tables') RETURNING id"))
    ).scalar_one()
    pack = (
        await db.execute(
            text(
                "INSERT INTO packs(company,reporting_year,team_id,created_by) VALUES ('Tables',2026,:team,:user) RETURNING id"
            ),
            {"team": team, "user": user},
        )
    ).scalar_one()
    return {"pack": pack}


@pytest.mark.parametrize("external", [False, True])
def test_external_header_and_normalization(external):
    from cda.parsing.tables import detect_tables

    extracted = [[None, " ﬁ  value\u00ad "], [None, None]]
    if not external:
        extracted.insert(0, ["Name", "Value"])
    table = SimpleNamespace(
        row_count=len(extracted),
        col_count=2,
        bbox=(0, 0, 100, 100),
        header=SimpleNamespace(names=[" Name ", "Value"], external=external),
        extract=lambda: extracted,
    )
    page = SimpleNamespace(find_tables=lambda: SimpleNamespace(tables=[table]))
    blocks = detect_tables(page)
    assert blocks[0].table.header == ["Name", "Value"]
    assert blocks[0].table.rows == [["", "fi value"]]


def test_covered_threshold():
    from cda.parsing.layout import Block
    from cda.parsing.tables import covered

    block = Block(0, 0, 100, 100, "body", 10, False)
    assert covered(block, Block(50, 0, 150, 100, "", 0, False))
    assert not covered(block, Block(51, 0, 150, 100, "", 0, False))
    assert not covered(Block(0, 0, 0, 0, "", 0, False), block)


def test_long_row_and_margin_tables():
    from cda.parsing.layout import Block
    from cda.parsing.tables import TableData, markdown
    from cda.parsing.units import build_units

    data = TableData(["Name", "Value"], [["word " * 400, "2"]], (0, 0, 100, 40))
    block = Block(*data.bbox, markdown(data), 50, True, data)
    units = build_units([(1, None, "24", [block])])
    assert [u.kind for u in units] == ["table", "table_row"]
    assert units[1].token_count > 350
    assert units[1].parent_key == units[0].key


def test_repeated_margin_tables_kept(tmp_path):
    units = parse_pdf(make_pdf(tmp_path, pages=3, y=30)).units
    assert len([u for u in units if u.kind == "table"]) == 3
    assert all(u.printed_page == "24" for u in units)
