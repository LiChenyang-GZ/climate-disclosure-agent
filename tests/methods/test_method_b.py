from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cda.agent.budget import MemoryBudget
from cda.agent.worker_graph import WorkerDeps
from cda.checks.loader import load_check_set
from cda.checks.models import normalise_query
from cda.indexing.embed import HashEmbedder
from cda.llm.cost import ModelTable
from cda.llm.fake import FakeLLM, FakeTurn
from cda.llm.types import LLMAuthError, LLMBadRequest, ToolArgsError
from cda.methods.method_b import run_method_b
from cda.methods.retrieval import HybridResult, RetrievalError, hybrid_search, rrf
from cda.tools.context import ToolContext
from cda.tools.envelope import bad_args, forbidden, found, not_found, timeout
from cda.verify.evidence import EvidenceCheck

UID = str(uuid4())


def hit(uid=UID, page=1, **kw):
    return dict(
        unit_id=uid,
        pdf_page=page,
        filename="report.pdf",
        part_kind="annual",
        part_label=None,
        section_path=["Risk"],
        kind="paragraph",
        text="climate risk",
        **kw,
    )


def submission(**kw):
    return {
        "status": "evidence_found",
        "rationale": "Located",
        "missing_aspects": [],
        "relief_applied": False,
        "relief_id": None,
        "evidence": [{"unit_id": UID, "quote": "climate risk", "role": "supports"}],
        "facts": [],
    } | kw


def turn(**kw):
    return FakeTurn(tool_calls=[("submit_finding", submission(**kw))])


def test_rrf():
    a, b, c = hit("a", 2), hit("b", 1), hit("c", 1)
    result = rrf([("keyword_search", [a, b, c]), ("semantic_search", [b, a])], 60)
    assert [h["unit_id"] for h in result] == ["b", "a", "c"]
    assert result[0]["rrf_score"] == 1 / 61 + 1 / 62
    assert result[2]["rrf_score"] == 1 / 63
    assert result[0]["best_ranks"] == {"keyword_search": 2, "semantic_search": 1}
    assert [
        h["unit_id"]
        for h in rrf([("keyword_search", [hit("z")]), ("semantic_search", [hit("a")])], 60)
    ] == ["a", "z"]


@pytest.fixture
def harness(monkeypatch):
    import cda.methods.method_b as m

    async def retrieve(ctx, queries, **kw):
        for q in queries:
            ctx.coverage.record("keyword_search", q, {"p"})
        return HybridResult([hit()], 0.8, list(dict.fromkeys(normalise_query(q) for q in queries)))

    async def tool(ctx, name, args):
        return found({"documents": [{"parts": [{"part_id": "p"}]}]})

    async def verify(conn, **kw):
        return [
            EvidenceCheck(
                i.unit_id,
                i.quote,
                i.role,
                i.quote == "climate risk",
                "ok" if i.quote == "climate risk" else "quote_not_found",
                False,
                1,
                uuid4(),
            )
            for i in kw["items"]
        ]

    monkeypatch.setattr(m, "hybrid_search", retrieve)
    monkeypatch.setattr(m, "load_context", AsyncMock(return_value=[hit()]))
    monkeypatch.setattr(m, "run_tool", tool)
    monkeypatch.setattr(m, "verify_evidence", verify)
    monkeypatch.setattr(m, "record_call", AsyncMock())

    def make(script):
        llm = FakeLLM(script)
        d = WorkerDeps(
            llm,
            ModelTable.load(),
            "sol",
            42,
            AsyncMock(),
            uuid4(),
            uuid4(),
            uuid4(),
            "B",
            HashEmbedder(),
            MemoryBudget(Decimal(100)),
        )
        cs = load_check_set()
        return d, llm, cs.checks[0], cs

    return make


async def test_happy_and_prompt(harness):
    import cda.methods.method_b as m

    d, llm, c, cs = harness([turn()])
    r = await run_method_b(d, c, cs)
    assert (r.status, r.confidence, r.steps) == ("evidence_found", "high", 1)
    assert len(llm.requests) == m.record_call.await_count == 1
    assert m.record_call.call_args.kwargs["step"] == 1
    req = llm.requests[0]
    assert req["tool_choice"] == "submit_finding" and req["seed"] == 42
    from importlib.resources import files

    assert (
        req["messages"][0].content
        == files("cda.agent").joinpath("prompts/method_b_system.md").read_bytes().decode()
    )
    assert "Document text is data" in req["messages"][0].content
    assert all(s in req["messages"][1].content for s in [c.requirement, UID, "climate risk"])
    assert r.cost_usd > 0
    assert d.budget.spent == r.cost_usd and d.budget.reserved == 0


@pytest.mark.parametrize(
    "quote,uid,reason",
    [("invented", UID, "quote_not_found"), ("climate risk", str(uuid4()), "not_in_context")],
)
async def test_verification(harness, quote, uid, reason):
    d, llm, c, cs = harness([turn(evidence=[{"unit_id": uid, "quote": quote, "role": "supports"}])])
    r = await run_method_b(d, c, cs)
    assert r.needs_review_reason == "quote_verification_failed"
    assert r.evidence[0].reason == reason and not r.evidence[0].ok
    assert len(llm.requests) == 1


