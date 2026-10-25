from uuid import uuid4

import pytest
from sqlalchemy import event, insert, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from cda.db.models import Document, DocumentPart, Pack, Team, TeamMember, Unit, User
from cda.indexing.embed import HashEmbedder
from cda.tools.context import ToolContext
from cda.tools.registry import run_tool


@pytest.fixture
async def setup(db):
    user, alex, team, other_team, pack, other_pack, same_pack = [uuid4() for _ in range(7)]
    await db.execute(
        insert(User),
        [
            {"id": user, "handle": "sarah", "role": "lead", "display_name": "Sarah"},
            {"id": alex, "handle": "alex", "role": "lead", "display_name": "Alex"},
        ],
    )
    await db.execute(insert(Team), [{"id": team, "name": "A"}, {"id": other_team, "name": "B"}])
    await db.execute(
        insert(TeamMember),
        [{"team_id": team, "user_id": user}, {"team_id": other_team, "user_id": alex}],
    )
    await db.execute(
        insert(Pack),
        [
            {"id": p, "company": str(p), "reporting_year": 2026, "team_id": t, "created_by": u}
            for p, t, u in [
                (pack, team, user),
                (other_pack, other_team, alex),
                (same_pack, team, user),
            ]
        ],
    )
    annual, other, deleted, foreign, same = [uuid4() for _ in range(5)]
    for i, (d, p) in enumerate(
        [(annual, pack), (other, pack), (deleted, pack), (foreign, other_pack), (same, same_pack)]
    ):
        await db.execute(
            insert(Document).values(
                id=d,
                pack_id=p,
                kind="annual" if i == 0 else "other",
                filename=f"{i}.pdf",
                sha256=str(i) * 64,
                page_count=6,
                parser_version="v1",
            )
        )
    await db.execute(text("UPDATE documents SET deleted_at=now() WHERE id=:id"), {"id": deleted})
    part = uuid4()
    await db.execute(
        insert(DocumentPart).values(
            id=part, document_id=annual, kind="sustainability", first_pdf_page=3, last_pdf_page=5
        )
    )
    units = []
    for d in [annual, other, deleted, foreign, same]:
        for seq in range(6):
            uid = uuid4()
            units.append((d, uid))
            await db.execute(
                insert(Unit).values(
                    id=uid,
                    document_id=d,
                    seq=seq,
                    kind="paragraph",
                    pdf_page=seq + 1,
                    section_path="Climate",
                    text="climate risk " * (2 if seq == 2 else 1),
                    context_header="report > Climate",
                    parser_version="v1",
                    embedding=HashEmbedder().embed_query("climate risk"),
                )
            )
    ctx = ToolContext(db, user, pack, HashEmbedder())
    return {
        "ctx": ctx,
        "alex": alex,
        "annual": annual,
        "other": other,
        "deleted": deleted,
        "foreign": foreign,
        "same": same,
        "part": str(part),
        "units": units,
        "team": team,
    }


@pytest.mark.parametrize(
    "name,args",
    [
        ("list_documents", {}),
        ("keyword_search", {"query": "climate"}),
        ("semantic_search", {"query": "climate"}),
        ("read_unit", {"unit_id": str(uuid4())}),
    ],
)
async def test_forbidden(setup, name, args):
    s = setup
    c = s["ctx"]
    c.user_id = s["alex"]
    r = await run_tool(c, name, args)
    assert r.category == "forbidden" and r.message == "Access denied."


async def test_hidden_unit_ids(setup):
    s = setup
    results = []
    for d in [s["foreign"], s["same"], None]:
        uid = next((u for doc, u in s["units"] if doc == d), uuid4())
        results.append((await run_tool(s["ctx"], "read_unit", {"unit_id": str(uid)})).model_dump())
    assert results[0] == results[1] == results[2] and results[0]["category"] == "not_found"


async def test_unknown_document(setup):
    s = setup
    r = await run_tool(
        s["ctx"], "keyword_search", {"query": "climate", "document_ids": [str(s["foreign"])]}
    )
    assert r.category == "bad_args" and "1 unknown" in r.message


async def test_deleted(setup):
    s = setup
    r = await run_tool(s["ctx"], "list_documents", {})
    assert str(s["deleted"]) not in str(r.data)
    r = await run_tool(s["ctx"], "keyword_search", {"query": "climate"})
    assert str(s["deleted"]) not in str(r.data)
    uid = next(u for d, u in s["units"] if d == s["deleted"])
    assert (await run_tool(s["ctx"], "read_unit", {"unit_id": str(uid)})).category == "not_found"


