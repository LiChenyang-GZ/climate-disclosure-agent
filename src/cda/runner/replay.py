"""Replay a recorded run without calling the model (D89).

uv run python -m cda.runner.replay --run <id> [--embedder bge|hash]
"""

import argparse
import asyncio
import os
import selectors
import sys
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from cda.agent.checkpoint import open_checkpointer
from cda.agent.hooks import Comparer, noop_comparer
from cda.agent.prompts import prompt_sha256
from cda.agent.run_graph import RunDeps, run_run
from cda.checks.loader import load_check_set
from cda.db.models import Finding, LLMCall, Run
from cda.db.session import make_engine
from cda.indexing.embed import BgeEmbedder, Embedder, HashEmbedder
from cda.llm.cost import ModelTable
from cda.llm.replay import ReplayLLM


class ReplayRefused(Exception):
    """The source run cannot be replayed with the current code."""


@dataclass
class ReplayReport:
    source_id: UUID
    run_id: UUID
    status: str
    misses: int
    # (check_id, source status, replay status) for every check whose status differs.
    differences: list[tuple[str, str, str]] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"Replay run: {self.run_id} (source {self.source_id})",
            f"Status: {self.status}",
            f"Misses: {self.misses}",
        ]
        if self.differences:
            out.append("Differences (check: source -> replay):")
            out += [f"  {cid}: {old} -> {new}" for cid, old, new in self.differences]
        else:
            out.append("Differences: none")
        return out


async def _compare(conn: AsyncConnection, run_id: UUID) -> None:
    from cda.compare import compare_run

    await compare_run(conn, run_id)


async def _statuses(engine: AsyncEngine, run_id: UUID) -> dict[str, str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(Finding.check_id, Finding.status).where(Finding.run_id == run_id)
        )
        return {r.check_id: r.status for r in rows}


async def replay_run(
    engine: AsyncEngine,
    source_id: UUID,
    *,
    embedder: Embedder | None,
    checkpointer: BaseCheckpointSaver[Any],
    comparer: Comparer = noop_comparer,
) -> ReplayReport:
    cs = load_check_set()
    async with engine.begin() as conn:
        source = (await conn.execute(select(Run).where(Run.id == source_id))).mappings().first()
        if source is None:
            raise ReplayRefused("run not found")
        recorded = await conn.scalar(
            select(func.count())
            .select_from(LLMCall)
            .where(LLMCall.run_id == source_id, LLMCall.response.is_not(None))
        )
        if not recorded:
            raise ReplayRefused("no recorded responses")
        if source.check_set_version != cs.version or source.prompt_sha256 != prompt_sha256():
            raise ReplayRefused("recorded with a different check set / prompts")
        llm = await ReplayLLM.from_run(conn, source_id)
        new_id: UUID = (
            await conn.execute(
                insert(Run)
                .values(
                    pack_id=source.pack_id,
                    method=source.method,
                    model_key=source.model_key,
                    check_set_version=source.check_set_version,
                    prompt_sha256=source.prompt_sha256,
                    budget_usd=source.budget_usd,
                    check_ids=source.check_ids,
                    skipped_documents=source.skipped_documents,
                    settings=source.settings,
                    status="queued",
                    idempotency_key=f"replay-{uuid4()}",
                    created_by=source.created_by,
                )
                .returning(Run.id)
            )
        ).scalar_one()
    deps = RunDeps(
        llm=llm,
        models=ModelTable.load(),
        embedder=embedder,
        checkpointer=checkpointer,
        comparer=comparer,
    )
    status = await run_run(engine, new_id, deps=deps)
    before, after = await _statuses(engine, source_id), await _statuses(engine, new_id)
    differences = [
        (cid, before.get(cid, "missing"), after.get(cid, "missing"))
        for cid in sorted(set(before) | set(after))
        if before.get(cid) != after.get(cid)
    ]
    return ReplayReport(source_id, new_id, status, llm.misses, differences)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Replay a recorded run without calling the model.")
    p.add_argument("--run", type=UUID, required=True)
    p.add_argument("--embedder", choices=("bge", "hash"), default="bge")
    return p


async def run_cli(args: argparse.Namespace, url: str) -> ReplayReport:
    engine = make_engine(url)
    embedder: Embedder = HashEmbedder() if args.embedder == "hash" else BgeEmbedder()
    if isinstance(embedder, BgeEmbedder):
        await asyncio.to_thread(embedder.warm_up)
    try:
        async with open_checkpointer(url) as saver:
            return await replay_run(
                engine, args.run, embedder=embedder, checkpointer=saver, comparer=_compare
            )
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    url = os.environ.get("CDA_DATABASE_URL")
    if not url:
        print("CDA_DATABASE_URL is required", file=sys.stderr)
        return 2

    def loop_factory() -> asyncio.AbstractEventLoop:
        # psycopg (the checkpointer) needs a selector loop on Windows.
        if sys.platform == "win32":
            return asyncio.SelectorEventLoop(selectors.SelectSelector())
        return asyncio.new_event_loop()

    try:
        with asyncio.Runner(loop_factory=loop_factory) as runner:
            report = runner.run(run_cli(args, url))
    except ReplayRefused as exc:
        print(f"Cannot replay {args.run}: {exc}", file=sys.stderr)
        return 2
    print("\n".join(report.lines()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
