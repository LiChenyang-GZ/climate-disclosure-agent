"""Database permission boundary, independently of application query filters."""

import asyncio
from pathlib import Path
from time import monotonic
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert, text, true
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from cda.db.models import Document, DocumentPart, Pack, Team, TeamMember, Unit, User
from cda.tools.context import ToolContext
from cda.tools.registry import TOOLS, ToolDef, run_tool
from cda.tools.rls import tool_scope

pytestmark = pytest.mark.db
READABLE = ("packs", "team_members", "documents", "document_parts", "units")


@pytest.fixture
async def rows(db):
    result = []
    for handle in ("sarah", "alex"):
        user, team, pack, doc, part, unit = [uuid4() for _ in range(6)]
        await db.execute(
            insert(User).values(id=user, handle=handle, role="lead", display_name=handle)
        )
        await db.execute(insert(Team).values(id=team, name=handle))
        await db.execute(insert(TeamMember).values(team_id=team, user_id=user))
        await db.execute(
            insert(Pack).values(
                id=pack, company=handle, reporting_year=2026, team_id=team, created_by=user
            )
        )
        await db.execute(
            insert(Document).values(
                id=doc,
                pack_id=pack,
                kind="other",
                filename=f"{handle}.pdf",
                sha256=handle[0] * 64,
                page_count=1,
                parser_version="v1",
            )
        )
        await db.execute(
            insert(DocumentPart).values(
                id=part, document_id=doc, kind="other", first_pdf_page=1, last_pdf_page=1
            )
        )
        await db.execute(
            insert(Unit).values(
                id=unit,
                document_id=doc,
                seq=0,
                kind="paragraph",
                pdf_page=1,
                section_path="",
                text=f"climate {handle}",
                parser_version="v1",
            )
        )
        result.append(
            dict(
                zip(
                    ("user", "team", "pack", "doc", "part", "unit"),
                    (user, team, pack, doc, part, unit),
                    strict=True,
                )
            )
        )
    return result


async def counts(db):
    return [await db.scalar(text(f"SELECT count(*) FROM {table}")) for table in READABLE]


async def test_role(db):
    row = (
        await db.execute(
            text(
                "SELECT rolcanlogin, pg_has_role(current_user, oid, 'MEMBER') "
                "FROM pg_roles WHERE rolname='cda_tool'"
            )
        )
    ).one()
    assert row == (False, True)


@pytest.mark.parametrize(
    "table", ["users", "runs", "findings", "evidence", "llm_calls", "reviews", "audit_log", "teams"]
)
async def test_denied_select(db, table):
    with pytest.raises(DBAPIError, match="permission denied"):
        async with tool_scope(db, uuid4(), 1):
            await db.execute(text(f"SELECT * FROM {table}"))


@pytest.mark.parametrize("table", READABLE)
@pytest.mark.parametrize("operation", ["INSERT", "UPDATE", "DELETE"])
async def test_denied_write(db, table, operation):
    column = "user_id" if table == "team_members" else "id"
    sql = {
        "INSERT": f"INSERT INTO {table} DEFAULT VALUES",
        "UPDATE": f"UPDATE {table} SET {column}={column}",
        "DELETE": f"DELETE FROM {table}",
    }[operation]
    with pytest.raises(DBAPIError, match="permission denied"):
        async with tool_scope(db, uuid4(), 1):
            await db.execute(text(sql))


async def test_role_roundtrip(pg_url):
    cfg = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", pg_url.replace("%", "%%"))
    await asyncio.to_thread(command.upgrade, cfg, "head")
    await asyncio.to_thread(command.downgrade, cfg, "0004_unit_seq")
    engine = create_async_engine(pg_url)
    try:
        async with engine.begin() as conn:
            assert (
                await conn.scalar(
                    text("SELECT count(*) FROM pg_policies WHERE policyname='cda_tool_select'")
                )
                == 0
            )
            assert (
                await conn.scalar(
                    text(
                        "SELECT count(*) FROM pg_class WHERE "
                        "relname IN ('packs','team_members','documents','document_parts','units') "
                        "AND relrowsecurity"
                    )
                )
                == 0
            )
            await conn.execute(
                text(
                    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles "
                    "WHERE rolname='cda_tool') THEN CREATE ROLE cda_tool NOLOGIN; END IF; END $$"
                )
            )
        await asyncio.to_thread(command.upgrade, cfg, "head")
        await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        await engine.dispose()


@pytest.mark.parametrize("index", [0, 1], ids=["sarah", "alex"])
async def test_team_visibility(db, rows, index):
    row = rows[index]
    async with tool_scope(db, row["user"], 1):
        for table, key in zip(READABLE, ("pack", "user", "doc", "part", "unit"), strict=True):
            column = "user_id" if table == "team_members" else "id"
            assert (await db.execute(text(f"SELECT {column} FROM {table}"))).scalars().all() == [
                row[key]
            ]


