"""Bounded text-layer PDF extraction."""

import hashlib
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf

from cda.parsing.layout import (
    Block,
    is_margin,
    margin_key,
    page_areas,
    printed_page,
    reading_order,
    recurring_margins,
)
from cda.parsing.units import ParsedUnit, build_units


class ParseError(ValueError):
    def __init__(self, code: str, page: int | None = None):
        self.code = code
        self.page = page
        super().__init__(f"{code}" + (f" (page {page})" if page is not None else ""))


@dataclass(frozen=True)
class ParsedDocument:
    sha256: str
    page_count: int
    parser_version: str
    units: list[ParsedUnit]


def extract_blocks(data: dict[str, Any]) -> list[Block]:
    result = []
    for raw in data["blocks"]:
        if raw["type"] != 0:
            continue
        sizes: Counter[float] = Counter()
        bold = 0
        total = 0
        lines = []
        for line in raw["lines"]:
            spans = line["spans"]
            lines.append("".join(s["text"] for s in spans))
            for span in spans:
                n = len(span["text"])
                sizes[float(span["size"])] += n
                total += n
                if span["flags"] & 16:
                    bold += n
        if sizes:
            result.append(
                Block(
                    raw["bbox"][0],
                    raw["bbox"][1],
                    raw["bbox"][2],
                    raw["bbox"][3],
                    "\n".join(lines),
                    sizes.most_common(1)[0][0],
                    bold > total / 2,
                )
            )
    return result


def parse_pdf(
    path: str | Path,
    *,
    max_pages: int = 600,
    page_timeout_s: float = 10.0,
    clock: Callable[[], float] = time.monotonic,
) -> ParsedDocument:
    content = Path(path).read_bytes()
    try:
        doc = pymupdf.open(stream=content, filetype="pdf")  # type: ignore[no-untyped-call]
    except (RuntimeError, ValueError) as exc:
        raise ParseError("invalid_pdf") from exc
    with doc:
        if doc.needs_pass:
            raise ParseError("encrypted")
        if doc.page_count > max_pages:
            raise ParseError("too_many_pages")
        areas = []
        heights = []
        for index in range(doc.page_count):
            start = clock()
            page = doc.load_page(index)  # type: ignore[no-untyped-call]
            blocks = extract_blocks(page.get_text("dict"))
            areas.append(page_areas(blocks, page.rect.width, page.rect.height))
            heights.append(page.rect.height)
            if clock() - start > page_timeout_s:
                raise ParseError("page_timeout", index + 1)
        if not any(
            b.text.strip() for groups in areas for _, _, _, blocks in groups for b in blocks
        ):
            raise ParseError("no_text_layer")
        dropped = recurring_margins(
            [[(half, b) for half, _, _, blocks in groups for b in blocks] for groups in areas],
            heights,
        )
        pages = []
        for index, (groups, height) in enumerate(zip(areas, heights, strict=True), 1):
            for half, x0, x1, blocks in groups:
                printed = printed_page(blocks, height, half, dropped)
                kept = [
                    b
                    for b in blocks
                    if not (is_margin(b, height) and margin_key(b, half) in dropped)
                ]
                pages.append((index, half, printed, reading_order(kept, x0, x1)))
        return ParsedDocument(
            hashlib.sha256(content).hexdigest(),
            doc.page_count,
            f"cda-parse-1/pymupdf-{pymupdf.VersionBind}",
            build_units(pages),
        )
