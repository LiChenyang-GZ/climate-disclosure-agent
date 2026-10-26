"""Real database evaluation flows; all completions are local scripted fakes."""

import asyncio
import re
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

from cda.agent.checkpoint import open_checkpointer
from cda.agent.prompts import prompt_sha256
from cda.agent.run_graph import RunDeps, run_run
from cda.checks.loader import load_check_set
from cda.db.models import CheckSet, Document, DocumentPart, Pack, Run, RunEvent, Unit
from cda.eval.answer_key import load_answer_key
from cda.eval.pack import ingest_pack
from cda.eval.retrieval_eval import evaluate_retrieval
from cda.eval.run_eval import score_stored_run
from cda.indexing.embed import HashEmbedder
from cda.llm.cost import ModelTable
from cda.llm.fake import FakeLLM, FakeTurn
from cda.parsing.units import normalize_text
from cda.runner.events import EventSink
from cda.tools.context import ToolContext

pytestmark = pytest.mark.db
PACK = Path("data/synthetic/wattlebrook")


@pytest.fixture
async def env(pg_url):
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", pg_url.replace("%", "%%"))
    await asyncio.to_thread(command.upgrade, config, "head")
    engine = create_async_engine(pg_url, pool_size=12, max_overflow=8)
    handle = "eval-" + uuid4().hex
    pid = await ingest_pack(
        engine, PACK, embedder=HashEmbedder(), team_handle=handle, user_handle=handle
    )
    async with engine.connect() as c:
        uid = await c.scalar(select(Pack.created_by).where(Pack.id == pid))
    try:
        yield engine, pid, uid, handle, pg_url
    finally:
        # Same cleanup as the other committing fixtures, so later tests see empty tables.
        async with engine.begin() as c:
            await c.execute(text("TRUNCATE users, teams, check_sets CASCADE"))
            if await c.scalar(text("SELECT to_regclass('langgraph.checkpoints')")):
                await c.execute(
                    text(
                        "TRUNCATE langgraph.checkpoints, langgraph.checkpoint_blobs, "
                        "langgraph.checkpoint_writes"
                    )
                )
        await engine.dispose()


async def test_ingest_idempotent(env):
    engine, pid, _uid, handle, _ = env
    async with engine.connect() as c:
        assert (
            await c.scalar(
                select(func.count()).select_from(Document).where(Document.pack_id == pid)
            )
            == 2
        )
        assert (
            await c.scalar(
                select(func.count())
                .select_from(DocumentPart)
                .join(Document)
                .where(Document.pack_id == pid, DocumentPart.kind == "sustainability")
            )
            == 1
        )
        assert (
            await c.scalar(
                select(func.count())
                .select_from(Unit)
                .join(Document)
                .where(Document.pack_id == pid, Unit.embedding.is_not(None))
            )
            > 0
        )
    assert (
        await ingest_pack(
            engine, PACK, embedder=HashEmbedder(), team_handle=handle, user_handle=handle
        )
        == pid
    )


async def test_retrieval_keyword(env, monkeypatch):
    async def fail(*args, **kwargs):
        raise AssertionError("model must not be called")

    monkeypatch.setattr(FakeLLM, "complete", fail)
    engine, pid, uid, _, _ = env
    async with engine.begin() as c:
        result = await evaluate_retrieval(
            ToolContext(c, uid, pid, HashEmbedder()),
            load_answer_key(PACK / "answer_key.yaml"),
            ("keyword",),
        )
    overall = result["modes"]["keyword"]["overall"]
    assert all(0 <= v <= 1 for v in overall.values())
    assert overall["Hit@20"] >= overall["Hit@1"]


