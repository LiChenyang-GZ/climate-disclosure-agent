import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4, uuid5

import pytest
from sqlalchemy import insert, select, update

from cda.config import Settings
from cda.db.models import Evidence, Finding, LLMCall, Run, RunEvent
from cda.tracing.export import LangfuseAdapter, export_run
from cda.tracing.settings import TracingSettings


class FakeClient:
    def __init__(self):
        self.calls = []

    def trace(self, **kwargs):
        self.calls.append(("trace", kwargs))

    def span(self, **kwargs):
        self.calls.append(("span", kwargs))

    def generation(self, **kwargs):
        self.calls.append(("generation", kwargs))

    def flush(self):
        self.calls.append(("flush", {}))


@pytest.mark.parametrize(
    "missing", ["LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", None]
)
def test_settings(monkeypatch, caplog, missing):
    for key, value in [
        ("LANGFUSE_HOST", "http://localhost:3000"),
        ("LANGFUSE_PUBLIC_KEY", "public-test"),
        ("LANGFUSE_SECRET_KEY", "private-test"),
    ]:
        monkeypatch.setenv(key, value)
    if missing:
        monkeypatch.delenv(missing)
    settings = TracingSettings()
    assert settings.enabled == (missing is None)
    assert "public-test" not in repr(settings) + caplog.text
    assert "private-test" not in repr(settings) + caplog.text


@pytest.fixture
async def exported(run_env):
    engine, rid, _, llm, _ = run_env
    now = datetime(2026, 1, 1, tzinfo=UTC)
    fids = [uuid4(), uuid4()]
    calls = [uuid4() for _ in range(3)]
    async with engine.begin() as c:
        await c.execute(
            update(Run)
            .where(Run.id == rid)
            .values(
                status="final",
                started_at=now,
                finished_at=now + timedelta(seconds=5),
                spent_usd=0.3,
            )
        )
        for i, cid in enumerate(["G1", "G2"]):
            await c.execute(
                insert(Finding).values(
                    id=fids[i],
                    run_id=rid,
                    check_id=cid,
                    status="evidence_found",
                    confidence="high",
                    rationale="Model rationale",
                    steps=2,
                    tokens_in=100,
                    tokens_out=20,
                    cost_usd=0.1,
                )
            )
            await c.execute(
                insert(Evidence).values(
                    finding_id=fids[i], unit_id=llm.unit, quote="SECRET QUOTE", role="supports"
                )
            )
            for kind, seconds, payload in [
                ("check_started", 0, {}),
                ("check_finished", 4, {"status": "evidence_found"}),
                (
                    "tool_called",
                    2,
                    {
                        "tool": "keyword_search",
                        "query": "climate",
                        "part_ids": ["part1"],
                        "category": "ok",
                        "hits": 2,
                        "prompt": "SECRET PROMPT",
                        "quote": "SECRET QUOTE",
                    },
                ),
            ]:
                await c.execute(
                    insert(RunEvent).values(
                        run_id=rid,
                        check_id=cid,
                        type=kind,
                        payload=payload,
                        created_at=now + timedelta(seconds=seconds),
                    )
                )
        for i, call in enumerate(calls):
            await c.execute(
                insert(LLMCall).values(
                    id=call,
                    run_id=rid,
                    check_id="G1" if i < 2 else "G2",
                    step=i,
                    model="gpt-6.1-sol",
                    prompt_sha256="a" * 64,
                    tools_sha256="b" * 64,
                    tokens_in=100,
                    tokens_out=20,
                    tokens_cached=50,
                    cost_usd=0.1,
                    latency_ms=100,
                    created_at=now + timedelta(seconds=3),
                )
            )
        await c.execute(
            insert(RunEvent).values(
                run_id=rid,
                type="run_final",
                payload={"prompt": "SECRET PROMPT"},
                created_at=now + timedelta(seconds=5),
            )
        )
    return engine, rid, now, fids, calls


