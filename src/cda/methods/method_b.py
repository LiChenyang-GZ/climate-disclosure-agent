"""Single-call classic RAG worker."""

from cda.agent.prompts import render_method_b
from cda.agent.state import WorkerResult, WorkerState
from cda.agent.worker_graph import WorkerDeps, _finish
from cda.checks.models import Check, CheckSet
from cda.llm.calls import record_call
from cda.llm.types import Message
from cda.methods.retrieval import RetrievalError, _data, hybrid_search, load_context
from cda.methods.single_call import run_single_call
from cda.tools.context import ToolContext
from cda.tools.registry import run_tool
from cda.verify.evidence import verify_evidence


async def run_method_b(
    deps: WorkerDeps,
    check: Check,
    check_set: CheckSet,
    *,
    top_k: int = 10,
    min_vector_score: float | None = None,
) -> WorkerResult:
    ctx = ToolContext(deps.conn, deps.user_id, deps.pack_id, deps.embedder, deps.tool_timeout_s)
    state = WorkerState(ctx)

    def finish(status: str, reason: str | None = None) -> WorkerResult:
        _finish(state, check, status, reason)
        assert state.result is not None
        return state.result

    if deps.embedder is None:
        return finish("error", "embedder_unavailable")
    try:
        inventory = _data(await run_tool(ctx, "list_documents", {}))
        parts = {p["part_id"] for d in inventory.get("documents", []) for p in d["parts"]}
        result = await hybrid_search(ctx, [check.title, *check.queries], top_k=top_k)
        if not result.hits:
            return finish("needs_review", "no_retrieval_hits")
        if min_vector_score is not None and (
            result.best_vector_score is None or result.best_vector_score < min_vector_score
        ):
            return finish("needs_review", "low_retrieval_score")
        context = await load_context(ctx, result.hits)
    except RetrievalError as exc:
        return finish("error", "retrieval_" + exc.category)
    system, user = render_method_b(check, context)
    messages = [Message("system", system), Message("user", user)]
    complete = not ctx.coverage.missing_parts(parts) and ctx.coverage.distinct_queries == len(
        result.queries
    )
    return await run_single_call(
        deps,
        check,
        messages,
        context_ids={b["unit_id"] for b in context},
        search_complete=complete,
        verifier=verify_evidence,
        recorder=record_call,
    )
