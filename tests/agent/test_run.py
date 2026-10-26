"""Committed, multi-connection integration tests for T12 (no provider calls)."""

import asyncio
import re
from collections import Counter
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from alembic import command
from conftest import _alembic_config
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import create_async_engine

from cda.agent.checkpoint import open_checkpointer
from cda.agent.prompts import prompt_sha256
from cda.agent.run_budget import RunBudget
from cda.agent.run_graph import RunDeps, resume_run, run_run
from cda.checks.loader import load_check_set
from cda.db.models import (
    CheckSet,
    Document,
    Finding,
    LLMCall,
    Pack,
    Review,
    Run,
    Team,
    TeamMember,
    Unit,
    User,
)
from cda.indexing.embed import HashEmbedder
from cda.llm.cost import ModelTable
from cda.llm.fake import FakeLLM, FakeTurn
from cda.llm.types import LLMAuthError, Usage

pytestmark = pytest.mark.db


class SimulatedCrash(Exception):
    pass


class CheckLLM:
    def __init__(self, unit):
        self.unit = unit
        self.calls = Counter()
        self.active = self.peak = 0
        self.gate = asyncio.Event()
        self.gate.set()
        self.errors = {}
        self.review = set()
        self.usage = Usage(100, 20, 0)

    async def complete(self, **kw):
        cid = re.search(r"Check (\w+):", kw["messages"][1].content)[1]
        self.calls[cid] += 1
        self.active += 1
        self.peak = max(self.active, self.peak)
        try:
            await self.gate.wait()
            await asyncio.sleep(0.01)
            if cid in self.errors:
                raise self.errors[cid]
            if self.calls[cid] == 1:
                turn = FakeTurn(tool_calls=[("keyword_search", {"query": "climate"})])
            else:
                turn = FakeTurn(
                    tool_calls=[
                        (
                            "submit_finding",
                            {
                                "status": "no_evidence_found"
                                if cid in self.review
                                else "evidence_found",
                                "rationale": "Located",
                                "missing_aspects": [],
                                "relief_applied": False,
                                "relief_id": None,
                                "evidence": []
                                if cid in self.review
                                else [
                                    {
                                        "unit_id": str(self.unit),
                                        "quote": "climate risk",
                                        "role": "supports",
                                    }
                                ],
                                "facts": [],
                            },
                        )
                    ]
                )
            turn = FakeTurn(tool_calls=turn.tool_calls, usage=self.usage)
            return await FakeLLM([turn], ModelTable.load()).complete(**kw)
        finally:
            self.active -= 1


@pytest.fixture
async def run_env(pg_url):
    await asyncio.to_thread(command.upgrade, _alembic_config(pg_url), "head")
    engine = create_async_engine(pg_url, pool_size=12, max_overflow=8)
    user, team, pack, doc, unit, rid = [uuid4() for _ in range(6)]
    cs = load_check_set()
    async with engine.begin() as c:
        await c.execute(
            insert(User).values(id=user, handle="runner", role="lead", display_name="R")
        )
        await c.execute(insert(Team).values(id=team, name="Run team"))
        await c.execute(insert(TeamMember).values(team_id=team, user_id=user))
        await c.execute(
            insert(Pack).values(
                id=pack, company="Test", reporting_year=2026, team_id=team, created_by=user
            )
        )
        await c.execute(
            insert(Document).values(
                id=doc,
                pack_id=pack,
                kind="annual",
                filename="report.pdf",
                sha256="a" * 64,
                page_count=1,
                parser_version="test",
            )
        )
        await c.execute(
            insert(Unit).values(
                id=unit,
                document_id=doc,
                kind="paragraph",
                seq=0,
                pdf_page=1,
                section_path="Climate",
                text="climate risk",
                parser_version="test",
                context_header="report > Climate",
                embedding=HashEmbedder().embed_query("climate risk"),
            )
        )
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        await c.execute(
            pg_insert(CheckSet)
            .values(version=cs.version, yaml_sha256=cs.sha256)
            .on_conflict_do_nothing()
        )
        await c.execute(
            insert(Run).values(
                id=rid,
                pack_id=pack,
                method="A",
                model_key="sol",
                check_set_version=cs.version,
                prompt_sha256=prompt_sha256(),
                budget_usd=100,
                status="queued",
                idempotency_key=str(rid),
                created_by=user,
            )
        )
    try:
        async with open_checkpointer(pg_url) as saver:
            llm = CheckLLM(unit)
            deps = RunDeps(
                llm=llm, models=ModelTable.load(), embedder=HashEmbedder(), checkpointer=saver
            )
            yield engine, rid, deps, llm, user
    finally:
        async with engine.begin() as c:
            await c.execute(text("TRUNCATE users, teams, check_sets CASCADE"))
            await c.execute(
                text(
                    "TRUNCATE langgraph.checkpoints, langgraph.checkpoint_blobs, "
                    "langgraph.checkpoint_writes"
                )
            )
        await engine.dispose()


