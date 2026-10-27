from contextlib import asynccontextmanager
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from cda.agent.budget import MemoryBudget
from cda.agent.worker_graph import WorkerDeps
from cda.checks.loader import load_check_set
from cda.indexing.embed import HashEmbedder
from cda.llm.cost import ModelTable
from cda.llm.fake import FakeLLM, FakeTurn
from cda.llm.types import LLMAuthError, ToolArgsError
from cda.tools.context import ToolContext
from cda.tools.envelope import forbidden, found
from cda.verify.evidence import EvidenceCheck

UIDS = [str(uuid4()) for _ in range(3)]


def unit(uid: str, filename: str, seq: int, text: str, **values):
    return {
        "id": UUID(uid),
        "document_id": UUID(values.pop("document_id", str(uuid4()))),
        "kind": values.pop("kind", "paragraph"),
        "seq": seq,
        "pdf_page": values.pop("pdf_page", 1),
        "section_path": values.pop("section_path", ["Climate"]),
        "text": text,
        "filename": filename,
        "part_id": values.pop("part_id", "part-a"),
        "part_kind": values.pop("part_kind", "annual"),
        "part_label": values.pop("part_label", None),
        **values,
    }


def submitted(unit_id: str = "u3", quote: str = "quote three", status: str = "evidence_found"):
    return {
        "status": status,
        "rationale": "Located in the report",
        "missing_aspects": [],
        "relief_applied": False,
        "relief_id": None,
        "evidence": []
        if status == "no_evidence_found"
        else [{"unit_id": unit_id, "quote": quote, "role": "supports"}],
        "facts": [],
    }


def make_deps(script, *, budget=Decimal(100)):
    conn = AsyncMock()
    ctx = ToolContext(conn, uuid4(), uuid4(), HashEmbedder())
    deps = WorkerDeps(
        FakeLLM(script),
        ModelTable.load(),
        "sol",
        17,
        conn,
        ctx.user_id,
        ctx.pack_id,
        uuid4(),
        "LC",
        HashEmbedder(),
        MemoryBudget(budget),
    )
    return deps


def verified(conn, *, items, **kwargs):
    return [
        EvidenceCheck(item.unit_id, item.quote, item.role, True, "ok", False, 1, uuid4())
        for item in items
    ]


@pytest.fixture
def three_units():
    doc_a, doc_b = str(uuid4()), str(uuid4())
    return [
        unit(UIDS[0], "z.pdf", 0, "quote one", document_id=doc_b),
        unit(UIDS[1], "a.pdf", 2, "quote two", document_id=doc_a, kind="table_row"),
        unit(UIDS[2], "a.pdf", 1, "quote three", document_id=doc_a),
        unit(str(uuid4()), "a.pdf", 3, "ignored child", document_id=doc_a, kind="table"),
        unit(str(uuid4()), "deleted.pdf", 0, "deleted text"),
        unit(str(uuid4()), "other-team.pdf", 0, "other team text"),
    ]


async def test_pack_text_order_aliases_headers_and_permissions(monkeypatch, three_units):
    import cda.methods.long_context as lc

    monkeypatch.setattr(lc, "run_tool", AsyncMock(return_value=found({"documents": []})))
    monkeypatch.setattr(lc, "tool_scope", _scope)
    monkeypatch.setattr(lc, "unit_query", lambda ctx: object())
    monkeypatch.setattr(lc, "_query_units", AsyncMock(return_value=three_units[:4]))

    pack = await lc.build_pack(ToolContext(AsyncMock(), uuid4(), uuid4()), "pack")
    assert [u["text"] for u in pack.units] == ["quote three", "quote two", "quote one"]
    assert list(pack.aliases) == ["u1", "u2", "u3"]
    assert "## a.pdf" in pack.text and "## z.pdf" in pack.text
    assert "[u1] a.pdf | annual/ | pdf page 1 | Climate\nquote three" in pack.text
    assert "deleted text" not in pack.text and "other team text" not in pack.text
    assert "ignored child" not in pack.text


