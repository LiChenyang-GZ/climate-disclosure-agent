from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine


def _alembic_config(pg_url: str) -> Config:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", pg_url.replace("%", "%%"))
    return config


@pytest.fixture
async def db(pg_url: str) -> AsyncIterator[AsyncConnection]:
    import asyncio

    await asyncio.to_thread(command.upgrade, _alembic_config(pg_url), "head")
    engine = create_async_engine(pg_url)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        yield connection
        await transaction.rollback()
    await engine.dispose()


@pytest.fixture
async def rows(db: AsyncConnection) -> dict[str, object]:
    statements = {
        "user": "INSERT INTO users(handle, role, display_name) VALUES ('tester', 'lead', 'Tester') RETURNING id",
        "team": "INSERT INTO teams(name) VALUES ('Test') RETURNING id",
        "pack": "INSERT INTO packs(company, reporting_year, team_id, created_by) VALUES ('Test', 2025, :team, :user) RETURNING id",
        "document": "INSERT INTO documents(pack_id, kind, filename, sha256, page_count, parser_version) VALUES (:pack, 'annual', 'a.pdf', repeat('a',64), 1, 'v1') RETURNING id",
        "unit": "INSERT INTO units(document_id, kind, pdf_page, seq, section_path, text, token_count, parser_version) VALUES (:document, 'paragraph', 1, 0, 'Climate', 'Scope 1 emissions', 3, 'v1') RETURNING id",
        "checks": "INSERT INTO check_sets(version, yaml_sha256) VALUES ('v1', repeat('b',64)) RETURNING version",
        "run": "INSERT INTO runs(pack_id, method, model_key, check_set_version, prompt_sha256, budget_usd, status, idempotency_key, created_by) VALUES (:pack, 'A', 'fake', :checks, repeat('c',64), 1, 'queued', 'key', :user) RETURNING id",
        "finding": "INSERT INTO findings(run_id, check_id, status, rationale, steps, cost_usd) VALUES (:run, 'C1', 'evidence_found', 'Found', 1, 0) RETURNING id",
        "evidence": "INSERT INTO evidence(finding_id, unit_id, quote, role) VALUES (:finding, :unit, 'Scope 1 emissions', 'supports') RETURNING id",
        "inconsistency": "INSERT INTO inconsistencies(run_id, type, detail) VALUES (:run, 'T6', '{}') RETURNING id",
    }
    result: dict[str, object] = {}
    for name, sql in statements.items():
        result[name] = (await db.execute(text(sql), result)).scalar_one()
    return result
