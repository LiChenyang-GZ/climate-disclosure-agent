"""Default PyMuPDF table detection and Markdown rendering."""

from dataclasses import dataclass

import pymupdf

from cda.parsing.layout import Block
from cda.parsing.units import normalize_text


@dataclass(frozen=True)
class TableData:
    header: list[str]
    rows: list[list[str]]
    bbox: tuple[float, float, float, float]


def markdown(table: TableData) -> str:
    def line(cells: list[str]) -> str:
        return "| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |"

    return "\n".join(
        [line(table.header), line(["---"] * len(table.header))] + [line(row) for row in table.rows]
    )


def detect_tables(page: pymupdf.Page) -> list[Block]:
    result = []
    for table in page.find_tables().tables:  # type: ignore[no-untyped-call]
        if table.row_count < 2 or table.col_count < 2:
            continue
        cells = [[normalize_text(cell or "") for cell in row] for row in table.extract()]
        names = table.header.names if table.header else None
        if names:
            header = [normalize_text(cell or "") for cell in names]
            if not table.header.external:
                cells = cells[1:]
        else:
            header = [f"Column {i + 1}" for i in range(table.col_count)]
        data = TableData(header, [row for row in cells if any(row)], tuple(table.bbox))
        result.append(Block(*data.bbox, markdown(data), 0, False, data))
    return result


def covered(block: Block, table: Block) -> bool:
    area = (block.x1 - block.x0) * (block.y1 - block.y0)
    intersection = max(0, min(block.x1, table.x1) - max(block.x0, table.x0)) * max(
        0, min(block.y1, table.y1) - max(block.y0, table.y0)
    )
    return area > 0 and intersection >= area * 0.5
