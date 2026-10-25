"""Committed worker integration tests using scripted fake model responses."""

import asyncio
import re
from collections import Counter
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import create_async_engine

from cda.agent.checkpoint import open_checkpointer
from cda.agent.prompts import prompt_sha256
from cda.agent.run_graph import RunDeps
from cda.checks.loader import load_check_set
from cda.config import Settings
from cda.db.models import (
    CheckSet,
    Document,
    Finding,
    Pack,
    Review,
    Run,
    RunEvent,
    Team,
    TeamMember,
    Unit,
    User,
)
from cda.indexing.embed import HashEmbedder
from cda.llm.cost import ModelTable
from cda.llm.fake import FakeLLM, FakeTurn
from cda.llm.types import Usage
from cda.runner.claim import claim_next
from cda.runner.events import EventSink
from cda.runner.lease import hold_lease
from cda.runner.main import serve


def _alembic_config(pg_url):
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", pg_url.replace("%", "%%"))
    return config


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


pytestmark = pytest.mark.db


async def second(env):
    values = dict(await row(env))
    async with env[0].begin() as c:
        values.update(id=uuid4(), idempotency_key=str(uuid4()))
        values.pop("created_at")  # copying it ties the claim order; let now() make it newer
        await c.execute(insert(Run).values(**values))
        return values["id"]


async def test_oldest_and_live_claim(run_env):
    other = await second(run_env)
    assert await claim_next(run_env[0], "one", 90) == run_env[1]
    assert (await row(run_env)).lease_owner == "one"
    assert (await row(run_env)).lease_expires_at is not None
    assert await claim_next(run_env[0], "two", 90) is None
    assert other != run_env[1]


async def test_concurrent_claims(run_env, pg_url):
    await second(run_env)
    engine = create_async_engine(pg_url)
    try:
        results = await asyncio.gather(
            claim_next(run_env[0], "one", 90), claim_next(engine, "two", 90)
        )
        assert sum(r is not None for r in results) == 1
    finally:
        await engine.dispose()


@pytest.mark.parametrize("expiry", [None, text("now() - interval '1 second'")])
async def test_expired_reclaimed(run_env, expiry):
    await change(
        run_env,
        status="running",
        lease_owner="old",
        lease_expires_at=expiry,
    )
    assert await claim_next(run_env[0], "new", 90) == run_env[1]


async def test_live_running_not_claimed(run_env):
    await change(
        run_env,
        status="running",
        lease_owner="old",
        lease_expires_at=text("now() + interval '90 seconds'"),
    )
    assert await claim_next(run_env[0], "new", 90) is None


async def test_review_gate(run_env):
    await change(run_env, status="waiting_review")
    async with run_env[0].begin() as c:
        fid = (
            await c.execute(
                insert(Finding)
                .values(
                    run_id=run_env[1],
                    check_id="G1",
                    status="needs_review",
                    rationale="",
                    steps=0,
                    cost_usd=0,
                )
                .returning(Finding.id)
            )
        ).scalar_one()
    assert await claim_next(run_env[0], "one", 90) is None
    async with run_env[0].begin() as c:
        await c.execute(
            insert(Review).values(
                finding_id=fid, reviewer_id=run_env[4], decision="confirm", note="Reviewed"
            )
        )
    assert await claim_next(run_env[0], "one", 90) == run_env[1]


@pytest.mark.parametrize("status", ["final", "failed", "cancelled"])
async def test_terminal_not_claimed(run_env, status):
    await change(run_env, status=status)
    assert await claim_next(run_env[0], "one", 90) is None


async def test_heartbeat_and_release(run_env):
    await claim_next(run_env[0], "one", 90)
    before = (await row(run_env)).lease_expires_at
    async with hold_lease(run_env[0], run_env[1], "one", 90, 0.1):
        async with asyncio.timeout(5):
            while (await row(run_env)).lease_expires_at <= before:
                await asyncio.sleep(0.05)
    assert (await row(run_env)).lease_owner is None
    assert (await row(run_env)).lease_expires_at is None


async def test_lease_loss_cancels_and_preserves_new_owner(run_env):
    await claim_next(run_env[0], "one", 90)

    async def work():
        async with hold_lease(run_env[0], run_env[1], "one", 90, 0.1) as lease:
            await change(run_env, lease_owner="two")
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                assert lease.lost.is_set()
                raise

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(work(), 6)
    assert (await row(run_env)).lease_owner == "two"