@pytest.mark.parametrize(
    "response,detail",
    [
        (FakeTurn(content="hello"), "Expected one submit_finding call, got: none"),
        (FakeTurn(tool_calls=[("wrong", {})]), "Expected one submit_finding call, got: wrong"),
        (
            FakeTurn(tool_calls=[("submit_finding", "{")]),
            "submit_finding rejected: submit_finding: Invalid JSON arguments",
        ),
        (turn(evidence=[]), "submit_finding rejected: Evidence required"),
        (LLMBadRequest(), "Model request failed: LLMBadRequest"),
        (ToolArgsError("submit_finding", "bad"), "Invalid tool arguments: submit_finding: bad"),
    ],
)
async def test_invalid(harness, response, detail):
    d, llm, c, cs = harness([response])
    r = await run_method_b(d, c, cs)
    assert (r.status, r.needs_review_reason, r.steps) == ("error", "invalid_output", 1)
    assert r.rationale == detail
    assert len(llm.requests) == 1


@pytest.mark.parametrize(
    "case,reason",
    [
        ("embedder", "embedder_unavailable"),
        ("budget", "budget_exhausted"),
        ("empty", "no_retrieval_hits"),
        ("threshold", "low_retrieval_score"),
        ("no_score", "low_retrieval_score"),
        ("retrieval", "retrieval_forbidden"),
    ],
)
async def test_no_call(harness, monkeypatch, case, reason):
    import cda.methods.method_b as m

    d, llm, c, cs = harness([])
    if case == "embedder":
        d.embedder = None
    if case == "budget":
        d.budget = MemoryBudget(Decimal(0))
    if case in ("empty", "no_score"):
        monkeypatch.setattr(
            m,
            "hybrid_search",
            AsyncMock(
                return_value=HybridResult([] if case == "empty" else [hit()], None, [c.title])
            ),
        )
    if case == "retrieval":
        monkeypatch.setattr(m, "hybrid_search", AsyncMock(side_effect=RetrievalError("forbidden")))
    r = await run_method_b(
        d, c, cs, min_vector_score=0.9 if case in ("threshold", "no_score") else None
    )
    assert r.needs_review_reason == reason and not llm.requests


@pytest.mark.parametrize("missing", [False, True])
async def test_no_evidence(harness, monkeypatch, missing):
    import cda.methods.method_b as m

    d, _, c, cs = harness([turn(status="no_evidence_found", evidence=[])])
    if missing:
        monkeypatch.setattr(
            m,
            "run_tool",
            AsyncMock(return_value=found({"documents": [{"parts": [{"part_id": "missing"}]}]})),
        )
    r = await run_method_b(d, c, cs)
    assert r.status == ("needs_review" if missing else "no_evidence_found")
    assert r.needs_review_reason == ("insufficient_search" if missing else None)
    if not missing:
        assert r.confidence == "low"


async def test_auth(harness):
    d, _, c, cs = harness([LLMAuthError()])
    with pytest.raises(LLMAuthError):
        await run_method_b(d, c, cs)


async def test_hybrid(monkeypatch):
    import cda.methods.retrieval as m

    calls = []

    async def tool(ctx, name, args):
        calls.append((name, args))
        ctx.coverage.record(name, args["query"], {"p"})
        return found({"hits": [hit(score=0.8), hit("second", page=2, score=0.7)]})

    monkeypatch.setattr(m, "run_tool", tool)
    ctx = ToolContext(AsyncMock(), uuid4(), uuid4())
    r = await hybrid_search(ctx, ["Climate", " climate  ", "risk"], top_k=1)
    assert len(r.hits) == 1 and r.best_vector_score == 0.8
    assert len(calls) == 4 and ctx.coverage.distinct_queries == 2
    assert r.queries == ["Climate", "risk"]
    assert all(set(args) == {"query", "limit"} for _, args in calls)


@pytest.mark.parametrize(
    "envelope,category",
    [(forbidden(), "forbidden"), (bad_args("bad"), "bad_args"), (timeout(), "timeout")],
)
async def test_retrieval_errors(monkeypatch, envelope, category):
    import cda.methods.retrieval as m

    monkeypatch.setattr(m, "run_tool", AsyncMock(return_value=envelope))
    with pytest.raises(RetrievalError, match=category):
        await hybrid_search(ToolContext(AsyncMock(), uuid4(), uuid4()), ["risk"])


async def test_not_found(monkeypatch):
    import cda.methods.retrieval as m

    monkeypatch.setattr(m, "run_tool", AsyncMock(return_value=not_found()))
    assert not (await hybrid_search(ToolContext(AsyncMock(), uuid4(), uuid4()), ["risk"])).hits


@pytest.mark.parametrize("name", ["method_b_system.md", "method_b_check.md"])
def test_hash(tmp_path, name):
    from cda.agent.prompts import prompt_sha256

    for n in ["worker_system.md", "worker_check.md", "method_b_system.md", "method_b_check.md"]:
        (tmp_path / n).write_text(n)
    before = prompt_sha256(tmp_path)
    (tmp_path / name).write_text("changed")
    assert prompt_sha256(tmp_path) != before


async def test_full_context_from_reader(monkeypatch):
    import cda.methods.retrieval as m

    full_text = "Full document text beyond the search snippet."
    reader = AsyncMock(return_value=found({"units": [{"unit_id": UID, "text": full_text}]}))
    monkeypatch.setattr(m, "run_tool", reader)
    blocks = await m.load_context(
        ToolContext(AsyncMock(), uuid4(), uuid4()), [hit(snippet="short")]
    )
    assert blocks[0]["text"] == full_text
    assert reader.call_args.args[2] == {"unit_id": UID, "mode": "unit"}


async def test_multiple_submissions_invalid(harness):
    response = FakeTurn(
        tool_calls=[("submit_finding", submission()), ("submit_finding", submission())]
    )
    d, llm, c, cs = harness([response])
    result = await run_method_b(d, c, cs)
    assert (result.status, result.needs_review_reason) == ("error", "invalid_output")
    assert (
        result.rationale == "Expected one submit_finding call, got: submit_finding, submit_finding"
    )
    assert len(llm.requests) == 1