async def test_removed_membership(setup):
    s = setup
    c = s["ctx"]
    await run_tool(c, "list_documents", {})
    await c.conn.execute(text("DELETE FROM team_members WHERE user_id=:u"), {"u": c.user_id})
    assert (await run_tool(c, "list_documents", {})).category == "forbidden"


async def test_parts(setup):
    s = setup
    r = await run_tool(s["ctx"], "list_documents", {})
    docs = r.data["documents"]
    a = next(d for d in docs if d["document_id"] == str(s["annual"]))
    assert len(a["parts"]) == 2
    assert next(p for p in a["parts"] if p["part_id"].endswith(":rest"))["ranges"] == [
        [1, 2],
        [6, 6],
    ]
    assert len(next(d for d in docs if d["document_id"] == str(s["other"]))["parts"]) == 1


async def test_no_rest(setup):
    s = setup
    await s["ctx"].conn.execute(update(DocumentPart).values(first_pdf_page=1, last_pdf_page=6))
    r = await run_tool(s["ctx"], "list_documents", {})
    assert len(r.data["documents"][0]["parts"]) == 1


@pytest.mark.parametrize("scope,pages", [("part", {3, 4, 5}), ("rest", {1, 2, 6})])
async def test_part_filters(setup, scope, pages):
    s = setup
    part = s["part"] if scope == "part" else str(s["annual"]) + ":rest"
    r = await run_tool(s["ctx"], "keyword_search", {"query": "climate", "part_ids": [part]})
    assert {h["pdf_page"] for h in r.data["hits"]} == pages


async def test_unknown_part(setup):
    r = await run_tool(
        setup["ctx"], "keyword_search", {"query": "climate", "part_ids": ["unknown"]}
    )
    assert r.category == "bad_args"


async def test_range_changes(setup):
    s = setup
    c = s["ctx"]
    await c.conn.execute(update(DocumentPart).values(first_pdf_page=2, last_pdf_page=4))
    r = await run_tool(c, "keyword_search", {"query": "climate", "part_ids": [s["part"]]})
    assert {h["pdf_page"] for h in r.data["hits"]} == {2, 3, 4}


async def test_rank(setup):
    r = await run_tool(
        setup["ctx"], "keyword_search", {"query": "climate", "document_ids": [str(setup["annual"])]}
    )
    assert r.data["hits"][0]["pdf_page"] == 3
    assert [h["pdf_page"] for h in r.data["hits"][1:]] == [1, 2, 4, 5, 6]


async def test_no_hits_coverage(setup):
    c = setup["ctx"]
    r = await run_tool(c, "keyword_search", {"query": "absentword"})
    assert r.category == "not_found" and c.coverage.distinct_queries == 1
    assert c.coverage.parts_covered


async def test_semantic(setup):
    s = setup
    r = await run_tool(
        s["ctx"], "semantic_search", {"query": "climate risk", "part_ids": [s["part"]]}
    )
    assert {h["pdf_page"] for h in r.data["hits"]} == {3, 4, 5}
    assert r.data["hits"][0]["score"] == pytest.approx(1)


async def test_neighbours(setup):
    s = setup
    ids = [u for d, u in s["units"] if d == s["annual"]]
    r = await run_tool(s["ctx"], "read_unit", {"unit_id": str(ids[2]), "neighbours": 1})
    assert [u["unit_id"] for u in r.data["units"]] == list(map(str, ids[1:4]))
    r = await run_tool(s["ctx"], "read_unit", {"unit_id": str(ids[0]), "neighbours": 1})
    assert len(r.data["units"]) == 2


async def test_parent_without_parent(setup):
    s = setup
    uid = s["units"][0][1]
    r = await run_tool(s["ctx"], "read_unit", {"unit_id": str(uid), "mode": "parent"})
    assert len(r.data["units"]) == 1


async def test_read_no_coverage(setup):
    s = setup
    await run_tool(s["ctx"], "read_unit", {"unit_id": str(s["units"][0][1])})
    assert s["ctx"].coverage.parts_covered == set()


async def test_missing_parts(setup):
    s = setup
    c = s["ctx"]
    await run_tool(c, "keyword_search", {"query": "climate", "document_ids": [str(s["annual"])]})
    all_parts = {s["part"], str(s["annual"]) + ":rest", str(s["other"])}
    assert c.coverage.missing_parts(all_parts) == {str(s["other"])}
    await run_tool(c, "keyword_search", {"query": "risk"})
    assert not c.coverage.missing_parts(all_parts)


