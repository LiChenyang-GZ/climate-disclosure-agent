"""Pure geometry and recurring-margin heuristics."""

import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cda.db.enums import PageHalf

if TYPE_CHECKING:
    from cda.parsing.tables import TableData


@dataclass(frozen=True)
class Block:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    font_size: float
    bold: bool
    table: "TableData | None" = None


MarginKey = tuple[PageHalf | None, str, int]


def page_areas(
    blocks: list[Block], width: float, height: float
) -> list[tuple[PageHalf | None, float, float, list[Block]]]:
    mid = width / 2
    if width / height >= 1.3 and not any(b.x0 < mid - 2 and b.x1 > mid + 2 for b in blocks):
        return [
            ("left", 0, mid, [b for b in blocks if (b.x0 + b.x1) / 2 < mid]),
            ("right", mid, width, [b for b in blocks if (b.x0 + b.x1) / 2 >= mid]),
        ]
    return [(None, 0, width, blocks)]


def reading_order(blocks: list[Block], x0: float, x1: float) -> list[Block]:
    width = x1 - x0
    narrow = sorted([b for b in blocks if b.x1 - b.x0 <= width * 0.6], key=lambda b: b.x0)
    edge = narrow[0].x1 if narrow else x0
    split = None
    for b in narrow:
        lo = max(edge, x0 + width * 0.3)
        hi = min(b.x0, x0 + width * 0.7)
        if hi - lo >= 18:
            split = (lo + hi) / 2
            break
        edge = max(edge, b.x1)
    if split is None:
        return sorted(blocks, key=lambda b: (b.y0, b.x0))
    full = sorted([b for b in blocks if b.x1 - b.x0 > width * 0.6], key=lambda b: (b.y0, b.x0))
    remaining = [b for b in blocks if b.x1 - b.x0 <= width * 0.6]
    result = []

    def band(items: list[Block]) -> list[Block]:
        return sorted(items, key=lambda b: ((b.x0 + b.x1) / 2 >= split, b.y0, b.x0))

    for separator in full:
        result.extend(band([b for b in remaining if b.y0 < separator.y0]))
        remaining = [b for b in remaining if b.y0 >= separator.y0]
        result.append(separator)
    return result + band(remaining)


def is_margin(block: Block, height: float) -> bool:
    return block.y0 <= height * 0.08 or block.y1 >= height * 0.92


def margin_key(block: Block, half: PageHalf | None) -> MarginKey:
    return (
        half,
        re.sub(r"\s+", " ", re.sub(r"\d+", "#", block.text.lower())).strip(),
        round(block.y0 / 10) * 10,
    )


def recurring_margins(
    pages: list[list[tuple[PageHalf | None, Block]]], heights: list[float]
) -> set[MarginKey]:
    if len(pages) < 3:
        return set()
    counts: Counter[MarginKey] = Counter()
    for blocks, height in zip(pages, heights, strict=True):
        counts.update({margin_key(b, h) for h, b in blocks if is_margin(b, height)})
    return {key for key, count in counts.items() if count > len(pages) / 2}


def printed_page(
    blocks: list[Block], height: float, half: PageHalf | None, dropped: set[MarginKey]
) -> str | None:
    # Enumerate canonical lower-case numerals through forty.
    romans = {
        t + u
        for t in ("", "x", "xx", "xxx", "xl")
        for u in ("", "i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix")
        if t != "xl" or not u
    } - {""}
    for b in sorted(blocks, key=lambda b: (b.y0, b.x0)):
        if not is_margin(b, height):
            continue
        value = b.text.strip()
        match = re.fullmatch(r"(?:Page\s+)?(\d+)", value)
        if match:
            return match.group(1)
        if value in romans:
            return value
        key = margin_key(b, half)
        if key in dropped:
            numbers: list[str] = re.findall(r"\d+", value)
            for number in reversed(numbers):
                if not (len(number) == 4 and 1900 <= int(number) <= 2099):
                    return number
    return None
