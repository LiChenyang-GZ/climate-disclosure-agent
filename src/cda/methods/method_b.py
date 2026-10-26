"""Single-call classic RAG worker."""

from dataclasses import replace
from decimal import Decimal
from uuid import UUID

from cda.agent.finding import SubmitFinding
from cda.agent.prompts import render_method_b
from cda.agent.state import WorkerResult, WorkerState
from cda.agent.worker_graph import WorkerDeps, _finish
from cda.checks.models import Check, CheckSet
from cda.llm.calls import record_call
from cda.llm.client import parse_tool_call
from cda.llm.cost import cost_usd, estimate_tokens, max_cost_usd
from cda.llm.types import LLMAuthError, Message, ToolArgsError, ToolSpec
from cda.methods.retrieval import RetrievalError, _data, hybrid_search, load_context
from cda.tools.context import ToolContext
from cda.tools.registry import run_tool
from cda.verify.evidence import EvidenceItem, verify_evidence


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

    def finish(status: str, reason: str | None = None, *, high: bool | None = None) -> WorkerResult:
        _finish(state, check, status, reason, None if high is None else "high" if high else "low")
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
    tools = [ToolSpec("submit_finding", "Submit the finding for this check.", SubmitFinding)]
    price = deps.models[deps.model_key]
    estimate = max_cost_usd(
        price,
        prompt_tokens=estimate_tokens(messages, tools),
        max_output_tokens=deps.max_output_tokens,
    )
    if not await deps.budget.reserve(estimate):
        return finish("not_checked", "budget_exhausted")
    state.step = 1
    try:
        response = await deps.llm.complete(
            messages=messages,
            tools=tools,
            model_key=deps.model_key,
            seed=deps.seed,
            tool_choice="submit_finding",
            max_output_tokens=deps.max_output_tokens,
        )
    except ToolArgsError:
        await deps.budget.settle(estimate, estimate)
        state.cost = estimate
        return finish("error", "invalid_output")
    except LLMAuthError:
        await deps.budget.settle(estimate, Decimal(0))
        raise
    except Exception:  # noqa: BLE001 - single-call failures are invalid_output
        await deps.budget.settle(estimate, Decimal(0))
        return finish("error", "invalid_output")
    state.cost = cost_usd(price, response.usage)
    state.tokens_in, state.tokens_out = response.usage.tokens_in, response.usage.tokens_out
    await deps.budget.settle(estimate, state.cost)
    await record_call(
        deps.conn, run_id=deps.run_id, check_id=check.id, step=1, response=response, cost=state.cost
    )
    calls = response.message.tool_calls
    if len(calls) != 1 or calls[0].name != "submit_finding":
        return finish("error", "invalid_output")
    try:
        parsed = parse_tool_call(calls[0], tools)
        assert isinstance(parsed, SubmitFinding)
        parsed.check_rules(check)
    except (ToolArgsError, ValueError):
        return finish("error", "invalid_output")
    state.submission = parsed
    state.evidence = await verify_evidence(
        deps.conn,
        user_id=deps.user_id,
        pack_id=deps.pack_id,
        items=[EvidenceItem(UUID(e.unit_id), e.quote, e.role) for e in parsed.evidence],
        timeout_s=deps.tool_timeout_s,
    )
    context_ids = {b["unit_id"] for b in context}
    state.evidence = [
        e if str(e.unit_id) in context_ids else replace(e, ok=False, reason="not_in_context")
        for e in state.evidence
    ]
    if any(not e.ok for e in state.evidence):
        return finish("needs_review", "quote_verification_failed")
    complete = not ctx.coverage.missing_parts(parts) and ctx.coverage.distinct_queries == len(
        result.queries
    )
    if parsed.status == "no_evidence_found" and not complete:
        return finish("needs_review", "insufficient_search")
    high = parsed.status != "no_evidence_found" and any(e.ok for e in state.evidence) and complete
    return finish(parsed.status, high=high)