async def row(env):
    async with env[0].connect() as c:
        return (await c.execute(select(Run).where(Run.id == env[1]))).mappings().one()


async def change(env, **values):
    async with env[0].begin() as c:
        await c.execute(update(Run).where(Run.id == env[1]).values(**values))


async def findings(env):
    async with env[0].connect() as c:
        return (await c.execute(select(Finding).where(Finding.run_id == env[1]))).mappings().all()


async def execute(env):
    return await run_run(env[0], env[1], deps=env[2])


async def test_queued_finishes(run_env):
    assert await execute(run_env) == "final"
    r = await row(run_env)
    fs = await findings(run_env)
    assert len(fs) == len({f.check_id for f in fs}) == 15
    assert r.started_at and r.finished_at
    async with run_env[0].connect() as c:
        assert r.spent_usd == await c.scalar(select(func.sum(LLMCall.cost_usd)))


@pytest.mark.parametrize("method", ["LC"])
async def test_unsupported_method(run_env, method):
    await change(run_env, method=method)
    assert await execute(run_env) == "failed"
    assert (await row(run_env)).error == "method_not_supported"
    assert not run_env[3].calls


async def test_changed_check_set(run_env):
    async with run_env[0].begin() as c:
        await c.execute(insert(CheckSet).values(version="old", yaml_sha256="b" * 64))
    await change(run_env, check_set_version="old")
    assert await execute(run_env) == "failed"
    assert (await row(run_env)).error == "check_set_changed"
    assert not run_env[3].calls


async def test_changed_prompt(run_env):
    await change(run_env, prompt_sha256="b" * 64)
    assert await execute(run_env) == "failed"
    assert (await row(run_env)).error == "prompt_changed"
    assert not run_env[3].calls


async def test_cancelled_before_start(run_env):
    await change(run_env, status="cancelled")
    assert await execute(run_env) == "cancelled"
    assert (await row(run_env)).finished_at
    assert not run_env[3].calls


async def test_parallel_limit(run_env):
    llm = run_env[3]
    llm.gate.clear()
    task = asyncio.create_task(execute(run_env))
    try:
        async with asyncio.timeout(15):
            while llm.active < 5:
                await asyncio.sleep(0.01)
        llm.gate.set()
        assert await task == "final"
        assert 2 <= llm.peak <= 5
        assert len({f.check_id for f in await findings(run_env)}) == 15
    finally:
        llm.gate.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_serial_limit(run_env):
    run_env[2].max_concurrency = 1
    assert await execute(run_env) == "final"
    assert run_env[3].peak == 1


async def test_budget_exhausted(run_env):
    run_env[2].max_concurrency = 1
    run_env[3].usage = Usage(100, 2000, 0)
    await change(run_env, budget_usd=Decimal("0.15"))
    assert await execute(run_env) == "final"
    fs = await findings(run_env)
    assert any(f.status == "evidence_found" for f in fs)
    assert any(f.needs_review_reason == "budget_exhausted" for f in fs)
    r = await row(run_env)
    assert r.spent_usd <= r.budget_usd


async def test_atomic_reservations(run_env):
    await change(run_env, status="running", budget_usd=1, spent_usd=Decimal("0.8"))
    budget = RunBudget(run_env[0], run_env[1])
    assert sum(await asyncio.gather(*[budget.reserve(Decimal("0.1")) for _ in range(5)])) == 2
    assert (await row(run_env)).spent_usd == 1


async def test_settle_refunds_and_floors(run_env):
    await change(run_env, status="running")
    budget = RunBudget(run_env[0], run_env[1])
    assert await budget.reserve(Decimal(1))
    await budget.settle(Decimal(1), Decimal("0.2"))
    assert (await row(run_env)).spent_usd == Decimal("0.2")
    await budget.settle(Decimal(1), Decimal(0))
    assert (await row(run_env)).spent_usd == 0


async def test_cancelled_mid_run(run_env):
    run_env[2].max_concurrency = 1

    async def emit(rid, cid, kind, payload):
        if kind == "check_finished":
            await change(run_env, status="cancelled")

    run_env[2].emit = emit
    assert await execute(run_env) == "cancelled"
    fs = await findings(run_env)
    assert any(f.needs_review_reason == "cancelled" for f in fs)