class Oracle:
    def __init__(self, injection=False):
        self.key = load_answer_key(PACK / "answer_key.yaml")
        self.injection = injection

    async def complete(self, **kw):
        text = kw["messages"][-1].content
        cid = re.search(r"Check (\w+):", text)[1]
        cell = next(c for c in self.key.cells if c.check_id == cid)
        targets = [e for e in cell.evidence if e.role == "primary"]
        if self.injection:
            targets = next(i for i in self.key.planted_issues if i.type == "injection").locations
        pack = kw["messages"][1].content
        blocks = re.findall(
            r"\[(u\d+)\] ([^\n]+)\n(.*?)(?=\n\n\[u\d+\]|\n\n## |\Z)", pack, re.DOTALL
        )
        evidence = []
        for target in targets:
            alias = next(
                a
                for a, header, body in blocks
                if target.document in header
                and f"pdf page {target.pdf_page} |" in header
                and normalize_text(target.quote) in normalize_text(body)
            )
            evidence.append({"unit_id": alias, "quote": target.quote, "role": "supports"})
        status = (
            "evidence_found"
            if self.injection
            else {
                "found": "evidence_found",
                "partial": "partial_evidence",
                "not_found": "no_evidence_found",
                "inconsistent": "inconsistent",
            }[cell.status]
        )
        # Inconsistent submissions require paired sides; the key supplies both.
        if status == "inconsistent":
            for i, e in enumerate(evidence):
                e["role"] = "side_a" if i == 0 else "side_b"
        payload = {
            "status": status,
            "rationale": "Reference oracle",
            "missing_aspects": ["Reference key is partial"] if status == "partial_evidence" else [],
            "relief_applied": bool(cell.relief_applied),
            "relief_id": cell.relief_applied,
            "evidence": evidence,
            "facts": [],
        }
        return await FakeLLM(
            [FakeTurn(tool_calls=[("submit_finding", payload)])], ModelTable.load()
        ).complete(**kw)


async def new_run(engine, pid, uid, method):
    cs = load_check_set()
    async with engine.begin() as c:
        await c.execute(
            pg_insert(CheckSet)
            .values(version=cs.version, yaml_sha256=cs.sha256)
            .on_conflict_do_nothing()
        )
        return (
            await c.execute(
                insert(Run)
                .values(
                    pack_id=pid,
                    created_by=uid,
                    method=method,
                    model_key="luna",
                    check_set_version=cs.version,
                    prompt_sha256=prompt_sha256(),
                    budget_usd=Decimal(10),
                    status="queued",
                    idempotency_key=uuid4().hex,
                )
                .returning(Run.id)
            )
        ).scalar_one()


@pytest.mark.parametrize("injection", [False, True])
async def test_lc_oracle(env, injection):
    engine, pid, uid, _, url = env
    rid = await new_run(engine, pid, uid, "LC")
    async with open_checkpointer(url) as saver:
        deps = RunDeps(
            Oracle(injection), ModelTable.load(), HashEmbedder(), saver, emit=EventSink(engine).emit
        )
        await run_run(engine, rid, deps=deps)
    score = await score_stored_run(engine, rid, PACK)
    assert score["injection_gate"]["pass"] is not injection
    if not injection:
        assert score["status_accuracy"] == 1
        assert score["evidence_recall"] == 1
        assert score["citation_accuracy"] == 1


async def test_agent_tool_event(env):
    engine, pid, uid, _, url = env
    rid = await new_run(engine, pid, uid, "A")

    class SearchFake:
        async def complete(self, **kw):
            turn = FakeTurn(tool_calls=[("keyword_search", {"query": "  CLIMATE   risk  "})])
            return await FakeLLM([turn], ModelTable.load()).complete(**kw)

    async with open_checkpointer(url) as saver:
        await run_run(
            engine,
            rid,
            deps=RunDeps(
                SearchFake(),
                ModelTable.load(),
                HashEmbedder(),
                saver,
                step_cap=2,  # step 1 runs the search; step 2 is forced to submit
                emit=EventSink(engine).emit,
            ),
        )
    async with engine.connect() as c:
        events = (
            (
                await c.execute(
                    select(RunEvent).where(
                        RunEvent.run_id == rid,
                        RunEvent.type == "tool_called",
                        RunEvent.check_id == "G1",
                    )
                )
            )
            .mappings()
            .all()
        )
    assert len(events) == 1
    assert events[0].payload["query"] == "climate risk"
    assert len(events[0].payload["part_ids"]) == 3
