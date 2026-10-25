import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from cda.db.models import Base

pytestmark = pytest.mark.db


@pytest.mark.parametrize(
    ("table", "column", "key"),
    [
        ("runs", "status", "run"),
        ("documents", "kind", "document"),
        ("units", "kind", "unit"),
        ("findings", "status", "finding"),
        ("evidence", "role", "evidence"),
        ("inconsistencies", "type", "inconsistency"),
    ],
)
async def test_invalid_allowed_value(
    db: AsyncConnection, rows: dict[str, object], table: str, column: str, key: str
) -> None:
    with pytest.raises(IntegrityError, match=f"ck_{table}_{column}"):
        async with db.begin_nested():
            columns = [
                c.name
                for c in Base.metadata.tables[table].columns
                if c.name != "id" and c.computed is None
            ]
            names = ", ".join(columns)
            values = ", ".join("'done'" if name == column else name for name in columns)
            await db.execute(
                text(f"INSERT INTO {table} ({names}) SELECT {values} FROM {table} WHERE id=:id"),
                {"id": rows[key]},
            )


async def test_duplicate_idempotency_key(db: AsyncConnection, rows: dict[str, object]) -> None:
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(
                text(
                    "INSERT INTO runs(pack_id, method, model_key, check_set_version, prompt_sha256, budget_usd, status, idempotency_key, created_by) SELECT pack_id, method, model_key, check_set_version, prompt_sha256, budget_usd, status, idempotency_key, created_by FROM runs WHERE id=:run"
                ),
                rows,
            )


async def test_duplicate_finding(db: AsyncConnection, rows: dict[str, object]) -> None:
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(
                text(
                    "INSERT INTO findings(run_id, check_id, status, rationale, steps, cost_usd) SELECT run_id, check_id, status, rationale, steps, cost_usd FROM findings WHERE id=:finding"
                ),
                rows,
            )


async def test_document_soft_delete(db: AsyncConnection, rows: dict[str, object]) -> None:
    sql = text(
        "INSERT INTO documents(pack_id, kind, filename, sha256, page_count, parser_version) SELECT pack_id, kind, filename, sha256, page_count, parser_version FROM documents WHERE id=:document"
    )
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(sql, rows)
    await db.execute(text("UPDATE documents SET deleted_at=now() WHERE id=:document"), rows)
    await db.execute(sql, rows)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("text", "'changed'"),
        ("document_id", "gen_random_uuid()"),
        ("parent_id", ":unit"),
        ("kind", "'section'"),
        ("pdf_page", "2"),
        ("printed_page", "'ii'"),
        ("section_path", "'Other'"),
        ("token_count", "4"),
        ("parser_version", "'v2'"),
    ],
)
async def test_immutable_unit(
    db: AsyncConnection, rows: dict[str, object], column: str, value: str
) -> None:
    with pytest.raises(DBAPIError, match="immutable"):
        async with db.begin_nested():
            await db.execute(text(f"UPDATE units SET {column}={value} WHERE id=:unit"), rows)


async def test_embedding_dimensions(db: AsyncConnection, rows: dict[str, object]) -> None:
    with pytest.raises(DBAPIError, match="384"):
        async with db.begin_nested():
            await db.execute(text("UPDATE units SET embedding='[1,2,3]' WHERE id=:unit"), rows)
    await db.execute(
        text(
            "UPDATE units SET embedding=CAST(:vector AS vector), context_header='Climate' WHERE id=:unit"
        ),
        {**rows, "vector": str([0.1] * 384)},
    )


@pytest.mark.parametrize(
    ("decision", "status", "note"),
    [
        ("change_status", "partial_evidence", "too short"),
        ("confirm", "partial_evidence", "A sufficiently long note"),
        ("change_status", None, "A sufficiently long note"),
    ],
)
async def test_invalid_review(
    db: AsyncConnection, rows: dict[str, object], decision: str, status: str | None, note: str
) -> None:
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(
                text(
                    "INSERT INTO reviews(finding_id, reviewer_id, decision, new_status, note) VALUES (:finding, :user, :decision, :status, :note)"
                ),
                {**rows, "decision": decision, "status": status, "note": note},
            )


@pytest.mark.parametrize(
    ("decision", "status", "note"),
    [("change_status", "partial_evidence", "Ten chars!"), ("confirm", None, "")],
)
async def test_valid_review(
    db: AsyncConnection, rows: dict[str, object], decision: str, status: str | None, note: str
) -> None:
    await db.execute(
        text(
            "INSERT INTO reviews(finding_id, reviewer_id, decision, new_status, note) VALUES (:finding, :user, :decision, :status, :note)"
        ),
        {**rows, "decision": decision, "status": status, "note": note},
    )


async def test_pack_delete_restricted(db: AsyncConnection, rows: dict[str, object]) -> None:
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(text("DELETE FROM packs WHERE id=:pack"), rows)


async def test_run_children_cascade(db: AsyncConnection, rows: dict[str, object]) -> None:
    await db.execute(text("INSERT INTO run_events(run_id, type) VALUES (:run, 'progress')"), rows)
    await db.execute(
        text(
            "INSERT INTO llm_calls(run_id, step, model, prompt_sha256, tools_sha256, temperature, cost_usd) VALUES (:run, 1, 'fake', repeat('a',64), repeat('b',64), 0, 0)"
        ),
        rows,
    )
    # Findings restrict run deletion; remove the seeded finding and its references first.
    await db.execute(text("DELETE FROM findings WHERE id=:finding"), rows)
    await db.execute(text("DELETE FROM inconsistencies WHERE id=:inconsistency"), rows)
    await db.execute(text("DELETE FROM runs WHERE id=:run"), rows)
    for table in ("run_events", "llm_calls"):
        assert (
            await db.execute(text(f"SELECT count(*) FROM {table} WHERE run_id=:run"), rows)
        ).scalar_one() == 0
