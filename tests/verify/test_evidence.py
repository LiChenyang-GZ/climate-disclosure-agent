import warnings
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import event, insert, text, true
from sqlalchemy.exc import SADeprecationWarning

from cda.db.models import Document, Pack, Team, TeamMember, Unit, User
from cda.verify.evidence import EvidenceItem, verify_evidence

pytestmark = pytest.mark.db


@pytest.fixture
async def rows(db):
    sarah, alex, team, other_team, pack, other_pack = [uuid4() for _ in range(6)]
    for user, name, tid in [(sarah, "sarah", team), (alex, "alex", other_team)]:
        await db.execute(insert(User).values(id=user, handle=name, role="lead", display_name=name))
        await db.execute(insert(Team).values(id=tid, name=name))
        await db.execute(insert(TeamMember).values(team_id=tid, user_id=user))
    for pid in [pack, other_pack]:
        await db.execute(
            insert(Pack).values(
                id=pid, company="test", reporting_year=2026, team_id=team, created_by=sarah
            )
        )
    units = []
    docs = []
    for index, pid in enumerate([pack, other_pack, pack, pack]):
        doc = uuid4()
        docs.append(doc)
        await db.execute(
            insert(Document).values(
                id=doc,
                pack_id=pid,
                kind="other",
                filename=f"{index}.pdf",
                sha256=str(index) * 64,
                page_count=2,
                parser_version="v1",
                deleted_at=datetime.now(UTC) if index == 2 else None,
            )
        )
        unit = uuid4()
        units.append(unit)
        await db.execute(
            insert(Unit).values(
                id=unit,
                document_id=doc,
                seq=0,
                kind="paragraph",
                pdf_page=1,
                section_path="",
                text="The climate",
                parser_version="v1",
            )
        )
    for seq, kind, value in [
        (1, "section", "wrong heading"),
        (2, "paragraph", "disclosures follow"),
    ]:
        await db.execute(
            insert(Unit).values(
                id=uuid4(),
                document_id=docs[0],
                seq=seq,
                kind=kind,
                pdf_page=2,
                section_path="",
                text=value,
                parser_version="v1",
            )
        )
    return {"sarah": sarah, "alex": alex, "pack": pack, "units": units, "docs": docs}


async def check(db, rows, unit=None, user=None, quote="The climate"):
    return (
        await verify_evidence(
            db,
            user_id=user or rows["sarah"],
            pack_id=rows["pack"],
            items=[EvidenceItem(unit or rows["units"][0], quote, "supports")],
        )
    )[0]


async def test_valid(db, rows):
    result = await check(db, rows)
    assert result.ok and not result.spans_next
    assert result.pdf_page == 1 and result.document_id == rows["docs"][0]


async def test_no_sqlalchemy_deprecation_warnings(db, rows):
    with warnings.catch_warnings():
        warnings.simplefilter("error", SADeprecationWarning)
        result = await check(db, rows, quote="climate disclosures")
    assert result.ok and result.spans_next


@pytest.mark.parametrize("case", ["invisible", "missing", "other_pack", "deleted"])
async def test_not_found(db, rows, case):
    unit = {
        "invisible": rows["units"][0],
        "missing": uuid4(),
        "other_pack": rows["units"][1],
        "deleted": rows["units"][2],
    }[case]
    result = await check(db, rows, unit, rows["alex"] if case == "invisible" else None)
    assert (result.ok, result.reason, result.spans_next, result.pdf_page, result.document_id) == (
        False,
        "unit_not_found",
        False,
        None,
        None,
    )


async def test_next_skips_section(db, rows):
    result = await check(db, rows, quote="climate disclosures")
    assert result.ok and result.spans_next


async def test_no_other_document(db, rows):
    result = await check(db, rows, rows["units"][3], quote="climate disclosures")
    assert result.reason == "quote_not_found"


async def test_order_duplicates_and_two_queries(db, rows):
    items = [
        EvidenceItem(
            rows["units"][0],
            "climate disclosures" if i % 2 else "bad quote",
            "side_a" if i % 2 else "side_b",
        )
        for i in range(50)
    ]
    statements = []

    def count(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and "set_config" not in statement:
            statements.append(statement)

    event.listen(db.sync_connection, "before_cursor_execute", count)
    try:
        results = await verify_evidence(
            db, user_id=rows["sarah"], pack_id=rows["pack"], items=items
        )
    finally:
        event.remove(db.sync_connection, "before_cursor_execute", count)
    assert len(statements) == 2
    assert len(results) == 50
    assert [(r.unit_id, r.quote, r.role) for r in results] == [
        (i.unit_id, i.quote, i.role) for i in items
    ]
    assert [r.ok for r in results] == [bool(i % 2) for i in range(50)]


async def test_rls_independent(db, rows, monkeypatch):
    monkeypatch.setattr("cda.verify.evidence.visible_pack_clause", lambda user: true())
    assert (await check(db, rows, user=rows["alex"])).reason == "unit_not_found"


async def test_scope_restored(db, rows):
    sql = text("SELECT current_user, current_setting('cda.user_id', true)")
    await db.execute(
        text("SELECT set_config('cda.user_id', :uid, true)"), {"uid": str(rows["alex"])}
    )
    before = (await db.execute(sql)).one()
    await check(db, rows)
    assert (await db.execute(sql)).one() == before
