"""Whole-pack, one-call baseline used by LC experiments."""

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from cda.agent.finding import SubmitFinding
from cda.agent.prompts import render_lc
from cda.agent.state import WorkerResult, WorkerState
from cda.agent.worker_graph import WorkerDeps, _finish
from cda.checks.models import Check, CheckSet
from cda.db.models import Document, Unit
from cda.llm.calls import record_call
from cda.llm.cost import estimate_tokens
from cda.llm.types import Message, ToolSpec
from cda.methods.retrieval import RetrievalError, _data
from cda.methods.single_call import run_single_call
from cda.tools.context import ToolContext
from cda.tools.parts_sql import part_kind, unit_query
from cda.tools.registry import run_tool
from cda.tools.rls import tool_scope
from cda.verify.evidence import verify_evidence

Scope = Literal["pack", "sustainability"]


@dataclass
class LongContextPack:
    units: list[dict[str, Any]]
    aliases: dict[str, UUID]
    parts: set[str]
    text: str
    error_reason: str | None = None

    @classmethod
    def forbidden(cls) -> "LongContextPack":
        return cls([], {}, set(), "", "retrieval_forbidden")


async def _query_units(ctx: ToolContext, scope: Scope) -> list[dict[str, Any]]:
    stmt = unit_query(ctx).where(Unit.kind.in_(("paragraph", "table_row")))
    if scope == "sustainability":
        stmt = stmt.where(part_kind == "sustainability")
    stmt = stmt.order_by(Document.filename, Unit.seq)
    async with tool_scope(ctx.conn, ctx.user_id, ctx.timeout_s):
        rows = (await ctx.conn.execute(stmt)).mappings().all()
    return [dict(row) for row in rows]


async def build_pack(ctx: ToolContext, scope: Scope = "pack") -> LongContextPack:
    """Load visible report units through the same permission query as retrieval tools."""
    try:
        inventory = await run_tool(ctx, "list_documents", {})
        inventory_data = _data(inventory)
        rows = await _query_units(ctx, scope)
    except RetrievalError as exc:
        if exc.category == "forbidden":
            return LongContextPack.forbidden()
        raise
    ordered = sorted(rows, key=lambda row: (row["filename"], row["seq"]))
    kept = [
        row
        for row in ordered
        if row["kind"] in ("paragraph", "table_row")
        and (scope != "sustainability" or row["part_kind"] == "sustainability")
    ]
    aliases: dict[str, UUID] = {}
    lines: list[str] = []
    previous_document: str | None = None
    for index, row in enumerate(kept, 1):
        alias = f"u{index}"
        aliases[alias] = row["id"] if isinstance(row["id"], UUID) else UUID(str(row["id"]))
        document_id = str(row["document_id"])
        if document_id != previous_document:
            lines.append(f"## {row['filename']}")
            previous_document = document_id
        section = row["section_path"]
        section_path = (
            " > ".join(section) if isinstance(section, (list, tuple)) else str(section or "")
        )
        page = row.get("pdf_page")
        label = row.get("part_label") or ""
        lines.append(
            f"[{alias}] {row['filename']} | {row['part_kind']}/{label} | "
            f"pdf page {page} | {section_path}\n{row['text']}"
        )
    part_ids = {
        str(part["part_id"])
        for document in inventory_data.get("documents", [])
        for part in document.get("parts", [])
        if scope != "sustainability" or part.get("kind") == "sustainability"
    }
    part_ids.update(str(row["part_id"]) for row in kept)
    return LongContextPack(
        kept,
        aliases,
        part_ids,
        "\n\n".join(lines),
    )


async def run_long_context(
    deps: WorkerDeps,
    check: Check,
    check_set: CheckSet,
    *,
    scope: Scope = "pack",
    max_prompt_tokens: int = 200_000,
) -> WorkerResult:
    state = WorkerState(
        ToolContext(deps.conn, deps.user_id, deps.pack_id, None, deps.tool_timeout_s)
    )

    def finish(status: str, reason: str | None = None) -> WorkerResult:
        _finish(state, check, status, reason)
        assert state.result is not None
        return state.result

    pack = deps.lc_pack
    if pack is None:
        pack = await build_pack(state.context, scope)
    if pack.error_reason:
        return finish("error", pack.error_reason)
    if not pack.units:
        return finish("needs_review", "no_text")

    system, pack_text, check_text = render_lc(check, pack.text)
    messages = [Message("system", system), Message("user", pack_text), Message("user", check_text)]
    tools = [ToolSpec("submit_finding", "Submit the finding for this check.", SubmitFinding)]
    if estimate_tokens(messages, tools) > max_prompt_tokens:
        return finish("error", "context_too_large")
    return await run_single_call(
        deps,
        check,
        messages,
        context_ids={str(uid) for uid in pack.aliases.values()},
        search_complete=True,
        aliases=pack.aliases,
        verifier=verify_evidence,
        recorder=record_call,
    )