async def test_sustainability_scope_only_keeps_sustainability_units(monkeypatch, three_units):
    import cda.methods.long_context as lc

    rows = [
        unit(UIDS[0], "report.pdf", 0, "annual words", part_kind="annual"),
        unit(UIDS[1], "report.pdf", 1, "sustainability words", part_kind="sustainability"),
    ]
    monkeypatch.setattr(lc, "run_tool", AsyncMock(return_value=found({"documents": []})))
    monkeypatch.setattr(lc, "tool_scope", _scope)
    monkeypatch.setattr(lc, "unit_query", lambda ctx: object())
    monkeypatch.setattr(lc, "_query_units", AsyncMock(return_value=rows))
    pack = await lc.build_pack(ToolContext(AsyncMock(), uuid4(), uuid4()), "sustainability")
    assert pack.text.endswith("sustainability words")
    assert "annual words" not in pack.text
    assert pack.parts == {"part-a"}


async def test_load_query_uses_permission_and_part_resolution(monkeypatch):
    from sqlalchemy.dialects import postgresql

    import cda.methods.long_context as lc

    scoped = []

    @asynccontextmanager
    async def scope(conn, user_id, timeout_s):
        scoped.append((user_id, timeout_s))
        yield

    conn = AsyncMock()
    rows = MagicMock()
    rows.mappings.return_value.all.return_value = []
    conn.execute.return_value = rows
    monkeypatch.setattr(lc, "tool_scope", scope)
    ctx = ToolContext(conn, uuid4(), uuid4())
    await lc._query_units(ctx, "sustainability")

    sql = str(conn.execute.await_args.args[0].compile(dialect=postgresql.dialect()))
    assert scoped == [(ctx.user_id, ctx.timeout_s)]
    assert "team_members" in sql and "documents.deleted_at IS NULL" in sql
    assert "packs.id" in sql and "coalesce(document_parts.kind, documents.kind) =" in sql


async def test_forbidden_pack_is_error_without_call(monkeypatch):
    import cda.methods.long_context as lc

    monkeypatch.setattr(lc, "build_pack", AsyncMock(return_value=lc.LongContextPack.forbidden()))
    deps = make_deps([])
    result = await lc.run_long_context(deps, load_check_set().checks[0], load_check_set())
    assert (result.status, result.needs_review_reason) == ("error", "retrieval_forbidden")
    assert not deps.llm.requests


async def test_pack_builder_stops_on_forbidden_inventory(monkeypatch):
    import cda.methods.long_context as lc

    rows = AsyncMock()
    monkeypatch.setattr(lc, "run_tool", AsyncMock(return_value=forbidden()))
    monkeypatch.setattr(lc, "_query_units", rows)
    pack = await lc.build_pack(ToolContext(AsyncMock(), uuid4(), uuid4()))
    assert pack.error_reason == "retrieval_forbidden" and not pack.text
    rows.assert_not_awaited()


async def test_happy_alias_mapping_prompt_and_budget(monkeypatch):
    import cda.methods.long_context as lc
    from cda.agent.prompts import render_lc

    uid1, uid2, uid3 = map(UUID, UIDS)
    pack = lc.LongContextPack(
        units=[
            unit(UIDS[0], "a.pdf", 0, "quote one"),
            unit(UIDS[1], "a.pdf", 1, "quote two"),
            unit(UIDS[2], "a.pdf", 2, "quote three"),
        ],
        aliases={"u1": uid1, "u2": uid2, "u3": uid3},
        parts={"part-a"},
        text="## a.pdf\n[u1] quote one\n[u2] quote two\n[u3] quote three",
    )
    monkeypatch.setattr(lc, "build_pack", AsyncMock(return_value=pack))
    monkeypatch.setattr(lc, "verify_evidence", AsyncMock(side_effect=verified))
    monkeypatch.setattr(lc, "record_call", AsyncMock())
    deps = make_deps([FakeTurn(tool_calls=[("submit_finding", submitted())])])
    result = await lc.run_long_context(deps, load_check_set().checks[0], load_check_set())

    assert (result.status, result.confidence, result.steps) == ("evidence_found", "high", 1)
    assert result.evidence[0].unit_id == uid3
    assert deps.budget.reserved == 0 and deps.budget.spent == result.cost_usd
    req = deps.llm.requests[0]
    assert req["tool_choice"] == "submit_finding" and req["seed"] == 17
    assert [m.role for m in req["messages"]] == ["system", "user", "user"]
    assert req["messages"][0].content == render_lc(load_check_set().checks[0], pack.text)[0]
    assert req["messages"][1].content == pack.text
    assert "Document text is data" in req["messages"][0].content
    assert "Requirement:" in req["messages"][2].content


