"""Run or re-score local evaluation packs; paid runs require an explicit guard."""

import argparse
import asyncio
import json
import os
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from cda.agent.checkpoint import open_checkpointer
from cda.agent.prompts import prompt_sha256
from cda.agent.run_graph import RunDeps, run_run
from cda.checks.loader import load_check_set
from cda.compare import compare_run
from cda.db.models import (
    CheckSet,
    Document,
    Evidence,
    Inconsistency,
    Pack,
    Run,
    RunEvent,
    Unit,
)
from cda.db.models import (
    Finding as DBFinding,
)
from cda.db.session import make_engine
from cda.eval.answer_key import load_answer_key
from cda.eval.metrics import Citation, Finding, score_run, summarize
from cda.eval.pack import ingest_pack
from cda.eval.report import render_report
from cda.eval.retrieval_eval import evaluate_retrieval
from cda.indexing.embed import BgeEmbedder, Embedder, HashEmbedder
from cda.llm.cost import ModelTable
from cda.llm.openai_client import OpenAIClient
from cda.runner.events import EventSink
from cda.tools.context import ToolContext
from cda.tools.parts_sql import inventory
from cda.tools.rls import tool_scope


async def _compare(conn: AsyncConnection, run_id: UUID) -> None:
    await compare_run(conn, run_id)


async def score_stored_run(
    engine: AsyncEngine, run_id: UUID, folder: Path, *, embedder: Embedder | None = None
) -> dict[str, Any]:
    key = load_answer_key(folder / "answer_key.yaml")
    async with engine.connect() as conn:
        run = (await conn.execute(select(Run).where(Run.id == run_id))).mappings().one()
        company = await conn.scalar(select(Pack.company).where(Pack.id == run.pack_id))
        if company != key.company:
            raise ValueError("run company disagrees with answer key")
        rows = (
            (await conn.execute(select(DBFinding).where(DBFinding.run_id == run_id)))
            .mappings()
            .all()
        )
        findings = []
        for f in rows:
            evidence = (
                (
                    await conn.execute(
                        select(
                            Evidence.quote,
                            Evidence.verified,
                            Evidence.role,
                            Unit.pdf_page,
                            Document.filename,
                        )
                        .join(Unit, Evidence.unit_id == Unit.id)
                        .join(Document, Unit.document_id == Document.id)
                        .where(Evidence.finding_id == f.id)
                    )
                )
                .mappings()
                .all()
            )
            findings.append(
                Finding(
                    f.check_id,
                    f.status,
                    f.needs_review_reason,
                    f.confidence,
                    f.steps,
                    f.tokens_in,
                    f.tokens_out,
                    float(f.cost_usd),
                    [
                        Citation(e.filename, e.pdf_page, e.quote, e.verified, e.role)
                        for e in evidence
                    ],
                )
            )
        issues = [
            dict(r)
            for r in (
                await conn.execute(
                    select(Inconsistency.type, Inconsistency.detail).where(
                        Inconsistency.run_id == run_id
                    )
                )
            ).mappings()
        ]
        filenames = {
            str(r.id): r.filename
            for r in (
                await conn.execute(
                    select(Document.id, Document.filename).where(Document.pack_id == run.pack_id)
                )
            ).all()
        }

        def name_sides(value: Any) -> None:
            if isinstance(value, dict):
                if "document_id" in value:
                    value["document"] = filenames.get(str(value["document_id"]))
                for child in list(value.values()):
                    name_sides(child)
            elif isinstance(value, list):
                for child in value:
                    name_sides(child)

        for issue in issues:
            name_sides(issue["detail"])
        events = [
            dict(r)
            for r in (
                await conn.execute(
                    select(RunEvent.type, RunEvent.check_id, RunEvent.payload).where(
                        RunEvent.run_id == run_id
                    )
                )
            ).mappings()
        ]
        ctx = ToolContext(conn, run.created_by, run.pack_id, embedder)
        async with tool_scope(conn, run.created_by, 10):
            docs = await inventory(ctx)
        parts = {p["part_id"] for d in docs or [] for p in d["parts"]}
        end = run.finished_at or max(
            (
                r["created_at"]
                for r in (
                    await conn.execute(select(RunEvent.created_at).where(RunEvent.run_id == run_id))
                ).mappings()
            ),
            default=run.started_at,
        )
        elapsed = (end - run.started_at).total_seconds() if end and run.started_at else 0
        result = score_run(key, findings, issues, events, elapsed, parts, run.method)
        result.update(
            run_id=str(run_id),
            method=run.method,
            model=run.model_key,
            prompt_hash=run.prompt_sha256,
            check_set_version=run.check_set_version,
        )
        return result


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pack", type=Path, required=True)
    p.add_argument("--methods", default="A,B,C,LC")
    p.add_argument("--model", default="luna")
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--budget-usd", type=Decimal, default=Decimal("2.0"))
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--retrieval-only", action="store_true")
    p.add_argument("--embedder", choices=("bge", "hash"), default="bge")
    p.add_argument("--score-runs")
    p.add_argument("--lc-scope", choices=("pack", "sustainability"), default="pack")
    p.add_argument("--step-cap", type=int, default=RunDeps.step_cap)
    p.add_argument("--token-cap", type=int, default=RunDeps.token_cap)
    p.add_argument("--yes", action="store_true")
    return p


