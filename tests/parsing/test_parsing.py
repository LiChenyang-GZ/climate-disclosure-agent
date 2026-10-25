import math

import pymupdf
import pytest
from pydantic import ValidationError

from cda.parsing.layout import Block, page_areas, printed_page, reading_order, recurring_margins
from cda.parsing.parts import load_parts_file, resolve_part
from cda.parsing.pdf import ParseError, parse_pdf
from cda.parsing.units import count_tokens, normalize_text, split_paragraph


def b(text, x=20, y=100, w=100, size=10, bold=False):
    return Block(x, y, x + w, y + 12, text, size, bold)


def test_columns_and_bands():
    blocks = [
        b("title", 0, 20, 500),
        b("R1", 300, 100),
        b("L1", 20, 120),
        b("middle", 0, 200, 500),
        b("R2", 300, 300),
        b("L2", 20, 320),
    ]
    assert [v.text for v in reading_order(blocks, 0, 500)] == [
        "title",
        "L1",
        "R1",
        "middle",
        "L2",
        "R2",
    ]


def test_indentation():
    blocks = [b("a", 20, 100, 350), b("b", 60, 150, 350)]
    assert reading_order(blocks, 0, 500) == blocks


def test_spread_detection():
    assert [a[0] for a in page_areas([b("a"), b("b", 600)], 1000, 600)] == ["left", "right"]
    assert page_areas([b("cross", 450, w=100)], 1000, 600)[0][0] is None
    assert page_areas([b("a")], 500, 800)[0][0] is None


def test_margins():
    pages = [[(None, b("Header", y=10)), (None, b(f"Page {i}", y=780))] for i in range(5)]
    pages[4][0] = (None, b("Header", y=30))
    dropped = recurring_margins(pages, [800] * 5)
    assert (None, "header", 10) in dropped
    assert (None, "header", 30) not in dropped
    assert recurring_margins(pages[:2], [800] * 2) == set()
    assert printed_page([b("Page 12", y=780)], 800, None, dropped) == "12"
    assert printed_page([b("iv", y=780)], 800, None, set()) == "iv"
    assert printed_page([b("body")], 800, None, set()) is None


def test_normalize():
    assert (
        normalize_text(" ﬁ soft\u00adhyphen climate-\nrelated  text ")
        == "fi softhyphen climate-related text"
    )
    assert normalize_text(" \n ") == ""


@pytest.mark.parametrize("n", [0, 1, 3, 9, 263])
def test_tokens(n):
    assert count_tokens(" ".join(["word"] * n)) == math.ceil(n * 4 / 3)


def test_sentence_packing():
    text = " ".join([" ".join(["Word"] * 74) + " End." for _ in range(9)])
    parts = split_paragraph(text)
    assert len(parts) >= 3 and all(count_tokens(p) <= 350 for p in parts)
    assert " ".join(parts) == text
    assert all(p.endswith(".") for p in parts)


def test_abbreviation():
    text = " ".join(["word"] * 250) + ". Example e.g. Scope matters."
    assert "e.g. Scope" in split_paragraph(text)[-1]


def test_long_sentence():
    text = " ".join(["word"] * 375)
    parts = split_paragraph(text)
    assert all(count_tokens(p) <= 350 for p in parts)
    assert " ".join(parts) == text


def make_pdf(path, pages=3, blank=False):
    with pymupdf.open() as doc:
        for i in range(pages):
            p = doc.new_page()
            if not blank:
                p.insert_text((30, 20), "Header", fontsize=10)
                p.insert_text(
                    (30, 100),
                    "Climate" if i == 0 else "Metrics and targets",
                    fontsize=18 if i == 0 else 14,
                )
                p.insert_text((30, 150), "Our emissions are measured annually.", fontsize=10)
                p.insert_text((330, 180), "Right column disclosure.", fontsize=10)
                p.insert_text((30, 820), str(i + 10), fontsize=10)
        doc.save(path)


def test_golden(tmp_path):
    path = tmp_path / "a.pdf"
    make_pdf(path)
    result = parse_pdf(path)
    assert [
        (u.kind, u.section_path, u.pdf_page, u.page_half, u.printed_page, u.text[:30])
        for u in result.units
    ] == [
        ("section", "Climate", 1, None, "10", "Climate"),
        ("paragraph", "Climate", 1, None, "10", "Our emissions are measured ann"),
        ("paragraph", "Climate", 1, None, "10", "Right column disclosure."),
        ("section", "Climate > Metrics and targets", 2, None, "11", "Metrics and targets"),
        (
            "paragraph",
            "Climate > Metrics and targets",
            2,
            None,
            "11",
            "Our emissions are measured ann",
        ),
        ("paragraph", "Climate > Metrics and targets", 2, None, "11", "Right column disclosure."),
        ("section", "Climate > Metrics and targets", 3, None, "12", "Metrics and targets"),
        (
            "paragraph",
            "Climate > Metrics and targets",
            3,
            None,
            "12",
            "Our emissions are measured ann",
        ),
        ("paragraph", "Climate > Metrics and targets", 3, None, "12", "Right column disclosure."),
    ]
    assert result.units[1].parent_key == result.units[0].key


