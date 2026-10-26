"""Explicit LangGraph worker for one disclosure check."""

import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.ext.asyncio import AsyncConnection

from cda.agent.budget import Budget
from cda.agent.finding import SubmitFinding
from cda.agent.nosevidence import accept
from cda.agent.prompts import render
from cda.agent.state import WorkerResult, WorkerState
from cda.checks.models import Check, CheckSet
from cda.indexing.embed import Embedder
from cda.llm.calls import record_call
from cda.llm.client import LLMClient, parse_tool_call
from cda.llm.cost import ModelTable, cost_usd, estimate_tokens, max_cost_usd
from cda.llm.types import (
    LLMAuthError,
    LLMBadRequest,
    LLMRetryExhausted,
    Message,
    ToolArgsError,
    ToolSpec,
)
from cda.tools.context import ToolContext
from cda.tools.registry import run_tool, tools_for
from cda.verify.evidence import EvidenceItem, verify_evidence

logger = logging.getLogger(__name__)


@dataclass
class WorkerDeps:
    llm: LLMClient
    models: ModelTable
    model_key: str
    seed: int | None
    conn: AsyncConnection
    user_id: UUID
    pack_id: UUID
    run_id: UUID
    method: Literal["A", "B", "C"]
    embedder: Embedder | None
    budget: Budget
    step_cap: int = 12
    token_cap: int = 60_000
    max_output_tokens: int = 2000
    tool_timeout_s: float = 10.0


class GraphState(TypedDict):
    worker: WorkerState


def _finish(
    s: WorkerState,
    check: Check,
    status: str,
    reason: str | None = None,
    confidence: Literal["high", "low"] | None = None,
) -> None:
    f = s.submission
    s.result = WorkerResult(
        check.id,
        status,
        confidence,
        f.rationale if f else "",
        f.missing_aspects if f else [],
        f.relief_applied if f else False,
        reason,
        s.evidence,
        f.facts if f else [],
        s.step,
        s.tokens_in,
        s.tokens_out,
        s.cost,
    )
    s.route = END


def _retry(
    s: WorkerState, check: Check, message: str, *, instruction: str = "call submit_finding again"
) -> None:
    s.invalid += 1
    s.history.append(
        Message(
            "tool" if s.call else "user",
            message[:1000] + "; " + instruction,
            tool_call_id=s.call.id if s.call else None,
        )
    )
    if s.forced:
        _finish(s, check, "needs_review", "cap_hit")
    elif s.invalid >= 2:
        _finish(s, check, "error", "invalid_output")
    else:
        s.route = "guard"


