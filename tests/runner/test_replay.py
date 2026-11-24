"""Replay a recorded run against the database, with scripted fake responses (D89)."""

import asyncio
import re
from collections import Counter
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

from cda.agent.checkpoint import open_checkpointer
from cda.agent.prompts import prompt_sha256
from cda.agent.run_graph import RunDeps, run_run
from cda.checks.loader import load_check_set
from cda.db.models import (
    CheckSet,
    Document,
    Evidence,
    Finding,
    LLMCall,
    Pack,
    Run,
    Team,
    TeamMember,
    Unit,
    User,
)
from cda.indexing.embed import HashEmbedder
from cda.llm.cost import ModelTable
from cda.llm.fake import FakeLLM, FakeTurn
from cda.llm.types import Usage
from cda.runner import replay as replay_cli
from cda.runner.replay import ReplayRefused, replay_run

pytestmark = pytest.mark.db


def _alembic_config(pg_url):
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", pg_url.replace("%", "%%"))
    return config


def finding(unit_id):
    return {
        "status": "evidence_found",
        "rationale": "Located",
        "missing_aspects": [],
        "relief_applied": False,
        "relief_id": None,
        "evidence": [{"unit_id": unit_id, "quote": "climate risk", "role": "supports"}],
        "facts": [],
    }


class Scripted:
    """A: one keyword search (G1 searches 'flood', the rest 'climate'), then a finding.

    B and LC: one forced submission. Usage differs per check so replayed tokens and
    cost can be compared with the recording.
    """

    def __init__(self, unit, method):
        self.unit, self.method = str(unit), method
        self.calls = Counter()

    async def complete(self, **kw):
        found = re.search(r"Check (\w+):", kw["messages"][1].content)
        cid = found[1] if found else "LC"
        self.calls[cid] += 1
        usage = Usage(100 + 7 * self.calls[cid] + len(self.calls), 20 + self.calls[cid], 0)
        if self.method == "LC":
            turn = FakeTurn(tool_calls=[("submit_finding", finding("u1"))], usage=usage)
        elif self.method == "B" or self.calls[cid] > 1:
            turn = FakeTurn(tool_calls=[("submit_finding", finding(self.unit))], usage=usage)
        else:
            query = "flood" if cid == "G1" else "climate"
            turn = FakeTurn(tool_calls=[("keyword_search", {"query": query})], usage=usage)
        return await FakeLLM([turn], ModelTable.load()).complete(**kw)


@pytest.fixture
async def env(pg_url):
    await asyncio.to_thread(command.upgrade, _alembic_config(pg_url), "head")
    engine = create_async_engine(pg_url, pool_size=12, max_overflow=8)
    user, team, pack, doc, unit = [uuid4() for _ in range(5)]
    cs = load_check_set()
    async with engine.begin() as c:
        await c.execute(
            insert(User).values(id=user, handle="replayer", role="lead", display_name="R")
        )
        await c.execute(insert(Team).values(id=team, name="Replay team"))
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
        await c.execute(
            pg_insert(CheckSet)
            .values(version=cs.version, yaml_sha256=cs.sha256)
            .on_conflict_do_nothing()
        )

    async def record(method):
        rid = uuid4()
        async with engine.begin() as c:
            await c.execute(
                insert(Run).values(
                    id=rid,
                    pack_id=pack,
                    method=method,
                    model_key="sol",
                    check_set_version=cs.version,
                    prompt_sha256=prompt_sha256(),
                    budget_usd=100,
                    status="queued",
                    idempotency_key=str(rid),
                    created_by=user,
                    settings={"step_cap": 5},
                )
            )
        deps = RunDeps(
            llm=Scripted(unit, method),
            models=ModelTable.load(),
            embedder=HashEmbedder(),
            checkpointer=saver,
        )
        assert await run_run(engine, rid, deps=deps) in ("final", "waiting_review")
        return rid

    async def add_flood_unit():
        async with engine.begin() as c:
            await c.execute(
                insert(Unit).values(
                    document_id=doc,
                    kind="paragraph",
                    seq=1,
                    pdf_page=1,
                    section_path="Physical risk",
                    text="flood exposure of coastal sites",
                    parser_version="test",
                    context_header="report > Physical risk",
                    embedding=HashEmbedder().embed_query("flood exposure of coastal sites"),
                )
            )

    try:
        async with open_checkpointer(pg_url) as saver:
            yield {
                "engine": engine,
                "saver": saver,
                "record": record,
                "add_flood_unit": add_flood_unit,
                "url": pg_url,
            }
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