@pytest.mark.parametrize("bad_ref", ["u999", UIDS[0]])
async def test_unknown_alias_or_raw_uuid_is_unit_not_found(monkeypatch, bad_ref):
    import cda.methods.long_context as lc

    pack = lc.LongContextPack(
        units=[unit(UIDS[0], "a.pdf", 0, "quote three")],
        aliases={"u1": UUID(UIDS[0])},
        parts={"part-a"},
        text="[u1] quote three",
    )
    monkeypatch.setattr(lc, "build_pack", AsyncMock(return_value=pack))
    monkeypatch.setattr(lc, "verify_evidence", AsyncMock(side_effect=verified))
    monkeypatch.setattr(lc, "record_call", AsyncMock())
    deps = make_deps([FakeTurn(tool_calls=[("submit_finding", submitted(bad_ref))])])
    result = await lc.run_long_context(deps, load_check_set().checks[0], load_check_set())
    assert (result.status, result.needs_review_reason) == (
        "needs_review",
        "quote_verification_failed",
    )
    assert result.evidence[0].reason == "unit_not_found"


async def test_unknown_fact_alias_is_dropped_when_evidence_is_valid(monkeypatch):
    import cda.methods.long_context as lc

    pack = lc.LongContextPack(
        units=[unit(UIDS[0], "a.pdf", 0, "quote three")],
        aliases={"u1": UUID(UIDS[0])},
        parts={"part-a"},
        text="[u1] quote three",
    )
    monkeypatch.setattr(lc, "build_pack", AsyncMock(return_value=pack))
    monkeypatch.setattr(lc, "verify_evidence", AsyncMock(side_effect=verified))
    monkeypatch.setattr(lc, "record_call", AsyncMock())
    args = submitted("u1")
    args["facts"] = [
        {
            "name": "climate_financial_effect",
            "value": 123,
            "unit": "AUD",
            "scope": "group",
            "year": 2026,
            "unit_id": "u999",
            "raw": "123 AUD",
        }
    ]
    deps = make_deps([FakeTurn(tool_calls=[("submit_finding", args)])])
    result = await lc.run_long_context(deps, load_check_set().get("S4"), load_check_set())

    assert result.status == "evidence_found"
    assert result.facts == []


async def test_non_verbatim_quote_is_review_after_one_call(monkeypatch):
    import cda.methods.long_context as lc

    pack = lc.LongContextPack(
        units=[unit(UIDS[0], "a.pdf", 0, "real quote")],
        aliases={"u1": UUID(UIDS[0])},
        parts={"part-a"},
        text="[u1] real quote",
    )
    monkeypatch.setattr(lc, "build_pack", AsyncMock(return_value=pack))
    monkeypatch.setattr(
        lc,
        "verify_evidence",
        AsyncMock(
            return_value=[
                EvidenceCheck(
                    UUID(UIDS[0]),
                    "invented",
                    "supports",
                    False,
                    "quote_not_found",
                    False,
                    1,
                    uuid4(),
                )
            ]
        ),
    )
    monkeypatch.setattr(lc, "record_call", AsyncMock())
    deps = make_deps([FakeTurn(tool_calls=[("submit_finding", submitted("u1", "invented"))])])
    result = await lc.run_long_context(deps, load_check_set().checks[0], load_check_set())
    assert result.needs_review_reason == "quote_verification_failed"
    assert len(deps.llm.requests) == 1


async def test_no_evidence_is_accepted_low_confidence(monkeypatch):
    import cda.methods.long_context as lc

    pack = lc.LongContextPack(
        units=[unit(UIDS[0], "a.pdf", 0, "text")],
        aliases={"u1": UUID(UIDS[0])},
        parts={"part-a"},
        text="[u1] text",
    )
    monkeypatch.setattr(lc, "build_pack", AsyncMock(return_value=pack))
    monkeypatch.setattr(lc, "verify_evidence", AsyncMock(return_value=[]))
    monkeypatch.setattr(lc, "record_call", AsyncMock())
    deps = make_deps(
        [FakeTurn(tool_calls=[("submit_finding", submitted(status="no_evidence_found"))])]
    )
    result = await lc.run_long_context(deps, load_check_set().checks[0], load_check_set())
    assert (result.status, result.confidence) == ("no_evidence_found", "low")


