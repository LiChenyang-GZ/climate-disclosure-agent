import json
import os
import sys
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from cda.api import service
from cda.api.errors import APIError
from cda.mcp_server.server import build_server, main


def test_startup_imports_embedding_stack_before_stdio(monkeypatch):
    monkeypatch.setenv("CDA_MCP_USER", "sarah")
    calls = []

    def run(server, *, transport):
        assert "numpy" in sys.modules
        assert "sentence_transformers" in sys.modules
        assert transport == "stdio"
        calls.append(transport)

    def warm_up(embedder):
        pytest.fail("Model weights must remain lazy at startup")

    monkeypatch.setattr("cda.mcp_server.server.FastMCP.run", run)
    monkeypatch.setattr("cda.mcp_server.server.BgeEmbedder.warm_up", warm_up)
    main()
    assert calls == ["stdio"]


@pytest.mark.parametrize("handle", [None, "bob"])
def test_startup_identity(monkeypatch, capsys, handle):
    if handle is None:
        monkeypatch.delenv("CDA_MCP_USER", raising=False)
    else:
        monkeypatch.setenv("CDA_MCP_USER", handle)
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert capsys.readouterr().err == "CDA_MCP_USER must be sarah or alex\n"


async def test_tools(monkeypatch):
    monkeypatch.setenv("CDA_MCP_USER", "sarah")
    async with create_connected_server_and_client_session(build_server()) as session:
        tools = (await session.list_tools()).tools
        assert {t.name for t in tools} == {
            "list_packs",
            "get_run",
            "get_findings",
            "search_pack",
            "start_check",
        }
        for t in tools:
            assert "review" not in t.name
            assert t.annotations.readOnlyHint == (t.name != "start_check")
        start = next(t for t in tools if t.name == "start_check")
        assert start.annotations.destructiveHint is False
        assert start.annotations.idempotentHint is True


@pytest.mark.parametrize(
    "name,args,field",
    [
        ("search_pack", {"pack_id": str(uuid4()), "query": "x", "limit": 11}, "limit"),
        ("search_pack", {"pack_id": str(uuid4()), "query": "x", "mode": "other"}, "mode"),
        (
            "start_check",
            {"pack_id": str(uuid4()), "method": "LC", "model_key": "luna", "idempotency_key": "x"},
            "method",
        ),
        (
            "start_check",
            {
                "pack_id": str(uuid4()),
                "method": "A",
                "model_key": "luna",
                "idempotency_key": "x",
                "budget_usd": 0,
            },
            "budget_usd",
        ),
        ("get_run", {"run_id": "SELECT secret"}, "run_id"),
    ],
)
async def test_validation(monkeypatch, name, args, field):
    monkeypatch.setenv("CDA_MCP_USER", "sarah")
    async with create_connected_server_and_client_session(build_server()) as session:
        result = await session.call_tool(name, args)
        assert result.isError
        error = json.loads(result.content[0].text)
        assert error["error"] == "invalid_request"
        assert field in error["message"]
        assert "SELECT" not in result.content[0].text


@pytest.mark.parametrize(
    "error,code",
    [
        (APIError(401, "unauthorized", "Select a demo user"), "unauthorized"),
        (RuntimeError("SELECT secret FROM x at C:\\path"), "internal_error"),
    ],
)
async def test_safe_errors(monkeypatch, error, code):
    monkeypatch.setenv("CDA_MCP_USER", "alex")
    monkeypatch.setattr(service, "load_actor", AsyncMock(side_effect=error))
    from contextlib import asynccontextmanager

    class Engine:
        async def dispose(self):
            pass

        @asynccontextmanager
        async def begin(self):
            yield None

    async with create_connected_server_and_client_session(build_server(engine=Engine())) as session:
        result = await session.call_tool("list_packs", {})
        assert result.isError
        assert json.loads(result.content[0].text)["error"] == code
        assert "SELECT" not in result.content[0].text
        assert "C:" not in result.content[0].text


async def test_stdio():
    from mcp import ClientSession

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "cda.mcp_server"],
        env={**os.environ, "CDA_MCP_USER": "sarah"},
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        assert len((await session.list_tools()).tools) == 5
