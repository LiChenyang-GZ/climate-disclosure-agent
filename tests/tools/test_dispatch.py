import asyncio
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from cda.tools.context import Coverage, ToolContext
from cda.tools.registry import TOOLS, ToolDef, run_tool, tools_for


@pytest.fixture
def ctx():
    return ToolContext(MagicMock(), uuid4(), uuid4(), timeout_s=0.1)


@pytest.mark.parametrize(
    "name,args",
    [
        ("unknown", {}),
        ("list_documents", {"private": 1}),
        ("keyword_search", {"query": "x", "limit": 11}),
        ("keyword_search", {"query": "x", "limit": 0}),
        ("keyword_search", {"query": ""}),
        ("keyword_search", {"query": "  "}),
        ("keyword_search", {"query": "x" * 201}),
        ("read_unit", {"unit_id": str(uuid4()), "neighbours": 3}),
        ("read_unit", {"unit_id": str(uuid4()), "mode": "page"}),
    ],
)
async def test_bad_args(ctx, name, args):
    result = await run_tool(ctx, name, args)
    assert result.category == "bad_args"
    assert "private" not in result.message and "page" not in result.message


async def test_timeout(ctx, monkeypatch):
    async def slow(ctx, args):
        await asyncio.sleep(1)

    monkeypatch.setitem(
        TOOLS, "slow", ToolDef("slow", "", TOOLS["list_documents"].args_model, slow)
    )
    result = await run_tool(ctx, "slow", {})
    assert result.category == "timeout" and result.retryable


async def test_exception(ctx, monkeypatch):
    async def bug(ctx, args):
        raise RuntimeError("bug")

    monkeypatch.setitem(TOOLS, "bug", ToolDef("bug", "", TOOLS["list_documents"].args_model, bug))
    with pytest.raises(RuntimeError):
        await run_tool(ctx, "bug", {})


async def test_unavailable(ctx):
    assert (await run_tool(ctx, "semantic_search", {"query": "x"})).category == "bad_args"
    assert "semantic_search" not in [t.name for t in tools_for("A")]
    assert "semantic_search" in [t.name for t in tools_for("C")]


def test_strict():
    for tool in TOOLS.values():
        schema = tool.args_model.model_json_schema()
        assert schema["additionalProperties"] is False
        for name, field in schema["properties"].items():
            assert name in schema.get("required", []) or any(
                v.get("type") == "null" for v in field.get("anyOf", [])
            )


def test_repeated():
    c = Coverage()
    assert c.record("keyword_search", " Climate  risk ", {"a"})
    assert not c.record("keyword_search", "climate risk", {"a"})
    assert c.record("keyword_search", "climate risk", {"b"})
    assert c.distinct_queries == 1


def test_cross_tool():
    c = Coverage()
    c.record("keyword_search", "Climate", {"a"})
    c.record("semantic_search", "climate", {"b"})
    assert c.distinct_queries == 1
    assert c.missing_parts({"a", "b", "c"}) == {"c"}
