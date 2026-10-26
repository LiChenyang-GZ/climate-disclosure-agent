"""D77: OR over safe English lexemes, with existing tool safeguards."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import yaml
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import create_async_engine

from cda.db.models import Document, DocumentPart, Pack, Team, TeamMember, Unit, User
from cda.eval.answer_key import load_answer_key
from cda.eval.pack import ingest_pack
from cda.eval.retrieval_eval import evaluate_retrieval
from cda.indexing.embed import HashEmbedder
from cda.tools.context import ToolContext
from cda.tools.keyword_search import SearchArgs, keyword_search
from cda.tools.registry import run_tool

pytestmark = pytest.mark.db


@pytest.fixture
async def corpus(db):
    user, team, pack, document = [uuid4() for _ in range(4)]
    await db.execute(
        insert(User).values(id=user, handle=str(user), role="lead", display_name="T24")
    )
    await db.execute(insert(Team).values(id=team, name=str(team)))
    await db.execute(insert(TeamMember).values(team_id=team, user_id=user))
    await db.execute(
        insert(Pack).values(
            id=pack, company="T24", reporting_year=2026, team_id=team, created_by=user
        )
    )
    await db.execute(
        insert(Document).values(
            id=document,
            pack_id=pack,
            kind="annual",
            filename="t24.pdf",
            sha256="a" * 64,
            page_count=2,
            parser_version="v1",
        )
    )

    async def add(body, seq, *, doc=document, page=1, uid=None):
        uid = uid or uuid4()
        await db.execute(
            insert(Unit).values(
                id=uid,
                document_id=doc,
                seq=seq,
                kind="paragraph",
                pdf_page=page,
                section_path="",
                text=body,
                parser_version="v1",
            )
        )
        return str(uid)

    return ToolContext(db, user, pack, HashEmbedder()), document, add


async def search(ctx, query, **scope):
    return await run_tool(ctx, "keyword_search", {"query": query, **scope})


async def test_one_of_eight_terms(corpus):
    ctx, _, add = corpus
    uid = await add("climate", 0)
    result = await search(
        ctx, "climate emissions governance strategy metrics targets energy adaptation"
    )
    assert result.ok
    assert [h["unit_id"] for h in result.data["hits"]] == [uid]


async def test_distinct_terms_rank_and_ties(corpus):
    ctx, _, add = corpus
    one = await add("climate", 0)
    four = await add("climate emissions governance strategy", 1)
    tied = await add("emissions", 2)
    result = await search(ctx, "climate emissions governance strategy")
    assert [h["unit_id"] for h in result.data["hits"]] == [four, one, tied]
    assert result.data["hits"][0]["rank"] > result.data["hits"][1]["rank"]
    assert result.data["hits"][1]["rank"] == result.data["hits"][2]["rank"]


async def test_stop_words_not_found(corpus):
    ctx, _, add = corpus
    await add("climate", 0)
    result = await search(ctx, "the and of")
    # D60: not_found is a normal result (ok=True); the spec text said ok=False by mistake.
    assert result.ok and result.category == "not_found"
    assert result.message == "No hits; try other terms."
    assert result.data == {"hits": []}
    assert ctx.coverage.distinct_queries == 1 and ctx.coverage.parts_covered


@pytest.mark.parametrize("symbol", ["'", ":", "&", "|", "!", "(", ")", "<->", "*", "\\"])
async def test_operator_characters_are_plain_text(corpus, symbol):
    ctx, _, add = corpus
    exact = await add("climate", 0)
    await add("climateology", 1)  # A prefix query would also return this unit.
    await add("biodiversity", 2)  # Negation would return this unit.
    result = await search(ctx, f"{symbol}climate{symbol}")
    assert result.ok
    assert [h["unit_id"] for h in result.data["hits"]] == [exact]


async def test_hyphenated_words(corpus):
    ctx, _, add = corpus
    ids = {await add("climate-related", 0), await add("climate related", 1)}
    result = await search(ctx, "climate-related")
    assert {h["unit_id"] for h in result.data["hits"]} == ids


@pytest.mark.parametrize("query,body", [("emissions", "emission"), ("risks", "risk")])
async def test_english_stemming(corpus, query, body):
    ctx, _, add = corpus
    uid = await add(body, 0)
    result = await search(ctx, query)
    assert result.ok and [h["unit_id"] for h in result.data["hits"]] == [uid]


async def test_scope_permission_and_soft_delete(corpus):
    ctx, document, add = corpus
    part = uuid4()
    await ctx.conn.execute(
        insert(DocumentPart).values(
            id=part, document_id=document, kind="sustainability", first_pdf_page=1, last_pdf_page=1
        )
    )
    visible = await add("climate", 0)
    await add("emissions", 1, page=2)
    for foreign in (False, True):
        doc, pack = uuid4(), ctx.pack_id
        if foreign:
            team, pack = uuid4(), uuid4()
            await ctx.conn.execute(insert(Team).values(id=team, name=str(team)))
            await ctx.conn.execute(
                insert(Pack).values(
                    id=pack,
                    company="Other",
                    reporting_year=2026,
                    team_id=team,
                    created_by=ctx.user_id,
                )
            )
        await ctx.conn.execute(
            insert(Document).values(
                id=doc,
                pack_id=pack,
                kind="other",
                filename=f"{doc}.pdf",
                sha256=str(doc).replace("-", "") * 2,
                page_count=1,
                parser_version="v1",
            )
        )
        await add("climate emissions", 0, doc=doc)
        if not foreign:
            await ctx.conn.execute(
                update(Document).where(Document.id == doc).values(deleted_at=func.now())
            )
    result = await search(
        ctx, "climate emissions", document_ids=[str(document)], part_ids=[str(part)]
    )
    assert [h["unit_id"] for h in result.data["hits"]] == [visible]
    result = await search(ctx, "climate emissions")
    assert len(result.data["hits"]) == 2
    assert {h["document_id"] for h in result.data["hits"]} == {str(document)}
    # Membership must also deny direct access to the other team's pack.
    ctx.pack_id = pack
    assert (await search(ctx, "climate")).category == "forbidden"


async def test_normalised_query_rejected(corpus):
    ctx, _, add = corpus
    await add("climate", 0)
    assert (await search(ctx, " Climate   emissions ")).ok
    result = await search(ctx, "climate emissions")
    assert not result.ok and result.category == "bad_args"
    assert "repeated query" in result.message
    assert ctx.coverage.distinct_queries == 1


async def test_wattlebrook_retrieval_improves(db, pg_url):
    # The db fixture migrates first; ingestion uses its own committed transactions.
    engine = create_async_engine(pg_url)
    pack_path = Path("data/synthetic/wattlebrook")
    handle = "t24-" + uuid4().hex
    pid = None
    try:
        pid = await ingest_pack(
            engine, pack_path, embedder=HashEmbedder(), team_handle=handle, user_handle=handle
        )
        async with engine.begin() as conn:
            uid = await conn.scalar(select(Pack.created_by).where(Pack.id == pid))
            result = await evaluate_retrieval(
                ToolContext(conn, uid, pid, HashEmbedder()),
                load_answer_key(pack_path / "answer_key.yaml"),
                ("keyword", "hybrid"),
            )
        baseline = yaml.safe_load(Path("evals/ci_baseline.yaml").read_text())
        # Freeze the recorded AND score: reviewers regenerate the baseline after T24.
        assert result["modes"]["keyword"]["overall"]["Hit@20"] > 0.143
        assert (
            result["modes"]["hybrid"]["overall"]["Hit@20"]
            >= baseline["metrics"]["hybrid"]["hit_at_20"] - 0.02
        )
    finally:
        async with engine.begin() as conn:
            if pid is not None:
                docs = select(Document.id).where(Document.pack_id == pid)
                await conn.execute(delete(Unit).where(Unit.document_id.in_(docs)))
                await conn.execute(delete(Document).where(Document.pack_id == pid))
                await conn.execute(delete(Pack).where(Pack.id == pid))
            await conn.execute(delete(Team).where(Team.name == handle))
            await conn.execute(delete(User).where(User.handle == handle))
        await engine.dispose()


async def test_sql_uses_bound_plain_lexemes(monkeypatch):
    """Check SQL construction without needing a running PostgreSQL server."""
    scope = AsyncMock(return_value={"part"})
    monkeypatch.setattr("cda.tools.keyword_search.search_scope", scope)
    statements = []

    async def execute(stmt):
        statements.append(stmt)
        return SimpleNamespace(mappings=list)

    ctx = SimpleNamespace(conn=SimpleNamespace(execute=execute), pack_id=uuid4(), user_id=uuid4())
    raw = "!climate:* 'emissions' & risk"
    await keyword_search(ctx, SearchArgs(query=raw))
    compiled = statements[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "plainto_tsquery('english'," in sql
    assert "replace(CAST(plainto_tsquery" in sql
    assert " AS TSQUERY)" in sql
    assert raw not in sql and raw in compiled.params.values()
    assert "&" in compiled.params.values() and "|" in compiled.params.values()
    assert "ts_rank_cd" in sql and "@@" in sql
    assert "ORDER BY rank DESC, units.seq, units.id" in sql