@pytest.mark.parametrize(
    "case,code",
    [
        ("invalid", "invalid_pdf"),
        ("encrypted", "encrypted"),
        ("limit", "too_many_pages"),
        ("blank", "no_text_layer"),
        ("timeout", "page_timeout"),
    ],
)
def test_parse_errors(tmp_path, case, code):
    path = tmp_path / "a.pdf"
    if case == "invalid":
        path.write_text("not pdf")
    elif case == "encrypted":
        with pymupdf.open() as doc:
            doc.new_page()
            doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user")
    else:
        make_pdf(path, pages=601 if case == "limit" else 3, blank=case == "blank")
    times = iter([0, 0, 0, 20, 20, 20])
    with pytest.raises(ParseError) as exc:
        parse_pdf(path, **({"clock": lambda: next(times)} if case == "timeout" else {}))
    assert exc.value.code == code
    if case == "timeout":
        assert exc.value.page == 2


def test_parts_valid(tmp_path):
    (tmp_path / "a.pdf").touch()
    path = tmp_path / "parts.yaml"
    path.write_text(
        "documents:\n- filename: a.pdf\n  kind: annual\n  parts:\n  - kind: sustainability\n    first_pdf_page: 2\n    last_pdf_page: 4\n"
    )
    parts = load_parts_file(path).documents[0].parts
    for page in [2, 3, 4]:
        assert resolve_part(parts, "annual", page).kind == "sustainability"
    assert resolve_part(parts, "annual", 1).part_id is None
    assert resolve_part([], "annual", 3).kind == "annual"


@pytest.mark.parametrize(
    "entry",
    [
        "kind: climate",
        "kind: annual\n  extra: true",
        "kind: annual\n  parts: [{kind: annual, first_pdf_page: 0, last_pdf_page: 2}]",
        "kind: annual\n  parts: [{kind: annual, first_pdf_page: 3, last_pdf_page: 2}]",
        "kind: annual\n  parts: [{kind: annual, first_pdf_page: 1, last_pdf_page: 3}, {kind: other, first_pdf_page: 3, last_pdf_page: 4}]",
    ],
)
def test_parts_invalid(tmp_path, entry):
    (tmp_path / "a.pdf").touch()
    path = tmp_path / "parts.yaml"
    path.write_text("documents:\n- filename: a.pdf\n  " + entry + "\n")
    with pytest.raises(ValidationError):
        load_parts_file(path)


def test_duplicate_filename(tmp_path):
    (tmp_path / "a.pdf").touch()
    path = tmp_path / "parts.yaml"
    path.write_text(
        "documents:\n- {filename: a.pdf, kind: annual}\n- {filename: a.pdf, kind: other}"
    )
    with pytest.raises(ValidationError):
        load_parts_file(path)


def test_missing_file(tmp_path):
    path = tmp_path / "parts.yaml"
    path.write_text("documents: [{filename: missing.pdf, kind: annual}]")
    with pytest.raises(ValidationError):
        load_parts_file(path)


def test_spread_pdf(tmp_path):
    path = tmp_path / "spread.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=1200, height=700)
        for offset in [0, 600]:
            # Separate y positions keep each column in its own text block.
            page.insert_text((offset + 30, 120), "Left column body.", fontsize=10)
            page.insert_text((offset + 330, 100), "Right column body.", fontsize=10)
            page.insert_text((offset + 30, 680), str(24 + offset // 600), fontsize=10)
        doc.save(path)
    units = parse_pdf(path).units
    assert [u.page_half for u in units] == ["left"] * 3 + ["right"] * 3
    assert [u.printed_page for u in units] == ["24"] * 3 + ["25"] * 3
    assert {u.pdf_page for u in units} == {1}
    assert [u.text for u in units][:2] == ["Left column body.", "24"]


def test_heading_tree_and_empty():
    from cda.parsing.units import build_units

    units = build_units(
        [
            (
                1,
                None,
                None,
                [
                    b("Preface paragraph with enough body text to establish the normal size."),
                    b("Climate", y=130, size=20),
                    b("Metrics and targets", y=160, size=14),
                    b("Bold heading", y=190, bold=True),
                    b("Paragraph body text.", y=220),
                    b("   ", y=260),
                ],
            )
        ]
    )
    assert units[0].parent_key is None and units[0].section_path == ""
    assert [u.kind for u in units] == ["paragraph", "section", "section", "section", "paragraph"]
    assert units[-1].parent_key == units[-2].key
    assert units[-2].section_path == "Climate > Metrics and targets > Bold heading"


def test_repeated_body_and_half_isolation():
    pages = [[(None, b("Repeated body.", y=100)), (None, b("Header", y=10))] for _ in range(5)]
    dropped = recurring_margins(pages, [800] * 5)
    assert all(key[1] != "repeated body." for key in dropped)
    assert printed_page([b("Report 17", y=780)], 800, "left", {("left", "report #", 780)}) == "17"
    assert printed_page([b("Report 17", y=780)], 800, "right", {("left", "report #", 780)}) is None


def test_printed_pdf_page_three(tmp_path):
    path = tmp_path / "pages.pdf"
    make_pdf(path)
    units = parse_pdf(path).units
    assert any(u.pdf_page == 3 and u.printed_page == "12" for u in units)


@pytest.mark.parametrize(
    "footer,expected",
    [
        ("Annual Report 2026 | 24", "24"),
        ("Sustainability Report 2026", None),
        ("Report 24 | 2026", "24"),
        ("Report 1899 | 2099", "1899"),
        ("Report 1900 | 2100", "2100"),
    ],
)
def test_recurring_footer_with_year(footer, expected):
    block = b(footer, y=780)
    dropped = recurring_margins([[(None, block)]] * 3, [800] * 3)
    assert printed_page([block], 800, None, dropped) == expected
    assert printed_page([block], 800, None, set()) is None
