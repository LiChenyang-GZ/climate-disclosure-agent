from uuid import uuid4

from cda.tools.keyword_search import snippet
from cda.tools.read_unit import cap_units


def test_snippet_middle():
    text = "a " * 250 + "climate risk" + " z" * 250
    result = snippet(text, "climate")
    assert result.strip("…") in text and "climate" in result and len(result) <= 300


def test_truncate_requested_first():
    rows = [
        {
            "id": uuid4(),
            "seq": i,
            "text": "x" * 3000,
            "kind": "paragraph",
            "pdf_page": 1,
            "page_half": None,
            "printed_page": None,
            "section_path": "",
            "part_id": "part",
        }
        for i in range(3)
    ]
    units, truncated = cap_units(rows, rows[1])
    assert truncated and sum(len(u["text"]) for u in units) <= 4000
    assert [u["unit_id"] for u in units] == list(map(str, [rows[0]["id"], rows[1]["id"]]))
    assert units[1]["text"] == rows[1]["text"]
    assert units[0]["text"].endswith("\n[truncated]")