@pytest.mark.parametrize("setting", [None, ""])
async def test_fail_closed(db, rows, setting):
    savepoint = await db.begin_nested()
    try:
        await db.execute(text("SET LOCAL ROLE cda_tool"))
        if setting is not None:
            await db.execute(text("SELECT set_config('cda.user_id', '', true)"))
        assert await counts(db) == [0] * 5
    finally:
        await savepoint.rollback()


async def test_no_teams(db, rows):
    async with tool_scope(db, uuid4(), 1):
        assert await counts(db) == [0] * 5


async def test_removed_membership(db, rows):
    async with tool_scope(db, rows[0]["user"], 1):
        assert await counts(db) == [1] * 5
    await db.execute(text("DELETE FROM team_members WHERE user_id=:u"), {"u": rows[0]["user"]})
    async with tool_scope(db, rows[0]["user"], 1):
        assert await counts(db) == [0] * 5


async def test_owner_unaffected(db, rows):
    assert await counts(db) == [2] * 5
    async with tool_scope(db, rows[0]["user"], 1):
        assert await counts(db) == [1] * 5
    assert await counts(db) == [2] * 5


@pytest.mark.parametrize("raises", [False, True])
async def test_restore_settings(db, rows, raises):
    sql = text(
        "SELECT current_user, current_setting('cda.user_id', true), "
        "current_setting('statement_timeout')"
    )
    before = (await db.execute(sql)).one()

    async def scoped():
        async with tool_scope(db, rows[0]["user"], 0.5):
            inside = (await db.execute(sql)).one()
            assert inside == ("cda_tool", str(rows[0]["user"]), "500ms")
            if raises:
                raise ValueError("propagated")

    if raises:
        with pytest.raises(ValueError, match="propagated"):
            await scoped()
    else:
        await scoped()
    after = (await db.execute(sql)).one()
    assert after[0] == before[0] and after[1] in (None, "") and after[2] == before[2]


async def test_nested_scope(db, rows):
    async with db.begin_nested():
        async with tool_scope(db, rows[0]["user"], 1):
            assert await counts(db) == [1] * 5
        assert await counts(db) == [2] * 5
    assert await db.scalar(text("SELECT 1")) == 1


async def test_statement_timeout(db):
    with pytest.raises(DBAPIError) as exc:
        async with tool_scope(db, uuid4(), 0.01):
            await db.execute(text("SELECT pg_sleep(1)"))
    assert exc.value.orig.sqlstate == "57014"
    assert await db.scalar(text("SELECT 1")) == 1


async def test_dispatch_statement_timeout(db, monkeypatch):
    original_role = await db.scalar(text("SELECT current_user"))

    async def slow(ctx, args):
        await ctx.conn.execute(text("SELECT pg_sleep(3)"))

    monkeypatch.setitem(
        TOOLS, "slow", ToolDef("slow", "", TOOLS["list_documents"].args_model, slow)
    )
    result = await run_tool(ToolContext(db, uuid4(), uuid4(), timeout_s=0.3), "slow", {})
    assert result.category == "timeout" and result.retryable
    assert await db.scalar(text("SELECT 1")) == 1
    assert await db.scalar(text("SELECT current_user")) == original_role
    assert await db.scalar(text("SELECT current_setting('cda.user_id', true)")) in (None, "")


async def test_dispatch_python_timeout(db, monkeypatch):
    async def slow(ctx, args):
        await asyncio.sleep(5)

    monkeypatch.setitem(
        TOOLS, "slow", ToolDef("slow", "", TOOLS["list_documents"].args_model, slow)
    )
    started = monotonic()
    result = await run_tool(ToolContext(db, uuid4(), uuid4(), timeout_s=0.2), "slow", {})
    assert result.category == "timeout" and result.retryable
    assert monotonic() - started >= 1.2
    assert await db.scalar(text("SELECT 1")) == 1


@pytest.mark.parametrize("name", ["keyword_search", "read_unit", "list_documents"])
async def test_rls_without_application_filter(db, rows, monkeypatch, name):
    monkeypatch.setattr("cda.tools.parts_sql.visible_pack_clause", lambda user: true())
    args = {
        "keyword_search": {"query": "climate"},
        "read_unit": {"unit_id": str(rows[0]["unit"])},
        "list_documents": {},
    }[name]
    result = await run_tool(ToolContext(db, rows[1]["user"], rows[0]["pack"]), name, args)
    assert result.category in ("forbidden", "not_found")
    assert "sarah" not in str(result.data)
