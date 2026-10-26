"""Durable outer run graph, with independent database transactions per check."""

import asyncio
from contextvars import Context
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, TypedDict, cast
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt
from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from cda.agent.hooks import Comparer, EventSink, noop_comparer, noop_sink
from cda.agent.prompts import prompt_sha256
from cda.agent.run_budget import RunBudget
from cda.agent.state import WorkerResult
from cda.agent.store import store_finding
from cda.agent.worker_graph import WorkerDeps, run_worker
from cda.checks.loader import load_check_set
from cda.db.enums import RunStatus
from cda.db.models import Finding, Review, Run
from cda.indexing.embed import Embedder
from cda.llm.client import LLMClient
from cda.llm.cost import ModelTable
from cda.llm.types import LLMAuthError, LLMResponse, Message, ToolSpec
from cda.methods.method_b import run_method_b


@dataclass
class RunDeps:
    llm: LLMClient
    models: ModelTable
    embedder: Embedder | None
    checkpointer: BaseCheckpointSaver[Any]
    seed: int = 20261025
    max_concurrency: int = 5
    comparer: Comparer = noop_comparer
    emit: EventSink = noop_sink
    step_cap: int = 12
    token_cap: int = 60_000
    max_output_tokens: int = 2000
    tool_timeout_s: float = 10.0
    b_top_k: int = 10
    b_min_vector_score: float | None = None
    reraise: tuple[type[BaseException], ...] = ()


class RunState(TypedDict, total=False):
    check_ids: list[str]
    check_id: str


async def _status(engine: AsyncEngine, run_id: UUID) -> RunStatus:
    async with engine.connect() as conn:
        return (await conn.execute(select(Run.status).where(Run.id == run_id))).scalar_one()


async def _fail(engine: AsyncEngine, run_id: UUID, error: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(status="failed", error=error, finished_at=func.now())
        )


class _RunLLM:
    """Mark auth failure immediately, and carry injected crashes through T11's guard."""

    def __init__(self, engine: AsyncEngine, run_id: UUID, deps: RunDeps) -> None:
        self.engine, self.run_id, self.deps = engine, run_id, deps
        self.crash: BaseException | None = None

    async def complete(
        self,
        *,
        messages: list[Message],
        tools: list[ToolSpec],
        model_key: str,
        seed: int | None = None,
        tool_choice: str = "auto",
        max_output_tokens: int = 2000,
    ) -> LLMResponse:
        try:
            return await self.deps.llm.complete(
                messages=messages,
                tools=tools,
                model_key=model_key,
                seed=seed,
                tool_choice=tool_choice,
                max_output_tokens=max_output_tokens,
            )
        except LLMAuthError:
            await _fail(self.engine, self.run_id, "llm_auth")
            raise
        except self.deps.reraise as exc:
            self.crash = exc
            raise