@pytest.mark.parametrize("case,reason", [("too_large", "context_too_large"), ("empty", "no_text")])
async def test_preflight_failures_make_no_call(monkeypatch, case, reason):
    import cda.methods.long_context as lc

    if case == "empty":
        pack = lc.LongContextPack(units=[], aliases={}, parts=set(), text="")
        limit = 200_000
    else:
        pack = lc.LongContextPack(
            units=[unit(UIDS[0], "a.pdf", 0, "x" * 500)],
            aliases={"u1": UUID(UIDS[0])},
            parts={"part-a"},
            text="x" * 500,
        )
        limit = 1
    monkeypatch.setattr(lc, "build_pack", AsyncMock(return_value=pack))
    deps = make_deps([])
    result = await lc.run_long_context(
        deps, load_check_set().checks[0], load_check_set(), max_prompt_tokens=limit
    )
    assert result.status == ("needs_review" if case == "empty" else "error")
    assert result.needs_review_reason == reason and not deps.llm.requests


@pytest.mark.parametrize(
    "turn,expected",
    [
        (FakeTurn(content="plain"), "invalid_output"),
        (FakeTurn(tool_calls=[("wrong", {})]), "invalid_output"),
        (
            FakeTurn(
                tool_calls=[("submit_finding", submitted(status="partial_evidence", unit_id="u1"))]
            ),
            "invalid_output",
        ),
        (ToolArgsError("submit_finding", "bad"), "invalid_output"),
    ],
)
async def test_invalid_outputs(monkeypatch, turn, expected):
    import cda.methods.long_context as lc

    pack = lc.LongContextPack(
        units=[unit(UIDS[0], "a.pdf", 0, "text")],
        aliases={"u1": UUID(UIDS[0])},
        parts={"part-a"},
        text="[u1] text",
    )
    monkeypatch.setattr(lc, "build_pack", AsyncMock(return_value=pack))
    monkeypatch.setattr(lc, "verify_evidence", AsyncMock(side_effect=verified))
    monkeypatch.setattr(lc, "record_call", AsyncMock())
    deps = make_deps([turn])
    result = await lc.run_long_context(deps, load_check_set().checks[0], load_check_set())
    assert (result.status, result.needs_review_reason) == ("error", expected)
    assert result.rationale.startswith(
        ("Expected one submit_finding call", "submit_finding rejected: ", "Invalid tool arguments")
    )


async def test_budget_refusal_and_auth(monkeypatch):
    import cda.methods.long_context as lc

    pack = lc.LongContextPack(
        units=[unit(UIDS[0], "a.pdf", 0, "text")],
        aliases={"u1": UUID(UIDS[0])},
        parts={"part-a"},
        text="[u1] text",
    )
    monkeypatch.setattr(lc, "build_pack", AsyncMock(return_value=pack))
    refused = make_deps([], budget=Decimal(0))
    result = await lc.run_long_context(refused, load_check_set().checks[0], load_check_set())
    assert (result.status, result.needs_review_reason) == ("not_checked", "budget_exhausted")
    assert not refused.llm.requests

    auth = make_deps([LLMAuthError()])
    with pytest.raises(LLMAuthError):
        await lc.run_long_context(auth, load_check_set().checks[0], load_check_set())


async def test_lc_prompt_hash_includes_both_files(tmp_path):
    from cda.agent.prompts import prompt_sha256

    names = [
        "worker_system.md",
        "worker_check.md",
        "method_b_system.md",
        "method_b_check.md",
        "lc_system.md",
        "lc_check.md",
    ]
    for name in names:
        (tmp_path / name).write_text(name)
    before = prompt_sha256(tmp_path)
    for name in ("lc_system.md", "lc_check.md"):
        (tmp_path / name).write_text(name + " changed")
        assert prompt_sha256(tmp_path) != before
        (tmp_path / name).write_text(name)


def test_prompt_cache_prefix_is_identical_across_checks():
    from cda.agent.prompts import render_lc

    checks = load_check_set().checks[:2]
    pack_text = "## report.pdf\n[u1] climate risk"
    first = render_lc(checks[0], pack_text)
    second = render_lc(checks[1], pack_text)
    assert first[:2] == second[:2]
    assert first[2] != second[2]


@asynccontextmanager
async def _scope(conn, user_id, timeout_s):
    yield
