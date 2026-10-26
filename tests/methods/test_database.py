"""Database integration with the T08/T12 fixtures and local embeddings."""

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import func, insert, select, text, update

from cda.agent.run_graph import run_run
from cda.agent.store import store_finding
from cda.db.models import Document, Evidence, Finding, LLMCall, Run, Unit
from cda.indexing.embed import HashEmbedder
from cda.methods.retrieval import RetrievalError, hybrid_search, load_context

pytestmark = pytest.mark.db


async def test_database_search(setup):
    ctx = setup["ctx"]
    result = await hybrid_search(ctx, ["climate risk", " CLIMATE  risk ", "risk"], top_k=3)
    assert len(result.hits) == 3
    assert ctx.coverage.distinct_queries == 2
    assert {t for t, _, _ in ctx.coverage.queries} == {"keyword_search", "semantic_search"}
    assert not {h["document_id"] for h in result.hits} & {
        str(setup[k]) for k in ["deleted", "foreign", "same"]
    }


async def test_database_permission(setup):
    ctx = replace(setup["ctx"], user_id=setup["alex"])
    with pytest.raises(RetrievalError, match="forbidden"):
        await hybrid_search(ctx, ["climate"])


async def test_table_parent_once(setup, monkeypatch):
    import cda.methods.retrieval as m

    ctx = setup["ctx"]
    parent = uuid4()
    await ctx.conn.execute(
        insert(Unit).values(
            id=parent,
            document_id=setup["annual"],
            seq=20,
            kind="table",
            pdf_page=1,
            section_path="Risk",
            text="Full table",
            parser_version="test",
        )
    )
    # Units are immutable (D52), so the table rows are inserted as children of the table.
    ids = [uuid4(), uuid4()]
    for seq, uid in enumerate(ids, start=21):
        await ctx.conn.execute(
            insert(Unit).values(
                id=uid,
                document_id=setup["annual"],
                parent_id=parent,
                seq=seq,
                kind="table_row",
                pdf_page=1,
                section_path="Risk",
                text="carbon liability table row",
                parser_version="test",
                embedding=HashEmbedder().embed_query("carbon liability"),
            )
        )
    original = m.run_tool
    calls = []

    async def tool(ctx, name, args):
        calls.append((name, args))
        return await original(ctx, name, args)

    monkeypatch.setattr(m, "run_tool", tool)
    result = await hybrid_search(ctx, ["carbon liability"], top_k=10)
    rows = [h for h in result.hits if h["unit_id"] in set(map(str, ids))]
    assert len(rows) == 2
    context = await load_context(ctx, rows)
    assert [h["unit_id"] for h in context].count(str(parent)) == 1
    assert sum(args.get("mode") == "parent" for _, args in calls) == 1


@pytest.mark.parametrize("method", ["B", "LC"])
async def test_run_dispatch(run_env, monkeypatch, method):
    import cda.agent.run_graph as g
    from cda.checks.loader import load_check_set

    engine, rid, deps, llm, _ = run_env
    cs = load_check_set()
    monkeypatch.setattr(
        g, "load_check_set", lambda: cs.model_copy(update={"checks": cs.checks[:2]})
    )
    async with engine.begin() as conn:
        await conn.execute(update(Run).where(Run.id == rid).values(method=method))
    if method == "LC":
        from cda.llm.fake import FakeLLM, FakeTurn

        answer = {
            "status": "evidence_found",
            "rationale": "Found the cited report text.",
            "missing_aspects": [],
            "relief_applied": False,
            "relief_id": None,
            "evidence": [{"unit_id": "u1", "quote": "climate risk", "role": "supports"}],
            "facts": [],
        }
        llm = FakeLLM([FakeTurn(tool_calls=[("submit_finding", answer)]) for _ in range(2)])
        deps.llm = llm
    status = await run_run(engine, rid, deps=deps)
    async with engine.connect() as conn:
        run = (await conn.execute(select(Run).where(Run.id == rid))).mappings().one()
        findings = (
            (await conn.execute(select(Finding).where(Finding.run_id == rid))).mappings().all()
        )
        calls = (await conn.execute(select(LLMCall).where(LLMCall.run_id == rid))).mappings().all()
    assert status == "final" and len(findings) == len(calls) == 2
    assert all(
        f.status == "evidence_found" and f.confidence == "high" and f.steps == 1 for f in findings
    )
    assert run.spent_usd == sum(c.cost_usd for c in calls)


