from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from cda.parsing import store
from cda.parsing.pdf import ParsedDocument
from cda.parsing.units import ParsedUnit


async def test_ingest_reading_order(monkeypatch, tmp_path):
    units = [
        ParsedUnit("section", None, "section", 1, None, None, "Climate", "Climate", 1),
        ParsedUnit("body", "section", "paragraph", 1, None, None, "Climate", "Body", 1),
    ]
    parsed = ParsedDocument("a" * 64, 1, "test", units)
    monkeypatch.setattr(store, "parse_pdf", lambda path: parsed)
    conn = MagicMock()
    result = MagicMock()
    result.scalar_one.return_value = uuid4()
    conn.execute = AsyncMock(return_value=result)
    await store.ingest_pdf(conn, pack_id=uuid4(), kind="annual", path=tmp_path / "a.pdf")
    _, rows = conn.execute.await_args.args
    assert [r["seq"] for r in rows] == [0, 1]
    assert rows[1]["parent_id"] == rows[0]["id"]
