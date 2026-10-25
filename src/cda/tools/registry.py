"""Validated dispatch with bounded execution time."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from cda.tools.context import ToolContext
from cda.tools.envelope import ToolResult, bad_args, timeout
from cda.tools.keyword_search import SearchArgs, keyword_search
from cda.tools.list_documents import ListDocumentsArgs, list_documents
from cda.tools.read_unit import ReadUnitArgs, read_unit
from cda.tools.semantic_search import semantic_search


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[..., Awaitable[ToolResult]]


TOOLS: dict[str, ToolDef] = {
    t.name: t
    for t in [
        ToolDef(
            "list_documents",
            "List visible documents and logical parts.",
            ListDocumentsArgs,
            list_documents,
        ),
        ToolDef("keyword_search", "Search evidence with keywords.", SearchArgs, keyword_search),
        ToolDef("semantic_search", "Search evidence by meaning.", SearchArgs, semantic_search),
        ToolDef("read_unit", "Read evidence and its context.", ReadUnitArgs, read_unit),
    ]
}


def tools_for(method: str) -> list[ToolDef]:
    return [t for t in TOOLS.values() if t.name != "semantic_search" or method == "C"]


async def run_tool(ctx: ToolContext, name: str, raw_args: dict[str, Any]) -> ToolResult:
    tool = TOOLS.get(name)
    if tool is None:
        return bad_args("Unknown tool.")
    try:
        args = tool.args_model.model_validate(raw_args)
    except ValidationError as exc:
        # Only known schema field names, never inputs or exception descriptions.
        fields = sorted(
            {
                str(e["loc"][0])
                for e in exc.errors()
                if e["loc"] and e["loc"][0] in tool.args_model.model_fields
            }
        )
        return bad_args("Invalid arguments" + (": " + ", ".join(fields) if fields else "") + ".")
    if name == "semantic_search" and ctx.embedder is None:
        return bad_args("Semantic search not available.")
    task = asyncio.ensure_future(tool.handler(ctx, args))
    try:
        return await asyncio.wait_for(task, timeout=ctx.timeout_s)
    except TimeoutError:
        if task.cancelled():
            return timeout()
        return task.result()
