import asyncio
import time
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from cda.db.models import Unit
from cda.parsing import store
from cda.parsing.pdf import ParsedDocument
from cda.parsing.units import ParsedUnit


@pytest.fixture
def ingestion(monkeypatch):
    units = [
        ParsedUnit("heading", None, "section", 1, None, None, "Climate", "Climate", 2),
        ParsedUnit("body", "heading", "paragraph", 1, None, None, "Climate", "Body.", 2),
    ]
    parsed = ParsedDocument("a" * 64, 1, "test-parser", units)
    monkeypatch.setattr(store, "parse_pdf", lambda path: parsed)
    document_id = uuid4()
    result = MagicMock()
    result.scalar_one.return_value = document_id
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=result)
    return conn, document_id, parsed


async def test_ingest_keeps_event_loop_responsive(ingestion, monkeypatch, tmp_path):
    conn, document_id, parsed = ingestion

    def slow_parse(path):
        time.sleep(0.3)
        return parsed

    monkeypatch.setattr(store, "parse_pdf", slow_parse)
    heartbeat = asyncio.create_task(asyncio.sleep(0.05))
    try:
        assert (
            await store.ingest_pdf(
                conn, pack_id=uuid4(), kind="annual", path=tmp_path / "report.pdf"
            )
            == document_id
        )
        assert heartbeat.done()
    finally:
        await heartbeat


async def test_ingest_batches_units_with_client_ids(ingestion, tmp_path):
    conn, document_id, parsed = ingestion
    await store.ingest_pdf(conn, pack_id=uuid4(), kind="annual", path=tmp_path / "report.pdf")
    assert conn.execute.await_count == 2
    statement, rows = conn.execute.await_args.args
    assert statement.table.compare(Unit.__table__)
    assert len(rows) == len(parsed.units)
    assert all(isinstance(row["id"], UUID) for row in rows)
    assert len({row["id"] for row in rows}) == len(rows)
    assert rows[0]["parent_id"] is None
    assert rows[1]["parent_id"] == rows[0]["id"]
    assert all(row["document_id"] == document_id for row in rows)
    conn.begin_nested.return_value.__aenter__.assert_awaited_once()
    conn.begin_nested.return_value.__aexit__.assert_awaited_once()
