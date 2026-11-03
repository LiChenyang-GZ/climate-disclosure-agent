"""Word and Excel extraction into the same units as PDFs (X1).

Office files have no fixed pages, so the unit "page" is a locator: the top-level
section number for .docx and the sheet number for .xlsx (the sheet name goes in
printed_page). Both feed `build_units` the inputs the PDF parser builds, so unit
shapes, sentence packing and table rows match PDFs exactly.
"""

import hashlib
import re
import zipfile
from datetime import date, datetime, time
from importlib.metadata import version
from pathlib import Path
from typing import Any

from cda.db.enums import PageHalf
from cda.parsing.layout import Block
from cda.parsing.pdf import ParsedDocument, ParseError
from cda.parsing.tables import TableData, markdown
from cda.parsing.units import build_units, normalize_text

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # encrypted Office files are OLE containers
BODY_SIZE = 10.0
MAX_CELLS = 20_000
ROWS_PER_TABLE = 50

Page = tuple[int, PageHalf | None, str | None, list[Block]]


def _open_check(content: bytes) -> None:
    if content.startswith(OLE_MAGIC):
        raise ParseError("encrypted")
    if not content.startswith(b"PK"):
        raise ParseError("invalid_file")


def _heading_size(level: int) -> float:
    # Title = level 0; larger sizes rank higher in build_units.
    return 22.0 - 2.0 * min(level, 5)


def _heading_level(style_name: str | None) -> int | None:
    name = (style_name or "").strip()
    if name == "Title":
        return 0
    match = re.fullmatch(r"Heading (\d)", name)
    return int(match[1]) if match else None


def _table(header: list[str], rows: list[list[str]]) -> Block:
    header = [cell or f"Column {i + 1}" for i, cell in enumerate(header)]
    data = TableData(header, [row for row in rows if any(row)], (0.0, 0.0, 0.0, 0.0))
    return Block(0.0, 0.0, 0.0, 0.0, markdown(data), 0, False, data)


def _text(text: str, size: float = BODY_SIZE, bold: bool = False) -> Block:
    return Block(0.0, 0.0, 0.0, 0.0, text, size, bold)


def _finish(content: bytes, version_tag: str, pages: list[Page]) -> ParsedDocument:
    units = build_units(pages)
    if not units:
        raise ParseError("empty_document")
    return ParsedDocument(
        hashlib.sha256(content).hexdigest(),
        max((p[0] for p in pages), default=1),
        version_tag,
        units,
    )


def parse_docx(path: str | Path) -> ParsedDocument:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    content = Path(path).read_bytes()
    _open_check(content)
    try:
        document = docx.Document(str(path))
        items = list(document.iter_inner_content())
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise ParseError("invalid_file") from exc

    levels = [
        level
        for item in items
        if isinstance(item, Paragraph)
        and normalize_text(item.text)
        and (level := _heading_level(item.style.name if item.style else None)) is not None
    ]
    # A Title sits above the sections; the first real heading level splits "pages".
    top = min((level for level in levels if level > 0), default=None)
    page = 1
    seen = False
    blocks_by_page: dict[int, list[Block]] = {1: []}
    for item in items:
        if isinstance(item, Paragraph):
            text = item.text
            if not normalize_text(text):
                continue
            level = _heading_level(item.style.name if item.style else None)
            if level is not None and level == top and seen:
                page += 1
                blocks_by_page[page] = []
            seen = seen or level != 0
            if level is not None:
                blocks_by_page[page].append(_text(text, _heading_size(level), bold=True))
            else:
                runs = [r for r in item.runs if r.text.strip()]
                bold = bool(runs) and all(r.bold for r in runs)
                blocks_by_page[page].append(_text(text, BODY_SIZE, bold))
        elif isinstance(item, Table):
            rows = []
            for row in item.rows:
                cells, ids = [], set()
                for cell in row.cells:
                    # Merged cells repeat the same underlying element; keep it once.
                    if id(cell._tc) in ids:
                        continue
                    ids.add(id(cell._tc))
                    cells.append(normalize_text(cell.text))
                rows.append(cells)
            rows = [r for r in rows if any(r)]
            if not rows:
                continue
            seen = True
            width = max(len(r) for r in rows)
            if len(rows) < 2 or width < 2:
                for r in rows:
                    blocks_by_page[page].append(_text(" ".join(c for c in r if c)))
                continue
            rows = [r + [""] * (width - len(r)) for r in rows]
            blocks_by_page[page].append(_table(rows[0], rows[1:]))
    pages: list[Page] = [(n, None, None, blocks) for n, blocks in sorted(blocks_by_page.items())]
    return _finish(content, f"cda-office-1/python-docx-{version('python-docx')}", pages)


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else f"{value:.10g}"
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time(0) else value.isoformat()
    if isinstance(value, date | time):
        return value.isoformat()
    return normalize_text(str(value))


def parse_xlsx(path: str | Path, *, max_cells: int = MAX_CELLS) -> ParsedDocument:
    import openpyxl  # type: ignore[import-untyped]

    content = Path(path).read_bytes()
    _open_check(content)
    try:
        workbook = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except (zipfile.BadZipFile, KeyError, ValueError, OSError) as exc:
        raise ParseError("invalid_file") from exc
    try:
        pages: list[Page] = []
        cells = 0
        number = 0
        for sheet in workbook.worksheets:
            if sheet.sheet_state != "visible":
                continue
            number += 1
            rows: list[list[str]] = []
            for raw in sheet.iter_rows(values_only=True):
                row = [_cell(v) for v in raw]
                cells += sum(1 for c in row if c)
                if cells > max_cells:
                    raise ParseError("too_large")
                if any(row):
                    rows.append(row)
            # Bold too: a sheet of only tables has no body text to compare sizes with.
            blocks = [_text(sheet.title, _heading_size(1), bold=True)]
            if rows:
                width = max(max((i + 1 for i, c in enumerate(r) if c), default=0) for r in rows)
                rows = [(r + [""] * width)[:width] for r in rows]
                header, data = rows[0], rows[1:]
                if not data:
                    blocks.append(_text(" ".join(c for c in header if c)))
                for start in range(0, len(data), ROWS_PER_TABLE):
                    blocks.append(_table(header, data[start : start + ROWS_PER_TABLE]))
            pages.append((number, None, sheet.title, blocks))
    finally:
        workbook.close()
    if not any(len(blocks) > 1 for *_, blocks in pages):
        raise ParseError("empty_document")
    return _finish(content, f"cda-office-1/openpyxl-{version('openpyxl')}", pages)