async def test_events_notify(run_env):
    raw = await run_env[0].raw_connection()
    listener = raw.driver_connection
    messages = asyncio.Queue()
    channel = "run_" + run_env[1].hex

    def callback(*args):
        messages.put_nowait(args[-1])

    await listener.add_listener(channel, callback)
    try:
        sink = EventSink(run_env[0])
        await sink.emit(run_env[1], None, "run_started", {})
        await sink.emit(run_env[1], "G1", "check_started", {})
        ids = [int(await asyncio.wait_for(messages.get(), 5)) for _ in range(2)]
        assert ids[0] < ids[1]
        async with run_env[0].connect() as c:
            assert list((await c.scalars(select(RunEvent.id).order_by(RunEvent.id))).all()) == ids
    finally:
        await listener.remove_listener(channel, callback)
        raw.close()


async def run_server(env, pg_url, run_fn=None, poll=0.05):
    stop = asyncio.Event()
    deps = replace(env[2], emit=EventSink(env[0]).emit)
    settings = Settings(database_url=pg_url, poll_seconds=poll)
    kwargs = {} if run_fn is None else {"run_fn": run_fn}
    task = asyncio.create_task(
        serve(env[0], settings, deps_factory=lambda *_: deps, stop=stop, **kwargs)
    )
    return stop, task


async def test_serve_serial_and_stop(run_env, pg_url):
    other = await second(run_env)
    seen = []

    async def run(engine, rid, *, deps):
        seen.append(rid)
        async with engine.begin() as c:
            await c.execute(update(Run).where(Run.id == rid).values(status="final"))
        return "final"

    stop, task = await run_server(run_env, pg_url, run)
    try:
        async with asyncio.timeout(10):
            while len(seen) < 2:
                await asyncio.sleep(0.02)
        assert seen == [run_env[1], other]
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)


async def test_serve_exception_continues(run_env, pg_url, caplog):
    await second(run_env)
    seen = []

    async def run(engine, rid, *, deps):
        seen.append(rid)
        async with engine.begin() as c:
            await c.execute(update(Run).where(Run.id == rid).values(status="failed"))
        if len(seen) == 1:
            raise ValueError("private secret")
        return "failed"

    stop, task = await run_server(run_env, pg_url, run)
    try:
        async with asyncio.timeout(10):
            while len(seen) < 2:
                await asyncio.sleep(0.02)
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
    assert (await row(run_env)).lease_owner is None
    assert "private secret" not in caplog.text


async def test_serve_real_graph(run_env, pg_url):
    stop, task = await run_server(run_env, pg_url)
    try:
        async with asyncio.timeout(30):
            while True:
                async with run_env[0].connect() as c:
                    finished = await c.scalar(
                        select(RunEvent.id).where(RunEvent.type == "run_final")
                    )
                if finished is not None:
                    break
                await asyncio.sleep(0.05)
        assert (await row(run_env)).status == "final"
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
    async with run_env[0].connect() as c:
        events = list((await c.scalars(select(RunEvent.type).order_by(RunEvent.id))).all())
    assert events[0] == "run_started"
    assert events[-1] == "run_final"


async def test_notify_wakes_idle_worker(run_env, pg_url, monkeypatch):
    await change(run_env, status="final")
    called = asyncio.Event()
    idle = asyncio.Event()

    async def observed_claim(*args):
        result = await claim_next(*args)
        if result is None:
            idle.set()
        return result

    monkeypatch.setattr("cda.runner.main.claim_next", observed_claim)

    async def run(engine, rid, *, deps):
        await change(run_env, status="final")
        called.set()
        return "final"

    stop, task = await run_server(run_env, pg_url, run, poll=60)
    try:
        await asyncio.wait_for(idle.wait(), 10)
        await change(run_env, status="queued")
        async with run_env[0].begin() as c:
            await c.execute(text("SELECT pg_notify('cda_runs', '')"))
        await asyncio.wait_for(called.wait(), 5)
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)


async def test_shutdown_releases_active_lease(run_env, pg_url):
    started = asyncio.Event()

    async def run(engine, rid, *, deps):
        started.set()
        await asyncio.Event().wait()

    stop, task = await run_server(run_env, pg_url, run)
    try:
        await asyncio.wait_for(started.wait(), 10)
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
    assert (await row(run_env)).lease_owner is None
    assert (await row(run_env)).lease_expires_at is None
