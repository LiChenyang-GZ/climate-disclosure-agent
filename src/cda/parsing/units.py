"""Text normalization, sentence packing, and hierarchical units."""

import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from cda.db.enums import PageHalf
from cda.parsing.layout import Block


@dataclass(frozen=True)
class ParsedUnit:
    key: str
    parent_key: str | None
    kind: Literal["section", "paragraph", "table", "table_row"]
    pdf_page: int
    page_half: PageHalf | None
    printed_page: str | None
    section_path: str
    text: str
    token_count: int


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("\u00ad", "")
    # Some fonts encode word spaces as control characters (QBE uses \x03, IAG \x07 and \x08),
    # which str.split() does not treat as whitespace.
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    text = re.sub(r"-\s*\r?\n\s*", "-", text)
    return " ".join(text.split())


def count_tokens(text: str) -> int:
    return math.ceil(len(text.split()) * 4 / 3)


def split_paragraph(text: str) -> list[str]:
    if not text:
        return []
    abbreviations = (
        "e.g.",
        "i.e.",
        "etc.",
        "No.",
        "Mr.",
        "Ms.",
        "Dr.",
        "St.",
        "vs.",
        "Inc.",
        "Ltd.",
        "Co.",
        "Pty.",
    )
    sentences = []
    start = 0
    for match in re.finditer(r"""[.!?]\s+(?=[A-Z0-9"'“‘(\[])""", text):
        end = match.start() + 1
        if text[:end].endswith(abbreviations):
            continue
        sentences.append(text[start:end])
        start = match.end()
    sentences.append(text[start:])
    pieces: list[str] = []
    for sentence in sentences:
        words = sentence.split()
        pieces.extend(" ".join(words[i : i + 262]) for i in range(0, len(words), 262))
    result = []
    current = ""
    for piece in pieces:
        candidate = (current + " " + piece).strip()
        if count_tokens(candidate) > 350:
            result.append(current)
            current = piece
        else:
            current = candidate
    if current:
        result.append(current)
    return result


def build_units(
    pages: list[tuple[int, PageHalf | None, str | None, list[Block]]],
) -> list[ParsedUnit]:
    from collections import Counter

    sizes: Counter[float] = Counter()
    for _, _, _, blocks in pages:
        for block in blocks:
            if block.table is None:
                sizes[block.font_size] += len(normalize_text(block.text))
    body = sizes.most_common(1)[0][0] if sizes else 0

    def heading(b: Block) -> bool:
        t = normalize_text(b.text)
        return (
            b.table is None
            and bool(t)
            and len(t.split()) <= 20
            and not t.endswith(".")
            and (b.font_size >= body * 1.15 or (b.bold and len(t.split()) <= 12))
        )

    heading_sizes = sorted(
        {
            b.font_size
            for _, _, _, blocks in pages
            for b in blocks
            if heading(b) and b.font_size > body
        },
        reverse=True,
    )
    stack: list[tuple[int, str, str]] = []
    result = []
    for page, half, printed, blocks in pages:
        counter = 0
        for block in blocks:
            if block.table is not None:
                counter += 1
                key = (
                    f"p{page}{'L' if half == 'left' else 'R' if half == 'right' else ''}-{counter}"
                )
                parent = stack[-1][1] if stack else None
                path = " > ".join(s[2] for s in stack)
                result.append(
                    ParsedUnit(
                        key,
                        parent,
                        "table",
                        page,
                        half,
                        printed,
                        path,
                        block.text,
                        count_tokens(block.text),
                    )
                )
                for row in block.table.rows:
                    counter += 1
                    row_key = f"p{page}{'L' if half == 'left' else 'R' if half == 'right' else ''}-{counter}"
                    row_text = "; ".join(
                        f"{header}: {cell}"
                        for header, cell in zip(block.table.header, row, strict=True)
                        if cell
                    )
                    result.append(
                        ParsedUnit(
                            row_key,
                            key,
                            "table_row",
                            page,
                            half,
                            printed,
                            path,
                            row_text,
                            count_tokens(row_text),
                        )
                    )
                continue
            text = normalize_text(block.text)
            if not text:
                continue
            section = heading(block)
            level = (
                min(heading_sizes.index(block.font_size) + 1, 3)
                if section and block.font_size in heading_sizes
                else min(len(heading_sizes) + 1, 3)
            )
            if section:
                while stack and stack[-1][0] >= level:
                    stack.pop()
            for piece in [text] if section else split_paragraph(text):
                counter += 1
                key = (
                    f"p{page}{'L' if half == 'left' else 'R' if half == 'right' else ''}-{counter}"
                )
                parent = stack[-1][1] if stack else None
                path = " > ".join([s[2] for s in stack] + ([text] if section else []))
                result.append(
                    ParsedUnit(
                        key,
                        parent,
                        "section" if section else "paragraph",
                        page,
                        half,
                        printed,
                        path,
                        piece,
                        count_tokens(piece),
                    )
                )
                if section:
                    stack.append((level, key, text))
    return result