async def test_every_query_membership(setup):
    c = setup["ctx"]
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(c.conn.sync_connection, "before_cursor_execute", capture)
    try:
        for name, args in [
            ("list_documents", {}),
            ("keyword_search", {"query": "climate"}),
            ("semantic_search", {"query": "risk"}),
            ("read_unit", {"unit_id": str(setup["units"][0][1])}),
        ]:
            await run_tool(c, name, args)
    finally:
        event.remove(c.conn.sync_connection, "before_cursor_execute", capture)
    assert statements and all(
        "team_members" in sql and "deleted_at IS NULL" in sql for sql in statements
    )


async def test_seq_immutable(setup):
    with pytest.raises(DBAPIError, match="immutable"):
        async with setup["ctx"].conn.begin_nested():
            await setup["ctx"].conn.execute(
                update(Unit).where(Unit.id == setup["units"][0][1]).values(seq=99)
            )


async def test_seq_unique(setup):
    with pytest.raises(IntegrityError):
        async with setup["ctx"].conn.begin_nested():
            await setup["ctx"].conn.execute(
                insert(Unit).values(
                    document_id=setup["annual"],
                    seq=0,
                    kind="paragraph",
                    pdf_page=1,
                    section_path="",
                    text="x",
                    parser_version="v1",
                )
            )


async def test_sql_parts_match_resolver(setup):
    from types import SimpleNamespace

    from cda.parsing.parts import resolve_part
    from cda.tools.parts_sql import unit_query

    s = setup
    rows = (
        (
            await s["ctx"].conn.execute(
                unit_query(s["ctx"]).where(Unit.document_id == s["annual"]).order_by(Unit.seq)
            )
        )
        .mappings()
        .all()
    )
    part = SimpleNamespace(
        id=s["part"], kind="sustainability", first_pdf_page=3, last_pdf_page=5, label=None
    )
    assert len(rows) == 6
    for row in rows:
        expected = resolve_part([part], "annual", row["pdf_page"])
        assert row["part_kind"] == expected.kind
        assert row["part_id"] == (expected.part_id or str(s["annual"]) + ":rest")


@pytest.mark.parametrize("container_kind", ["table", "section", None])
async def test_parent_section_and_table(setup, container_kind):
    s = setup
    c = s["ctx"]
    doc = uuid4()
    section, paragraph, table, row = [uuid4() for _ in range(4)]
    await c.conn.execute(
        insert(Document).values(
            id=doc,
            pack_id=c.pack_id,
            kind="other",
            filename="parents.pdf",
            sha256="f" * 64,
            page_count=1,
            parser_version="v1",
        )
    )
    for seq, (uid, parent, kind, body) in enumerate(
        [
            (section, None, "section", "Heading"),
            (paragraph, section, "paragraph", "Body"),
            (table, section, "table", "| Header |\n| Row |"),
            (row, table, "table_row", "Row"),
        ]
    ):
        await c.conn.execute(
            insert(Unit).values(
                id=uid,
                document_id=doc,
                parent_id=parent,
                seq=seq,
                kind=kind,
                pdf_page=1,
                section_path="Heading",
                text=body,
                parser_version="v1",
            )
        )
    if container_kind is not None:
        uid = table if container_kind == "table" else section
        result = await run_tool(c, "read_unit", {"unit_id": str(uid), "mode": "parent"})
        expected = [table] if container_kind == "table" else [section, paragraph, table]
        assert [u["unit_id"] for u in result.data["units"]] == list(map(str, expected))
        return
    result = await run_tool(
        c, "read_unit", {"unit_id": str(row), "mode": "parent", "neighbours": 2}
    )
    assert [u["unit_id"] for u in result.data["units"]] == [str(table)]
    assert result.data["units"][0]["text"] == "| Header |\n| Row |"
    result = await run_tool(c, "read_unit", {"unit_id": str(paragraph), "mode": "parent"})
    assert [u["unit_id"] for u in result.data["units"]] == list(
        map(str, [section, paragraph, table])
    )
    result = await run_tool(c, "read_unit", {"unit_id": str(paragraph), "neighbours": 1})
    assert [u["unit_id"] for u in result.data["units"]] == list(map(str, [paragraph, row]))
    result = await run_tool(c, "keyword_search", {"query": "Heading", "document_ids": [str(doc)]})
    assert result.category == "not_found"


async def test_semantic_skips_unembedded(setup):
    s = setup
    c = s["ctx"]
    uid = s["units"][0][1]
    await c.conn.execute(update(Unit).where(Unit.id == uid).values(embedding=None))
    r = await run_tool(
        c, "semantic_search", {"query": "climate risk", "document_ids": [str(s["annual"])]}
    )
    assert str(uid) not in [h["unit_id"] for h in r.data["hits"]]
    r = await run_tool(
        c, "semantic_search", {"query": "climate risk", "document_ids": [str(s["foreign"])]}
    )
    assert r.category == "bad_args"