async def run_run(engine: AsyncEngine, run_id: UUID, *, deps: RunDeps) -> RunStatus:
    """Start a run or continue its durable checkpoint until completion or review."""
    cs = load_check_set()
    budget = RunBudget(engine, run_id)

    async def start(state: RunState) -> RunState:
        async with engine.begin() as conn:
            r = (await conn.execute(select(Run).where(Run.id == run_id))).mappings().one()
            if r.status in ("cancelled", "failed", "final", "waiting_review"):
                return {}
            error = (
                "method_not_supported"
                if r.method not in ("A", "B", "C")
                else "check_set_changed"
                if r.check_set_version != cs.version
                else "prompt_changed"
                if r.prompt_sha256 != prompt_sha256()
                else None
            )
            if error:
                await conn.execute(
                    update(Run)
                    .where(Run.id == run_id)
                    .values(status="failed", error=error, finished_at=func.now())
                )
            elif r.status == "queued":
                await conn.execute(
                    update(Run)
                    .where(Run.id == run_id)
                    .values(status="running", started_at=func.now())
                )
        if error:
            await deps.emit(run_id, None, "run_failed", {"error": error})
        elif r.status == "queued":
            await deps.emit(run_id, None, "run_started", {})
        return {}

    async def load_checks(state: RunState) -> RunState:
        async with engine.connect() as conn:
            stored = set(
                (await conn.scalars(select(Finding.check_id).where(Finding.run_id == run_id))).all()
            )
        return {"check_ids": [c.id for c in cs.checks if c.id not in stored]}

    async def check_worker(state: RunState) -> RunState:
        cid = state["check_id"]
        async with engine.begin() as conn:
            if (
                await conn.scalar(
                    select(Finding.id).where(Finding.run_id == run_id, Finding.check_id == cid)
                )
                is not None
            ):
                return {}
            r = (await conn.execute(select(Run).where(Run.id == run_id))).mappings().one()
            if r.status == "failed":
                return {}
            await deps.emit(run_id, cid, "check_started", {})
            if r.status != "running":
                result = WorkerResult(
                    cid,
                    "not_checked",
                    None,
                    "",
                    [],
                    False,
                    "cancelled",
                    [],
                    [],
                    0,
                    0,
                    0,
                    Decimal(0),
                )
            else:
                llm = _RunLLM(engine, run_id, deps)
                try:
                    # T11 state contains a live connection. Do not inherit the outer
                    # graph's checkpoint context into that ephemeral worker graph.
                    worker = run_method_b if r.method == "B" else run_worker
                    options = (
                        {"top_k": deps.b_top_k, "min_vector_score": deps.b_min_vector_score}
                        if r.method == "B"
                        else {}
                    )
                    result = await asyncio.create_task(
                        worker(
                            WorkerDeps(
                                llm=llm,
                                models=deps.models,
                                model_key=r.model_key,
                                seed=deps.seed,
                                conn=conn,
                                user_id=r.created_by,
                                pack_id=r.pack_id,
                                run_id=run_id,
                                method=cast(Literal["A", "B", "C"], r.method),
                                embedder=deps.embedder,
                                budget=budget,
                                step_cap=deps.step_cap,
                                token_cap=deps.token_cap,
                                max_output_tokens=deps.max_output_tokens,
                                tool_timeout_s=deps.tool_timeout_s,
                            ),
                            next(c for c in cs.checks if c.id == cid),
                            cs,
                            **options,
                        ),
                        context=Context(),
                    )
                except LLMAuthError:
                    await _fail(engine, run_id, "llm_auth")
                    raise
                if llm.crash is not None:
                    raise llm.crash
                status = await _status(engine, run_id)
                if status == "failed":
                    return {}
                if result.status == "not_checked" and status == "cancelled":
                    result.needs_review_reason = "cancelled"
            await store_finding(conn, run_id, result)
        await deps.emit(run_id, cid, "check_finished", {"status": result.status})
        return {}

    async def compare(state: RunState) -> RunState:
        if await _status(engine, run_id) == "running":
            async with engine.begin() as conn:
                await deps.comparer(conn, run_id)
        return {}

    async def review_gate(state: RunState) -> RunState:
        while await _status(engine, run_id) in ("running", "waiting_review"):
            async with engine.begin() as conn:
                pending = list(
                    (
                        await conn.scalars(
                            select(Finding.check_id)
                            .where(
                                Finding.run_id == run_id,
                                Finding.status == "needs_review",
                                ~select(Review.id).where(Review.finding_id == Finding.id).exists(),
                            )
                            .order_by(Finding.check_id)
                        )
                    ).all()
                )
                if pending:
                    await conn.execute(
                        update(Run)
                        .where(Run.id == run_id, Run.status.in_(("running", "waiting_review")))
                        .values(status="waiting_review")
                    )
            if not pending:
                break
            await deps.emit(run_id, None, "run_waiting_review", {})
            interrupt({"run_id": str(run_id), "needs_review": pending})
        return {}

    async def finalise(state: RunState) -> RunState:
        async with engine.begin() as conn:
            final = (
                await conn.execute(
                    update(Run)
                    .where(Run.id == run_id)
                    .values(
                        status=case(
                            (Run.status.in_(("cancelled", "failed")), Run.status), else_="final"
                        ),
                        finished_at=func.now(),
                    )
                    .returning(Run.status)
                )
            ).scalar_one()
        if final != "failed":
            await deps.emit(run_id, None, "run_" + str(final), {})
        return {}

    async def after_start(state: RunState) -> str:
        return "load_checks" if await _status(engine, run_id) == "running" else "finalise"

    def fan_out(state: RunState) -> list[Send] | str:
        return [Send("check_worker", {"check_id": cid}) for cid in state["check_ids"]] or "compare"

    graph = StateGraph(RunState)
    for name, node in [
        ("start", start),
        ("load_checks", load_checks),
        ("check_worker", check_worker),
        ("compare", compare),
        ("review_gate", review_gate),
        ("finalise", finalise),
    ]:
        graph.add_node(name, node)
    graph.add_edge(START, "start")
    graph.add_conditional_edges("start", after_start)
    graph.add_conditional_edges("load_checks", fan_out)
    graph.add_edge("check_worker", "compare")
    graph.add_edge("compare", "review_gate")
    graph.add_edge("review_gate", "finalise")
    graph.add_edge("finalise", END)
    compiled = graph.compile(checkpointer=deps.checkpointer)
    config: RunnableConfig = {
        "configurable": {"thread_id": str(run_id)},
        "max_concurrency": max(1, min(5, deps.max_concurrency)),
    }
    try:
        status = await _status(engine, run_id)
        if status in ("final", "failed"):
            return status
        saved = await compiled.aget_state(config)
        if saved.values and status == "running":
            # A crash may have happened before code or check-set files changed.
            await start({})
            if await _status(engine, run_id) == "failed":
                return "failed"
        value: Any = {} if not saved.values else None
        if any(t.interrupts for t in saved.tasks):
            value = Command(resume=True)
        await compiled.ainvoke(value, config=config)
    except deps.reraise:
        raise
    except Exception as exc:  # noqa: BLE001 - persist a safe type name for graph failures
        error = "llm_auth" if isinstance(exc, LLMAuthError) else type(exc).__name__
        await _fail(engine, run_id, error)
        await deps.emit(run_id, None, "run_failed", {"error": error})
    return await _status(engine, run_id)


async def resume_run(engine: AsyncEngine, run_id: UUID, *, deps: RunDeps) -> RunStatus:
    return await run_run(engine, run_id, deps=deps)