async def test_auth_stops_run(run_env):
    run_env[2].max_concurrency = 1
    run_env[3].errors["G1"] = LLMAuthError()
    assert await execute(run_env) == "failed"
    assert (await row(run_env)).error == "llm_auth"
    assert set(run_env[3].calls) == {"G1"}


async def test_crash_resume(run_env):
    run_env[2].max_concurrency = 1
    run_env[2].reraise = (SimulatedCrash,)
    run_env[3].errors["S1"] = SimulatedCrash()
    with pytest.raises(SimulatedCrash):
        await execute(run_env)
    stored = {f.check_id for f in await findings(run_env)}
    assert len(stored) == 2
    counts = run_env[3].calls.copy()
    run_env[3].errors.clear()
    assert await execute(run_env) == "final"
    assert len(await findings(run_env)) == 15
    assert all(run_env[3].calls[c] == counts[c] for c in stored)


async def test_skips_existing(run_env):
    async with run_env[0].begin() as c:
        for cid in ["G1", "G2"]:
            await c.execute(
                insert(Finding).values(
                    run_id=run_env[1],
                    check_id=cid,
                    status="not_checked",
                    rationale="Existing",
                    steps=0,
                    cost_usd=0,
                )
            )
    assert await execute(run_env) == "final"
    assert len(run_env[3].calls) == 13
    assert len(await findings(run_env)) == 15


async def test_review_reinterrupts(run_env):
    run_env[3].review.add("G1")
    assert await execute(run_env) == "waiting_review"
    assert await resume_run(run_env[0], run_env[1], deps=run_env[2]) == "waiting_review"


async def test_review_resumes(run_env):
    run_env[3].review.add("G1")
    assert await execute(run_env) == "waiting_review"
    async with run_env[0].begin() as c:
        fid = await c.scalar(select(Finding.id).where(Finding.check_id == "G1"))
        await c.execute(
            insert(Review).values(
                finding_id=fid, reviewer_id=run_env[4], decision="confirm", note="Confirmed"
            )
        )
    assert await resume_run(run_env[0], run_env[1], deps=run_env[2]) == "final"


async def test_no_review_no_interrupt(run_env):
    assert await execute(run_env) == "final"
    saved = await run_env[2].checkpointer.aget_tuple(
        {"configurable": {"thread_id": str(run_env[1])}}
    )
    assert saved is not None
    assert not any(v[1] == "__interrupt__" for v in saved.pending_writes)


async def test_comparer_after_workers(run_env):
    async def compare(c, rid):
        assert await c.scalar(select(func.count()).select_from(Finding)) == 15

    run_env[2].comparer = AsyncMock(side_effect=compare)
    assert await execute(run_env) == "final"
    run_env[2].comparer.assert_awaited_once()


@pytest.mark.parametrize("review", [False, True])
async def test_event_order(run_env, review):
    events = []

    async def emit(rid, cid, kind, payload):
        events.append((cid, kind, payload))

    run_env[2].emit = emit
    if review:
        run_env[3].review.add("G1")
    await execute(run_env)
    assert events[0][1] == "run_started"
    assert events[-1][1] == ("run_waiting_review" if review else "run_final")
    for cid in run_env[3].calls:
        kinds = [kind for c, kind, p in events if c == cid]
        assert kinds == ["check_started", "check_finished"]
        assert next(p for c, k, p in events if c == cid and k == "check_finished")["status"]


async def test_compare_exception_safe(run_env):
    run_env[2].comparer = AsyncMock(side_effect=ValueError("private message"))
    assert await execute(run_env) == "failed"
    r = await row(run_env)
    assert r.error == "ValueError" and r.finished_at


async def test_checkpointer_setup_twice(run_env, pg_url):
    for _ in range(2):
        async with open_checkpointer(pg_url) as saver:
            assert saver is not None


async def test_checkpointer_tables_use_separate_schema(run_env, pg_url):
    async with open_checkpointer(pg_url), run_env[0].connect() as c:
        tables = (
            await c.execute(
                text(
                    "SELECT schemaname, tablename FROM pg_tables "
                    "WHERE tablename IN ('checkpoints', 'checkpoint_blobs', "
                    "'checkpoint_writes', 'checkpoint_migrations')"
                )
            )
        ).all()
    assert set(tables) == {
        ("langgraph", name)
        for name in (
            "checkpoints",
            "checkpoint_blobs",
            "checkpoint_writes",
            "checkpoint_migrations",
        )
    }
