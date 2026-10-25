import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from cda.db.models import Base


def _alembic_config(pg_url: str) -> Config:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", pg_url.replace("%", "%%"))
    return config


pytestmark = pytest.mark.db
TABLES = {
    "users",
    "teams",
    "team_members",
    "packs",
    "documents",
    "units",
    "check_sets",
    "runs",
    "run_events",
    "findings",
    "evidence",
    "facts",
    "inconsistencies",
    "reviews",
    "llm_calls",
    "audit_log",
}


def test_upgrade_downgrade_upgrade(pg_url: str) -> None:
    config = _alembic_config(pg_url)
    command.upgrade(config, "head")

    async def tables() -> set[str]:
        engine = create_async_engine(pg_url)
        async with engine.connect() as conn:
            names = await conn.run_sync(lambda c: inspect(c).get_table_names())
            assert (
                await conn.execute(text("SELECT extname FROM pg_extension WHERE extname='vector'"))
            ).scalar_one() == "vector"
        await engine.dispose()
        return set(names) - {"alembic_version"}

    assert asyncio.run(tables()) == TABLES
    command.downgrade(config, "0001_enable_pgvector")
    assert asyncio.run(tables()) == set()
    command.upgrade(config, "head")
    assert asyncio.run(tables()) == TABLES


async def test_metadata_matches(db: AsyncConnection) -> None:
    differences = await db.run_sync(
        lambda c: compare_metadata(
            MigrationContext.configure(
                c, opts={"compare_type": True, "compare_server_default": True}
            ),
            Base.metadata,
        )
    )
    assert differences == []


async def test_search_and_generated_column(db: AsyncConnection, rows: dict[str, object]) -> None:
    assert (
        await db.execute(
            text("SELECT id FROM units WHERE tsv @@ plainto_tsquery('english', 'scope emissions')")
        )
    ).scalar_one() == rows["unit"]
    await db.execute(
        text("UPDATE units SET context_header='renewable electricity' WHERE id=:unit"), rows
    )
    assert (
        await db.execute(
            text("SELECT tsv @@ plainto_tsquery('english', 'renewable') FROM units WHERE id=:unit"),
            rows,
        )
    ).scalar_one() is True


async def test_event_order(db: AsyncConnection, rows: dict[str, object]) -> None:
    ids = [
        (
            await db.execute(
                text("INSERT INTO run_events(run_id, type) VALUES (:run, 'progress') RETURNING id"),
                rows,
            )
        ).scalar_one()
        for _ in range(3)
    ]
    assert ids[0] < ids[1] < ids[2]


def test_all_models_declared() -> None:
    assert set(Base.metadata.tables) == TABLES
