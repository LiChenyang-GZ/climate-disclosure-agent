"""Local MCP tools using the REST services and permissions."""

import asyncio
import json
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from importlib import import_module
from typing import Annotated, Any, Literal
from uuid import UUID

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field, TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from cda.api import service
from cda.api.errors import APIError
from cda.config import Settings
from cda.db.session import make_engine
from cda.indexing.embed import BgeEmbedder, Embedder
from cda.tools import registry
from cda.tools.context import ToolContext


def build_server(*, embedder: Embedder | None = None, engine: AsyncEngine | None = None) -> FastMCP:
    handle = os.environ.get("CDA_MCP_USER")
    database = engine
    embeddings = embedder
    embedding_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(server: FastMCP) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if database is not None:
                await database.dispose()

    server = FastMCP("Climate Disclosure Agent", lifespan=lifespan)

    @asynccontextmanager
    async def connection() -> AsyncIterator[tuple[Any, dict[str, Any]]]:
        nonlocal database
        if handle not in ("sarah", "alex"):
            raise APIError(401, "unauthorized", "Select a demo user")
        if database is None:
            database = make_engine(Settings().database_url)
        async with database.begin() as conn:
            actor = await service.load_actor(conn, handle)
            yield conn, actor

    readonly = ToolAnnotations(readOnlyHint=True)

    @server.tool(annotations=readonly)
    async def list_packs() -> dict[str, Any]:
        """List packs visible to the configured demo user."""
        async with connection() as (conn, actor):
            return {"packs": await service.list_packs(conn, actor)}

    @server.tool(annotations=readonly)
    async def get_run(run_id: UUID) -> dict[str, Any]:
        """Read a run's status, costs, timings and findings counts."""
        async with connection() as (conn, actor):
            return await service.get_run(conn, actor, run_id)

    @server.tool(annotations=readonly)
    async def get_findings(run_id: UUID, finding_id: UUID | None = None) -> dict[str, Any]:
        """Read findings or a finding's evidence and plain-text quotes."""
        async with connection() as (conn, actor):
            await service.visible_run(conn, actor, run_id)
            if finding_id is None:
                return {"findings": await service.finding_rows(conn, actor, run_id)}
            result = await service.finding_detail(conn, actor, finding_id)
            if result["run_id"] != run_id:
                raise APIError(404, "not_found", "Resource not found")
            return result

    @server.tool(annotations=readonly)
    async def search_pack(
        pack_id: UUID,
        query: str,
        mode: Literal["keyword", "semantic"] = "keyword",
        limit: Annotated[int, Field(ge=1, le=10)] = 5,
    ) -> dict[str, Any]:
        """Search visible evidence using keywords or local semantic embeddings."""
        nonlocal embeddings
        async with connection() as (conn, actor):
            await service.visible_pack(conn, actor, pack_id)
            if mode == "semantic" and embeddings is None:
                async with embedding_lock:
                    if embeddings is None:
                        candidate = BgeEmbedder()
                        await asyncio.to_thread(candidate.warm_up)
                        embeddings = candidate
            ctx = ToolContext(conn, actor["id"], pack_id, embedder=embeddings)
            result = await registry.run_tool(
                ctx, mode + "_search", {"query": query, "limit": limit}
            )
            return result.model_dump()

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)
    )
    async def start_check(
        pack_id: UUID,
        method: Literal["A", "B", "C"],
        model_key: str,
        idempotency_key: Annotated[str, Field(min_length=1, max_length=200)],
        budget_usd: Annotated[
            Decimal, Field(gt=0, le=10, decimal_places=6, allow_inf_nan=False)
        ] = Decimal("2.0"),
    ) -> dict[str, Any]:
        """Queue a check for the worker. Reuse the same idempotency_key when retrying the same request. Poll get_run for progress."""
        body = service.RunInput(
            pack_id=pack_id, method=method, model_key=model_key, budget_usd=budget_usd
        )
        async with connection() as (conn, actor):
            row, created = await service.create_run(
                conn, actor, body, idempotency_key, service.IndexState(), via="mcp"
            )
            return {"run": row, "created": created}

    # Private handler override sanitizes every error; mcp<2 is pinned for this reason.
    @server._mcp_server.call_tool(validate_input=False)  # type: ignore[untyped-decorator]
    async def dispatch(name: str, arguments: dict[str, Any]) -> CallToolResult:
        try:
            tool = server._tool_manager.get_tool(name)
            if tool is None:
                raise APIError(422, "invalid_request", "Invalid request")
            validated = tool.fn_metadata.arg_model.model_validate(arguments)
            result = await tool.fn(**validated.model_dump())
            data = json.loads(TypeAdapter(Any).dump_json(result))
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(data))], structuredContent=data
            )
        except ValidationError as exc:
            fields = sorted(
                {
                    str(e["loc"][0])
                    for e in exc.errors()
                    if e["loc"]
                    and tool is not None
                    and e["loc"][0] in tool.fn_metadata.arg_model.model_fields
                }
            )
            error = {"error": "invalid_request", "message": "Invalid fields: " + ", ".join(fields)}
        except APIError as exc:
            error = {"error": exc.code, "message": exc.message, **exc.extra}
        except Exception as exc:  # noqa: BLE001 - sanitize all tool failures
            logging.getLogger(__name__).error("%s", type(exc).__name__)
            error = {"error": "internal_error", "message": "Request failed"}
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(error))], isError=True
        )

    return server


def main() -> None:
    if os.environ.get("CDA_MCP_USER") not in ("sarah", "alex"):
        print("CDA_MCP_USER must be sarah or alex", file=sys.stderr)
        raise SystemExit(2)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    # Import before stdio's stdin thread starts to avoid Windows C-extension import deadlocks.
    import_module("numpy")
    import_module("sentence_transformers")
    build_server().run(transport="stdio")
