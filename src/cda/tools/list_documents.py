"""Visible document inventory."""

from pydantic import BaseModel, ConfigDict

from cda.tools.context import ToolContext
from cda.tools.envelope import ToolResult, forbidden, found, not_found
from cda.tools.parts_sql import inventory


class ListDocumentsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def list_documents(ctx: ToolContext, args: ListDocumentsArgs) -> ToolResult:
    docs = await inventory(ctx)
    if docs is None:
        return forbidden()
    if not docs:
        return not_found("No documents in this pack.", {"documents": []})
    return found({"documents": docs})