async def test_export_structure(exported):
    engine, rid, now, fids, ids = exported
    client = FakeClient()
    summary = await export_run(engine, rid, client)
    trace = [p for k, p in client.calls if k == "trace"]
    assert len(trace) == 1
    assert trace[0]["id"] == str(rid)
    assert trace[0]["name"] == "run A Test 2026"
    assert trace[0]["metadata"]["status"] == "final"
    checks = [p for k, p in client.calls if k == "span" and p["id"] in map(str, fids)]
    assert len(checks) == summary.checks == 2
    for span in checks:
        assert span["start_time"] == now
        assert span["end_time"] == now + timedelta(seconds=4)
        assert span["output"]["status"] == "evidence_found"
        assert span["output"]["tokens_in"] == 100
    generations = [p for k, p in client.calls if k == "generation"]
    assert len(generations) == summary.generations == 3
    for i, gen in enumerate(generations):
        assert gen["id"] == str(ids[i])
        assert gen["parent_observation_id"] == str(fids[0 if i < 2 else 1])
        assert gen["usage"] == {"input": 100, "output": 20, "total_cost": 0.1, "unit": "TOKENS"}
        assert gen["metadata"]["tokens_cached"] == 50
        assert gen["metadata"]["reasoning_effort"] == "low"
        assert gen["end_time"] - gen["start_time"] == timedelta(milliseconds=100)
    tools = [p for k, p in client.calls if k == "span" and p["name"] == "keyword_search"]
    assert len(tools) == summary.tools == 2
    assert tools[0]["input"] == {"query": "climate", "part_ids": ["part1"]}
    assert tools[0]["output"] == {"category": "ok", "hits": 2}


async def test_cancelled_without_finding(run_env):
    engine, rid, *_ = run_env
    async with engine.begin() as c:
        await c.execute(update(Run).where(Run.id == rid).values(status="cancelled"))
        await c.execute(insert(RunEvent).values(run_id=rid, check_id="G1", type="check_started"))
    client = FakeClient()
    await export_run(engine, rid, client)
    span = next(p for k, p in client.calls if k == "span")
    assert span["id"] == str(uuid5(rid, "G1"))
    assert span["end_time"] is None


async def test_idempotency(exported):
    engine, rid, *_ = exported
    first, second_client = FakeClient(), FakeClient()
    await export_run(engine, rid, first)
    await export_run(engine, rid, second_client)
    assert first.calls == second_client.calls


async def test_no_quotes_or_prompts(exported):
    engine, rid, *_ = exported
    client = FakeClient()
    await export_run(engine, rid, client)
    assert "SECRET QUOTE" not in repr(client.calls)
    assert "SECRET PROMPT" not in repr(client.calls)


def test_adapter():
    sdk = Mock()
    adapter = LangfuseAdapter(sdk)
    for method in ["trace", "span", "generation"]:
        getattr(adapter, method)(id="test", name="name")
        getattr(sdk, method).assert_called_once_with(id="test", name="name")
    adapter.flush()
    sdk.flush.assert_called_once_with()


@pytest.mark.parametrize("enabled,raises", [(True, False), (True, True), (False, False)])
async def test_worker_hook(run_env, pg_url, monkeypatch, caplog, enabled, raises):
    from cda.runner import main as worker

    for key in ["LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"]:
        if enabled:
            monkeypatch.setenv(key, "test")
        else:
            monkeypatch.delenv(key, raising=False)
    other = await second(run_env)
    seen, exports = [], []
    client = FakeClient()
    monkeypatch.setattr(worker, "make_trace_client", lambda _: client)

    async def export(engine, rid, trace_client):
        exports.append(rid)
        assert rid in seen
        if raises:
            raise ValueError("private-test")

    monkeypatch.setattr(worker, "export_run", export)

    async def run(engine, rid, *, deps):
        async with engine.begin() as c:
            await c.execute(update(Run).where(Run.id == rid).values(status="failed"))
        seen.append(rid)
        return "failed"

    stop, task = await run_server(run_env, pg_url, run)
    try:
        async with asyncio.timeout(10):
            while len(seen) < 2 or (enabled and len(exports) < 2):
                await asyncio.sleep(0.02)
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
    assert seen == [run_env[1], other]
    assert exports == (seen if enabled else [])
    assert ("trace export failed ValueError" in caplog.text) == raises
    assert "private-test" not in caplog.text
    if enabled and not raises:
        assert client.calls == [("flush", {}), ("flush", {})]


async def second(env):
    async with env[0].begin() as c:
        values = dict((await c.execute(select(Run).where(Run.id == env[1]))).mappings().one())
        values.update(id=uuid4(), idempotency_key=str(uuid4()))
        values.pop("created_at")
        await c.execute(insert(Run).values(**values))
        return values["id"]


async def run_server(env, pg_url, run_fn):
    from cda.runner.main import serve

    stop = asyncio.Event()
    task = asyncio.create_task(
        serve(
            env[0],
            Settings(database_url=pg_url, poll_seconds=0.02),
            deps_factory=lambda *_: env[2],
            run_fn=run_fn,
            stop=stop,
        )
    )
    return stop, task
