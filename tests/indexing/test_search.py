from uuid import uuid4

import pytest
from sqlalchemy import insert, select, text, update

from cda.db.models import Document, Unit
from cda.indexing.embed import HashEmbedder
from cda.indexing.fts import keyword_units
from cda.indexing.index import IndexStats, index_document
from cda.indexing.vector import vector_units

pytestmark = pytest.mark.db


@pytest.fixture
async def corpus(db, rows):
    doc = rows["document"]
    await db.execute(update(Document).where(Document.id == doc).values(filename="climatefile.pdf"))
    ids = [rows["unit"]]
    for kind, body in [
        ("section", "Scope emissions"),
        ("paragraph", "Scope 1 emissions Scope 1 emissions"),
        ("table", "Scope emissions"),
        ("table_row", "board remuneration"),
    ]:
        uid = uuid4()
        ids.append(uid)
        await db.execute(
            insert(Unit).values(
                id=uid,
                document_id=doc,
                kind=kind,
                pdf_page=1,
                section_path="Governance",
                text=body,
                parser_version="v1",
            )
        )
    return doc, ids


async def test_index_all_headers_and_children(db, corpus):
    doc, ids = corpus
    assert await index_document(db, doc, HashEmbedder()) == IndexStats(5, 3)
    units = (await db.execute(select(Unit.__table__).where(Unit.id.in_(ids)))).mappings().all()
    assert all(u["context_header"].startswith("climatefile.pdf > ") for u in units)
    assert all(
        (u["embedding"] is not None) == (u["kind"] in ("paragraph", "table_row")) for u in units
    )


async def test_index_idempotent(db, corpus):
    doc, _ = corpus
    await index_document(db, doc, HashEmbedder())
    before = (await db.execute(text("SELECT id, xmin::text FROM units ORDER BY id"))).all()
    assert await index_document(db, doc, HashEmbedder()) == IndexStats(0, 0)
    assert before == (await db.execute(text("SELECT id, xmin::text FROM units ORDER BY id"))).all()


async def test_header_search(db, corpus):
    doc, ids = corpus
    await index_document(db, doc, HashEmbedder())
    assert {h.unit_id for h in await keyword_units(db, document_ids=[doc], query="governance")} == {
        ids[2],
        ids[4],
    }
    # PostgreSQL treats the full filename as a file token, rather than its stem.
    assert {
        h.unit_id for h in await keyword_units(db, document_ids=[doc], query="climatefile.pdf")
    } == {ids[0], ids[2], ids[4]}


async def test_bad_dimension_writes_nothing(db, corpus):
    doc, _ = corpus
    with pytest.raises(ValueError):
        await index_document(db, doc, HashEmbedder(3))
    assert not (await db.execute(select(Unit.id).where(Unit.context_header.is_not(None)))).all()


async def test_keyword_ranking_and_kinds(db, corpus):
    doc, ids = corpus
    hits = await keyword_units(db, document_ids=[doc], query="scope emissions")
    assert [h.unit_id for h in hits] == [ids[2], ids[0]]
    assert hits[0].rank > hits[1].rank


async def test_keyword_stopword(db, corpus):
    assert await keyword_units(db, document_ids=[corpus[0]], query="the") == []


@pytest.mark.parametrize("search", ["keyword", "vector"])
@pytest.mark.parametrize("limit", [0, 51])
async def test_invalid_limit(db, search, limit):
    with pytest.raises(ValueError):
        if search == "keyword":
            await keyword_units(db, document_ids=[], query="scope", limit=limit)
        else:
            await vector_units(
                db,
                document_ids=[],
                query_embedding=HashEmbedder().embed_query("scope"),
                limit=limit,
            )


async def test_vector_nearest_and_unembedded(db, corpus):
    doc, ids = corpus
    e = HashEmbedder()
    await index_document(db, doc, e)
    extra = uuid4()
    await db.execute(
        insert(Unit).values(
            id=extra,
            document_id=doc,
            kind="paragraph",
            pdf_page=1,
            section_path="",
            text="board remuneration",
            parser_version="v1",
        )
    )
    hits = await vector_units(
        db, document_ids=[doc], query_embedding=e.embed_query("board remuneration")
    )
    assert hits[0].unit_id == ids[4]
    assert {h.unit_id for h in hits} == {ids[0], ids[2], ids[4]}


@pytest.mark.parametrize("search", ["keyword", "vector"])
async def test_document_filtering(db, corpus, search):
    doc, _ = corpus
    await index_document(db, doc, HashEmbedder())

    async def run(ids):
        if search == "keyword":
            return await keyword_units(db, document_ids=ids, query="scope emissions")
        return await vector_units(
            db, document_ids=ids, query_embedding=HashEmbedder().embed_query("scope emissions")
        )

    assert await run([uuid4()]) == []
    assert await run([]) == []
    assert await run([doc])
    await db.execute(update(Document).where(Document.id == doc).values(deleted_at=text("now()")))
    assert await run([doc]) == []
