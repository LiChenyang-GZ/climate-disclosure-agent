import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine


@pytest.fixture
async def db(pg_url: str) -> AsyncIterator[AsyncConnection]:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", pg_url.replace("%", "%%"))
    await asyncio.to_thread(command.upgrade, config, "head")
    engine = create_async_engine(pg_url)
    try:
        async with engine.connect() as conn, conn.begin():
            yield conn
            await conn.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
async def rows(db):
    statements = {
        "user": "INSERT INTO users(handle, role, display_name) VALUES ('llm-tester', 'lead', 'Tester') RETURNING id",
        "team": "INSERT INTO teams(name) VALUES ('LLM Test') RETURNING id",
        "pack": "INSERT INTO packs(company, reporting_year, team_id, created_by) VALUES ('Test', 2025, :team, :user) RETURNING id",
        "checks": "INSERT INTO check_sets(version, yaml_sha256) VALUES ('llm-v1', repeat('b',64)) RETURNING version",
        "run": "INSERT INTO runs(pack_id, method, model_key, check_set_version, prompt_sha256, budget_usd, status, idempotency_key, created_by) VALUES (:pack, 'A', 'sol', :checks, repeat('c',64), 1, 'queued', 'llm-key', :user) RETURNING id",
    }
    result = {}
    for name, statement in statements.items():
        result[name] = (await db.execute(text(statement), result)).scalar_one()
    return result