async def summary(engine, rid):
    async with engine.connect() as c:
        findings = {
            f.check_id: f
            for f in (await c.execute(select(Finding).where(Finding.run_id == rid))).mappings()
        }
        quotes = {
            cid: sorted(
                (
                    await c.execute(
                        select(Evidence.quote, Evidence.verified).where(Evidence.finding_id == f.id)
                    )
                ).all()
            )
            for cid, f in findings.items()
        }
        calls = await c.scalar(
            select(func.count()).select_from(LLMCall).where(LLMCall.run_id == rid)
        )
        run = (await c.execute(select(Run).where(Run.id == rid))).mappings().one()
    return {
        "findings": {
            cid: (
                f.status,
                f.needs_review_reason,
                f.steps,
                f.tokens_in,
                f.tokens_out,
                f.cost_usd,
                quotes[cid],
            )
            for cid, f in findings.items()
        },
        "calls": calls,
        "spent": run.spent_usd,
        "run": run,
    }


async def replay(env, rid):
    return await replay_run(env["engine"], rid, embedder=HashEmbedder(), checkpointer=env["saver"])


@pytest.mark.parametrize("method", ["A", "B", "LC"])
async def test_replay_matches_recording(env, method):
    rid = await env["record"](method)
    source = await summary(env["engine"], rid)
    assert len(source["findings"]) == 15 and source["calls"] > 0
    report = await replay(env, rid)
    replayed = await summary(env["engine"], report.run_id)
    assert report.misses == 0 and report.differences == []
    assert replayed["findings"] == source["findings"]
    assert replayed["calls"] == source["calls"]
    assert replayed["spent"] == source["spent"] > 0
    for key in ("pack_id", "method", "model_key", "budget_usd", "settings", "check_set_version"):
        assert replayed["run"][key] == source["run"][key]
    assert report.status == replayed["run"]["status"]


async def test_changed_tool_result_is_a_miss(env):
    rid = await env["record"]("A")
    # After recording, G1's search ("flood") finds a new unit; other checks search "climate".
    await env["add_flood_unit"]()
    report = await replay(env, rid)
    replayed = await summary(env["engine"], report.run_id)
    source = await summary(env["engine"], rid)
    assert report.misses == 1
    assert replayed["findings"]["G1"][:2] == ("error", "llm_rejected")
    assert report.differences == [("G1", "evidence_found", "error")]
    others = {cid: v for cid, v in replayed["findings"].items() if cid != "G1"}
    assert others == {cid: v for cid, v in source["findings"].items() if cid != "G1"}


async def test_refuses_run_without_responses(env):
    rid = await env["record"]("B")
    async with env["engine"].begin() as c:
        await c.execute(update(LLMCall).where(LLMCall.run_id == rid).values(response=None))
    with pytest.raises(ReplayRefused, match="no recorded responses"):
        await replay(env, rid)


async def test_refuses_different_prompts(env):
    rid = await env["record"]("B")
    async with env["engine"].begin() as c:
        await c.execute(update(Run).where(Run.id == rid).values(prompt_sha256="c" * 64))
    with pytest.raises(ReplayRefused, match="different check set / prompts"):
        await replay(env, rid)


async def test_cli(env, monkeypatch, capsys):
    rid = await env["record"]("A")
    await env["add_flood_unit"]()
    monkeypatch.setenv("CDA_DATABASE_URL", env["url"])
    code = await asyncio.to_thread(replay_cli.main, ["--run", str(rid), "--embedder", "hash"])
    out = capsys.readouterr()
    assert code == 0, out.err
    assert "Misses: 1" in out.out
    assert "G1: evidence_found -> error" in out.out
    async with env["engine"].begin() as c:
        await c.execute(update(LLMCall).where(LLMCall.run_id == rid).values(response=None))
    code = await asyncio.to_thread(replay_cli.main, ["--run", str(rid), "--embedder", "hash"])
    out = capsys.readouterr()
    assert code == 2 and "no recorded responses" in out.err
    code = await asyncio.to_thread(replay_cli.main, ["--run", str(uuid4()), "--embedder", "hash"])
    assert code == 2 and "run not found" in capsys.readouterr().err