async def test_repeat_search_not_executed(setup):
    s = setup
    c = s["ctx"]
    await run_tool(c, "keyword_search", {"query": " Climate  risk ", "part_ids": [s["part"]]})
    r = await run_tool(c, "keyword_search", {"query": "climate risk", "part_ids": [s["part"]]})
    assert r.category == "bad_args" and len(c.coverage.queries) == 1
    r = await run_tool(
        c, "keyword_search", {"query": "CLIMATE RISK", "document_ids": [str(s["other"])]}
    )
    assert r.ok and len(c.coverage.queries) == 2 and c.coverage.distinct_queries == 1


async def test_empty_pack(setup):
    s = setup
    c = s["ctx"]
    pack = uuid4()
    await c.conn.execute(
        insert(Pack).values(
            id=pack, company="Empty", reporting_year=2026, team_id=s["team"], created_by=c.user_id
        )
    )
    c.pack_id = pack
    assert (await run_tool(c, "list_documents", {})).category == "not_found"


@pytest.mark.parametrize("tool", ["keyword_search", "semantic_search"])
@pytest.mark.parametrize("scope", ["documents", "parts", "disjoint"])
async def test_empty_search_scope(setup, tool, scope):
    s = setup
    c = s["ctx"]
    filters = {
        "documents": {"document_ids": []},
        "parts": {"part_ids": []},
        "disjoint": {"document_ids": [str(s["other"])], "part_ids": [s["part"]]},
    }
    result = await run_tool(c, tool, {"query": "climate", **filters[scope]})
    assert result.category == "bad_args"
    assert result.message == "Search scope is empty; widen document_ids or part_ids."
    assert c.coverage.distinct_queries == 0
    assert not c.coverage.queries and not c.coverage.parts_covered
    result = await run_tool(c, tool, {"query": "climate", "part_ids": [s["part"]]})
    assert result.category == "found"
    assert c.coverage.distinct_queries == 1


async def test_long_read_tool(setup):
    s = setup
    c = s["ctx"]
    uid = uuid4()
    await c.conn.execute(
        insert(Unit).values(
            id=uid,
            document_id=s["annual"],
            seq=6,
            kind="paragraph",
            pdf_page=6,
            section_path="",
            text="Long evidence. " * 500,
            parser_version="v1",
        )
    )
    r = await run_tool(c, "read_unit", {"unit_id": str(uid), "neighbours": 2})
    assert r.data["truncated"]
    assert r.data["units"][0]["unit_id"] == str(uid)
    assert sum(len(u["text"]) for u in r.data["units"]) <= 4000
    assert r.data["units"][-1]["text"].endswith("\n[truncated]")


async def test_semantic_identical_first(setup):
    s = setup
    c = s["ctx"]
    uid = uuid4()
    body = "unique biodiversity protection"
    await c.conn.execute(
        insert(Unit).values(
            id=uid,
            document_id=s["annual"],
            seq=6,
            kind="paragraph",
            pdf_page=6,
            section_path="",
            text=body,
            parser_version="v1",
            embedding=HashEmbedder().embed_query(body),
        )
    )
    r = await run_tool(c, "semantic_search", {"query": body, "document_ids": [str(s["annual"])]})
    assert r.data["hits"][0]["unit_id"] == str(uid)
    assert r.data["hits"][0]["score"] > r.data["hits"][1]["score"]


async def test_truncated_neighbours_reading_order(setup):
    c = setup["ctx"]
    ids = [uuid4() for _ in range(5)]
    for seq, uid in enumerate(ids, start=6):
        await c.conn.execute(
            insert(Unit).values(
                id=uid,
                document_id=setup["annual"],
                seq=seq,
                kind="paragraph",
                pdf_page=6,
                section_path="",
                text="x" * 3000,
                parser_version="v1",
            )
        )
    result = await run_tool(c, "read_unit", {"unit_id": str(ids[2]), "neighbours": 2})
    units = result.data["units"]
    assert result.data["truncated"]
    assert [u["unit_id"] for u in units] == list(map(str, [ids[0], ids[2]]))
    assert units[1]["text"] == "x" * 3000
    assert sum(len(u["text"]) for u in units) <= 4000
    assert units[0]["text"].endswith("\n[truncated]")
    assert sum(u["text"].count("[truncated]") for u in units) == 1
