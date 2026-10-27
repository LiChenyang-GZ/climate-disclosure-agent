"""Shared one-call, verification, and final-status path for B and LC."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import UUID

from cda.agent.finding import SubmitFinding
from cda.agent.state import WorkerResult, WorkerState
from cda.agent.worker_graph import WorkerDeps, _finish
from cda.checks.models import Check
from cda.llm.calls import record_call
from cda.llm.client import parse_tool_call
from cda.llm.cost import cost_usd, estimate_tokens, max_cost_usd
from cda.llm.types import LLMAuthError, Message, ToolArgsError, ToolCall, ToolSpec
from cda.tools.context import ToolContext
from cda.verify.evidence import EvidenceItem, verify_evidence


async def run_single_call(
    deps: WorkerDeps,
    check: Check,
    messages: list[Message],
    *,
    context_ids: set[str],
    search_complete: bool,
    aliases: dict[str, UUID] | None = None,
    verifier: Callable[..., Awaitable[Any]] = verify_evidence,
    recorder: Callable[..., Awaitable[Any]] = record_call,
) -> WorkerResult:
    """Make one forced submission call and apply shared verification/status rules."""
    state = WorkerState(
        ToolContext(deps.conn, deps.user_id, deps.pack_id, deps.embedder, deps.tool_timeout_s)
    )

    def finish(status: str, reason: str | None = None, *, high: bool | None = None) -> WorkerResult:
        _finish(state, check, status, reason, None if high is None else "high" if high else "low")
        assert state.result is not None
        return state.result

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
    except ToolArgsError as exc:
        await deps.budget.settle(estimate, estimate)
        state.cost = estimate
        state.error_detail = f"Invalid tool arguments: {exc}"[:1000]
        return finish("error", "invalid_output")
    except LLMAuthError:
        await deps.budget.settle(estimate, Decimal(0))
        raise
    except Exception as exc:  # noqa: BLE001 - single-call failures are invalid_output
        await deps.budget.settle(estimate, Decimal(0))
        # Type only: request errors can echo prompt or response text.
        state.error_detail = f"Model request failed: {type(exc).__name__}"
        return finish("error", "invalid_output")
    state.cost = cost_usd(price, response.usage)
    state.tokens_in, state.tokens_out = response.usage.tokens_in, response.usage.tokens_out
    await deps.budget.settle(estimate, state.cost)
    await recorder(
        deps.conn, run_id=deps.run_id, check_id=check.id, step=1, response=response, cost=state.cost
    )
    calls = response.message.tool_calls
    if len(calls) != 1 or calls[0].name != "submit_finding":
        names = ", ".join(c.name[:100] for c in calls) or "none"
        state.error_detail = f"Expected one submit_finding call, got: {names}"
        return finish("error", "invalid_output")
    bad_aliases: set[int] = set()
    call = calls[0]
    if aliases is not None:
        call, bad_aliases = _translate_aliases(call, aliases)
    try:
        parsed = parse_tool_call(call, tools)
        assert isinstance(parsed, SubmitFinding)
        parsed.drop_uncited_facts()
        parsed.check_rules(check)
    except (ToolArgsError, ValueError) as exc:
        state.error_detail = f"submit_finding rejected: {exc}"[:1000]
        return finish("error", "invalid_output")
    state.submission = parsed
    state.evidence = await verifier(
        deps.conn,
        user_id=deps.user_id,
        pack_id=deps.pack_id,
        items=[EvidenceItem(UUID(e.unit_id), e.quote, e.role) for e in parsed.evidence],
        timeout_s=deps.tool_timeout_s,
    )
    # Keep the verifier's unit_not_found: store_finding skips those items. Relabelling a
    # non-existent unit as not_in_context made it insert evidence with a dangling unit id.
    state.evidence = [
        replace(e, ok=False, reason="unit_not_found")
        if i in bad_aliases or e.reason == "unit_not_found"
        else replace(e, ok=False, reason="not_in_context")
        if str(e.unit_id) not in context_ids
        else e
        for i, e in enumerate(state.evidence)
    ]
    if any(not e.ok for e in state.evidence):
        return finish("needs_review", "quote_verification_failed")
    if parsed.status == "no_evidence_found" and not search_complete:
        return finish("needs_review", "insufficient_search")
    high = (
        parsed.status != "no_evidence_found"
        and any(e.ok for e in state.evidence)
        and search_complete
    )
    return finish(parsed.status, high=high)


def _translate_aliases(call: ToolCall, aliases: dict[str, UUID]) -> tuple[ToolCall, set[int]]:
    """Translate LC aliases to real IDs; keep invalid references detectable after parsing."""
    try:
        args = json.loads(call.arguments)
    except (TypeError, ValueError):
        return call, set()
    if not isinstance(args, dict):
        return call, set()
    bad: set[int] = set()
    evidence = args.get("evidence")
    if isinstance(evidence, list):
        for index, entry in enumerate(evidence):
            if not isinstance(entry, dict) or "unit_id" not in entry:
                continue
            alias = entry["unit_id"]
            mapped = aliases.get(alias) if isinstance(alias, str) else None
            if mapped is None:
                entry["unit_id"] = str(UUID(int=0))
                bad.add(index)
            else:
                entry["unit_id"] = str(mapped)
    facts = args.get("facts")
    if isinstance(facts, list):
        translated_facts = []
        for entry in facts:
            if not isinstance(entry, dict) or "unit_id" not in entry:
                translated_facts.append(entry)
                continue
            alias = entry["unit_id"]
            mapped = aliases.get(alias) if isinstance(alias, str) else None
            if mapped is not None:
                entry["unit_id"] = str(mapped)
                translated_facts.append(entry)
        args["facts"] = translated_facts
    return replace(call, arguments=json.dumps(args, ensure_ascii=False)), bad