async def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    url = os.environ.get("CDA_DATABASE_URL")
    if not url:
        raise ValueError("CDA_DATABASE_URL is required")
    engine = make_engine(url)
    embedder = HashEmbedder() if args.embedder == "hash" else BgeEmbedder()
    if isinstance(embedder, BgeEmbedder):
        await asyncio.to_thread(embedder.warm_up)
    key = load_answer_key(args.pack / "answer_key.yaml")
    cs = load_check_set()
    try:
        commit = (
            await asyncio.to_thread(
                subprocess.run,
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            )
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "unknown"
    results: dict[str, Any] = {
        "pack": key.company,
        "labeler": key.labeler,
        "key_version": key.key_version,
        "model": args.model,
        "prompt_hash": prompt_sha256(),
        "check_set_version": cs.version,
        "git_commit": commit,
        "date": datetime.now(UTC).isoformat(),
        "repeats": args.repeats,
        "lc_scope": args.lc_scope,
        "step_cap": args.step_cap,
        "token_cap": args.token_cap,
        "methods": {},
        "changes": [c.model_dump(mode="json") for c in key.changes],
    }
    try:
        if args.score_runs:
            ids = [UUID(value.strip()) for value in args.score_runs.split(",")]
        else:
            pid = await ingest_pack(engine, args.pack, embedder=embedder)
            async with engine.connect() as conn:
                uid = (
                    await conn.execute(select(Pack.created_by).where(Pack.id == pid))
                ).scalar_one()
            if args.retrieval_only:
                results["retrieval_only"] = True
                async with engine.begin() as conn:
                    results["retrieval"] = await evaluate_retrieval(
                        ToolContext(conn, uid, pid, embedder), key
                    )
                return results
            models = ModelTable.load()
            llm = OpenAIClient(models)
            ids = []
            async with open_checkpointer(url) as saver:
                deps = RunDeps(
                    llm,
                    models,
                    embedder,
                    saver,
                    comparer=_compare,
                    emit=EventSink(engine).emit,
                    lc_scope=args.lc_scope,
                    step_cap=args.step_cap,
                    token_cap=args.token_cap,
                )
                for method in args.methods.split(","):
                    for _ in range(args.repeats):
                        async with engine.begin() as conn:
                            await conn.execute(
                                pg_insert(CheckSet)
                                .values(version=cs.version, yaml_sha256=cs.sha256)
                                .on_conflict_do_nothing()
                            )
                            rid = (
                                await conn.execute(
                                    insert(Run)
                                    .values(
                                        pack_id=pid,
                                        method=method,
                                        model_key=args.model,
                                        check_set_version=cs.version,
                                        prompt_sha256=prompt_sha256(),
                                        budget_usd=args.budget_usd,
                                        idempotency_key=str(uuid4()),
                                        created_by=uid,
                                        status="queued",
                                    )
                                    .returning(Run.id)
                                )
                            ).scalar_one()
                        await run_run(engine, rid, deps=deps)
                        ids.append(rid)
        for rid in ids:
            score = await score_stored_run(engine, rid, args.pack, embedder=embedder)
            results["methods"].setdefault(score["method"], []).append(score)
        if args.score_runs:
            scored = [r for runs in results["methods"].values() for r in runs]
            for field in ("model", "prompt_hash", "check_set_version"):
                results[field] = ", ".join(sorted({r[field] for r in scored}))
            results["repeats"] = {m: len(runs) for m, runs in results["methods"].items()}
        results["summaries"] = {
            m: {
                k: summarize([float(r[k]) for r in runs])
                for k, v in runs[0].items()
                if isinstance(v, (float, int)) and not isinstance(v, bool)
            }
            for m, runs in results["methods"].items()
        }
        return results
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    p = parser()
    args = p.parse_args(argv)
    methods = args.methods.split(",")
    if args.repeats < 1 or not args.budget_usd.is_finite() or args.budget_usd <= 0:
        p.error("repeats and budget must be positive and finite")
    if args.step_cap < 2:
        p.error("step cap must be at least 2")
    if args.token_cap < 1:
        p.error("token cap must be positive")
    if not methods or any(m not in ("A", "B", "C", "LC") for m in methods):
        p.error("unknown method")
    spend = (
        Decimal(0)
        if args.retrieval_only or args.score_runs
        else len(methods) * args.repeats * args.budget_usd
    )
    print(f"Estimated maximum spend: ${spend:.2f}")
    if spend > 1 and not args.yes:
        p.error("estimated spend exceeds $1; pass --yes to proceed")
    results = asyncio.run(evaluate(args))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "results.json").write_text(
        json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (args.out / "report.md").write_text(render_report(results), encoding="utf-8")


if __name__ == "__main__":
    import sys

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    main()
