"""Revision regressions runnable without PostgreSQL."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from cda.tools.context import Coverage
from cda.tools.envelope import ToolResult
from cda.tools.keyword_search import SearchArgs, search_scope
from cda.tools.read_unit import ReadUnitArgs, read_unit


@pytest.mark.parametrize("scope", ["documents", "parts", "disjoint"])
@pytest.mark.parametrize("tool", ["keyword_search", "semantic_search"])
async def test_empty_scope_does_not_record(monkeypatch, scope, tool):
    doc, other = uuid4(), uuid4()
    docs = [
        {"document_id": str(doc), "parts": [{"part_id": "part"}]},
        {"document_id": str(other), "parts": [{"part_id": "other"}]},
    ]
    monkeypatch.setattr("cda.tools.keyword_search.inventory", AsyncMock(return_value=docs))
    ctx = SimpleNamespace(coverage=Coverage())
    filters = {
        "documents": {"document_ids": []},
        "parts": {"part_ids": []},
        "disjoint": {"document_ids": [other], "part_ids": ["part"]},
    }
    result = await search_scope(ctx, SearchArgs(query="climate", **filters[scope]), tool)
    assert isinstance(result, ToolResult) and result.category == "bad_args"
    assert result.message == "Search scope is empty; widen document_ids or part_ids."
    assert ctx.coverage.distinct_queries == 0
    assert not ctx.coverage.queries and not ctx.coverage.parts_covered
    result = await search_scope(ctx, SearchArgs(query="climate", part_ids=["part"]), tool)
    assert result == {"part"}
    assert ctx.coverage.distinct_queries == 1


@pytest.mark.parametrize("kind", ["table", "section"])
async def test_parent_container(monkeypatch, kind):
    doc, outer, uid, child = [uuid4() for _ in range(4)]

    def row(unit_id, parent_id, unit_kind, seq):
        return {
            "id": unit_id,
            "document_id": doc,
            "parent_id": parent_id,
            "kind": unit_kind,
            "seq": seq,
            "text": str(unit_id),
            "pdf_page": 1,
            "page_half": None,
            "printed_page": None,
            "section_path": "",
            "part_id": "part",
        }

    container = row(uid, outer if kind == "table" else None, kind, 1)
    children = [container, row(child, uid, "paragraph", 2)]
    first = MagicMock()
    first.mappings.return_value.first.return_value = container
    second = MagicMock()
    second.mappings.return_value.first.return_value = row(outer, None, "section", 0)
    second.mappings.return_value.__iter__.side_effect = lambda: iter(children)
    conn = SimpleNamespace(execute=AsyncMock(side_effect=[first, second]))
    ctx = SimpleNamespace(conn=conn)
    monkeypatch.setattr("cda.tools.read_unit.inventory", AsyncMock(return_value=[]))
    monkeypatch.setattr("cda.tools.read_unit.unit_query", lambda ctx: MagicMock())
    result = await read_unit(ctx, ReadUnitArgs(unit_id=uid, mode="parent"))
    expected = [uid] if kind == "table" else [uid, child]
    assert [u["unit_id"] for u in result.data["units"]] == list(map(str, expected))
    assert conn.execute.await_count == (1 if kind == "table" else 2)