def build_graph(
    deps: WorkerDeps, check: Check, check_set: CheckSet
) -> CompiledStateGraph[GraphState, None, GraphState, GraphState]:
    tools = [ToolSpec(t.name, t.description, t.args_model) for t in tools_for(deps.method)]
    tools.append(ToolSpec("submit_finding", "Submit the finding for this check.", SubmitFinding))
    system, user = render(check, check_set.min_searches_for(check.id))

    async def build_prompt(state: GraphState) -> GraphState:
        s = state["worker"]
        if s.step == 0:
            inventory = await run_tool(s.context, "list_documents", {})
            if not inventory.ok:
                raise RuntimeError("Inventory unavailable")
            for document in (inventory.data or {}).get("documents", []):
                for part in document["parts"]:
                    s.parts[part["part_id"]] = part.get("label") or part.get("kind", "Part")
        missing = s.context.coverage.missing_parts(set(s.parts))
        summary = (
            f"State summary: steps left {deps.step_cap - s.step}; "
            f"tokens left {deps.token_cap - s.tokens_in - s.tokens_out}; "
            f"distinct queries {s.context.coverage.distinct_queries} vs min_searches "
            f"{check_set.min_searches_for(check.id)}; unsearched parts: "
            + ", ".join(f"{p}: {s.parts[p]}" for p in sorted(missing))
            + "; candidate unit ids: "
            + ", ".join(s.candidate_units[-20:])
        )
        s.messages = [
            Message("system", system),
            Message("user", user),
            *s.history,
            Message("user", summary),
        ]
        prompt_tokens = estimate_tokens(s.messages, tools)
        s.forced = (
            s.step >= deps.step_cap - 1
            or prompt_tokens + deps.max_output_tokens > deps.token_cap - s.tokens_in - s.tokens_out
        )
        if s.forced:
            s.messages[-1] = Message("user", summary + "; LAST STEP: call submit_finding now.")
        return state

    async def call_model(state: GraphState) -> GraphState:
        s = state["worker"]
        price = deps.models[deps.model_key]
        estimate = max_cost_usd(
            price,
            prompt_tokens=estimate_tokens(s.messages, tools),
            max_output_tokens=deps.max_output_tokens,
        )
        if not await deps.budget.reserve(estimate):
            _finish(s, check, "not_checked", "budget_exhausted")
            return state
        s.step += 1
        s.call = None
        try:
            response = await deps.llm.complete(
                messages=s.messages,
                tools=tools,
                model_key=deps.model_key,
                seed=deps.seed,
                tool_choice="submit_finding" if s.forced else "required",
                max_output_tokens=deps.max_output_tokens,
            )
        except ToolArgsError:
            await deps.budget.settle(estimate, estimate)
            s.cost += estimate
            _retry(s, check, "Invalid tool arguments")
            return state
        except Exception:
            # No usage is available for a failed request; release the reservation.
            await deps.budget.settle(estimate, Decimal(0))
            raise
        actual = cost_usd(price, response.usage)
        await deps.budget.settle(estimate, actual)
        s.cost += actual
        s.tokens_in += response.usage.tokens_in
        s.tokens_out += response.usage.tokens_out
        await record_call(
            deps.conn,
            run_id=deps.run_id,
            check_id=check.id,
            step=s.step,
            response=response,
            cost=actual,
        )
        s.history.append(response.message)
        if len(response.message.tool_calls) != 1:
            _retry(s, check, "Expected exactly one tool call")
            return state
        s.call = response.message.tool_calls[0]
        if s.call.name not in {t.name for t in tools}:
            _retry(s, check, "Unknown tool")
        elif s.call.name == "submit_finding":
            s.route = "validate"
        elif s.forced:
            _finish(s, check, "needs_review", "cap_hit")
        else:
            s.route = "run_tool"
        return state

    async def run_tool_node(state: GraphState) -> GraphState:
        s = state["worker"]
        assert s.call is not None
        try:
            args = json.loads(s.call.arguments)
            if not isinstance(args, dict):
                raise TypeError("Expected object")
        except (ValueError, TypeError):
            _retry(
                s,
                check,
                f"Invalid JSON arguments for {s.call.name}",
                instruction="call the tool again with a JSON object.",
            )
            return state
        s.tool_result = await run_tool(s.context, s.call.name, args)
        s.route = "update_state"
        return state

    def update_state(state: GraphState) -> GraphState:
        s = state["worker"]
        assert s.tool_result is not None and s.call is not None
        s.history.append(Message("tool", s.tool_result.model_dump_json(), tool_call_id=s.call.id))

        def collect(value: Any) -> None:
            if isinstance(value, dict):
                uid = value.get("unit_id")
                if isinstance(uid, str):
                    if uid in s.candidate_units:
                        s.candidate_units.remove(uid)
                    s.candidate_units.append(uid)
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        collect(s.tool_result.data)
        return state

    def guard(state: GraphState) -> GraphState:
        s = state["worker"]
        if s.step >= deps.step_cap or s.tokens_in + s.tokens_out >= deps.token_cap:
            _finish(s, check, "needs_review", "cap_hit")
        else:
            s.route = "build_prompt"
        return state

    def validate(state: GraphState) -> GraphState:
        s = state["worker"]
        assert s.call is not None
        try:
            parsed = parse_tool_call(s.call, tools)
            assert isinstance(parsed, SubmitFinding)
            parsed.check_rules(check)
        except (ToolArgsError, ValueError) as exc:
            _retry(s, check, str(exc))
            return state
        s.submission = parsed
        s.route = "verify"
        return state

    async def verify(state: GraphState) -> GraphState:
        s = state["worker"]
        assert s.submission is not None
        s.evidence = await verify_evidence(
            deps.conn,
            user_id=deps.user_id,
            pack_id=deps.pack_id,
            items=[EvidenceItem(UUID(e.unit_id), e.quote, e.role) for e in s.submission.evidence],
            timeout_s=deps.tool_timeout_s,
        )
        failures = [e for e in s.evidence if not e.ok]
        if failures:
            s.verification_failures += 1
            assert s.call is not None
            s.history.append(
                Message(
                    "tool",
                    json.dumps([{"unit_id": str(e.unit_id), "reason": e.reason} for e in failures])
                    + "; call submit_finding again with corrected quotes",
                    tool_call_id=s.call.id,
                )
            )
            if s.forced:
                _finish(s, check, "needs_review", "cap_hit")
            elif s.verification_failures >= 2:
                _finish(s, check, "needs_review", "quote_verification_failed")
            else:
                s.route = "guard"
        else:
            s.route = "decide"
        return state

    def decide(state: GraphState) -> GraphState:
        s = state["worker"]
        assert s.submission is not None
        if s.forced:
            _finish(s, check, "needs_review", "cap_hit")
        elif s.submission.status == "no_evidence_found" and not accept(
            s.context.coverage, check_set.min_searches_for(check.id), set(s.parts)
        ):
            _finish(s, check, "needs_review", "insufficient_search")
        else:
            high = (
                s.submission.status != "no_evidence_found"
                and any(e.ok for e in s.evidence)
                and not s.context.coverage.missing_parts(set(s.parts))
            )
            _finish(s, check, s.submission.status, confidence="high" if high else "low")
        return state

    graph = StateGraph(GraphState)
    graph.add_node("build_prompt", build_prompt)
    graph.add_node("call_model", call_model)
    graph.add_node("run_tool", run_tool_node)
    graph.add_node("update_state", update_state)
    graph.add_node("guard", guard)
    graph.add_node("validate", validate)
    graph.add_node("verify", verify)
    graph.add_node("decide", decide)
    graph.add_edge(START, "build_prompt")
    graph.add_edge("build_prompt", "call_model")

    def route(state: GraphState) -> str:
        return state["worker"].route

    for name in ("call_model", "run_tool", "guard", "validate", "verify"):
        graph.add_conditional_edges(name, route)
    graph.add_edge("update_state", "guard")
    graph.add_edge("decide", END)
    return graph.compile()


async def run_worker(deps: WorkerDeps, check: Check, check_set: CheckSet) -> WorkerResult:
    s = WorkerState(
        ToolContext(deps.conn, deps.user_id, deps.pack_id, deps.embedder, deps.tool_timeout_s)
    )
    try:
        await build_graph(deps, check, check_set).ainvoke(
            {"worker": s}, config={"recursion_limit": max(100, deps.step_cap * 8 + 10)}
        )
    except LLMAuthError:
        raise
    except LLMRetryExhausted:
        _finish(s, check, "error", "llm_unavailable")
    except LLMBadRequest:
        _finish(s, check, "error", "llm_rejected")
    except Exception as exc:  # noqa: BLE001 - task requires a safe result for graph errors
        logger.error("Worker exception type: %s", type(exc).__name__)
        _finish(s, check, "error", "internal_error")
    assert s.result is not None
    return s.result