@pytest.mark.parametrize(
    "case,status,reason,calls",
    [
        ("happy", "evidence_found", None, 1),
        ("bad_quote", "needs_review", "quote_verification_failed", 1),
        ("outside_context", "needs_review", "quote_verification_failed", 1),
        ("unknown_unit", "needs_review", "quote_verification_failed", 1),
        ("empty", "needs_review", "no_retrieval_hits", 0),
        ("threshold", "needs_review", "low_retrieval_score", 0),
        ("no_evidence", "no_evidence_found", None, 1),
        ("plain", "error", "invalid_output", 1),
        ("wrong_tool", "error", "invalid_output", 1),
        ("rules", "error", "invalid_output", 1),
        ("budget", "not_checked", "budget_exhausted", 0),
        ("embedder", "error", "embedder_unavailable", 0),
    ],
)
async def test_database_worker(run_env, case, status, reason, calls):
    from decimal import Decimal

    from cda.agent.budget import MemoryBudget
    from cda.agent.worker_graph import WorkerDeps
    from cda.checks.loader import load_check_set
    from cda.llm.fake import FakeLLM, FakeTurn
    from cda.methods.method_b import run_method_b

    engine, rid, deps, _, user = run_env
    async with engine.begin() as conn:
        run = (await conn.execute(select(Run).where(Run.id == rid))).mappings().one()
        unit = (await conn.execute(select(Unit))).mappings().one()
        cited = unit.id
        if case == "unknown_unit":
            cited = uuid4()  # a unit id the model invented: no such row exists
        if case == "outside_context":
            cited = uuid4()
            await conn.execute(
                insert(Unit).values(
                    id=cited,
                    document_id=unit.document_id,
                    seq=1,
                    kind="paragraph",
                    pdf_page=1,
                    section_path="Other",
                    text="climate risk",
                    parser_version="test",
                )
            )
            # It has neither an embedding nor matching FTS terms for these queries.
            cs = load_check_set()
            check = cs.checks[0].model_copy(update={"title": "governance", "queries": ["board"]})
        else:
            cs = load_check_set()
            check = cs.checks[0]
        args = {
            "status": "no_evidence_found" if case == "no_evidence" else "evidence_found",
            "rationale": "Located",
            "missing_aspects": [],
            "relief_applied": False,
            "relief_id": None,
            "evidence": []
            if case in ("no_evidence", "rules")
            else [
                {
                    "unit_id": str(cited),
                    "quote": "invented" if case == "bad_quote" else "climate risk",
                    "role": "supports",
                }
            ],
            "facts": [],
        }
        turn = (
            FakeTurn(content="hello")
            if case == "plain"
            else FakeTurn(
                tool_calls=[("wrong" if case == "wrong_tool" else "submit_finding", args)]
            )
        )
        llm = FakeLLM([turn])
        budget = MemoryBudget(Decimal(0 if case == "budget" else 100))
        worker = WorkerDeps(
            llm,
            deps.models,
            "sol",
            42,
            conn,
            user,
            run.pack_id,
            rid,
            "B",
            None if case == "embedder" else deps.embedder,
            budget,
        )
        if case == "empty":
            # Units are immutable (D52): point the worker at a pack of the same team whose
            # only document has no units, so neither search can return a hit.
            empty_pack = await conn.scalar(
                text(
                    "INSERT INTO packs(company, reporting_year, team_id, created_by) "
                    "SELECT 'empty', reporting_year, team_id, created_by FROM packs "
                    "WHERE id = :pack RETURNING id"
                ),
                {"pack": run.pack_id},
            )
            await conn.execute(
                insert(Document).values(
                    pack_id=empty_pack,
                    kind="annual",
                    filename="empty.pdf",
                    sha256="e" * 64,
                    page_count=1,
                    parser_version="v1",
                )
            )
            worker = replace(worker, pack_id=empty_pack)
        result = await run_method_b(
            worker, check, cs, min_vector_score=2 if case == "threshold" else None
        )
        assert (result.status, result.needs_review_reason) == (status, reason)
        assert len(llm.requests) == calls
        assert (
            await conn.scalar(
                select(func.count()).select_from(LLMCall).where(LLMCall.run_id == rid)
            )
            == calls
        )
        assert budget.spent == result.cost_usd and budget.reserved == 0
        if case == "outside_context":
            assert result.evidence[0].reason == "not_in_context"
        if case == "unknown_unit":
            # Regression: relabelling this as not_in_context made store_finding insert a
            # dangling unit id and fail the whole run with IntegrityError.
            assert result.evidence[0].reason == "unit_not_found"
            fid = await store_finding(conn, rid, result)
            assert not (
                await conn.execute(select(Evidence).where(Evidence.finding_id == fid))
            ).all()
