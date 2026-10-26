import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.db
async def test_nullable_temperature_migration(pg_url, db, rows):
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", pg_url.replace("%", "%%"))
    # Commit fixture rows so the migration's separate connection can see them.
    await db.execute(
        text("""INSERT INTO llm_calls
        (run_id, step, model, prompt_sha256, tools_sha256, temperature, cost_usd)
        VALUES (:run, 1, 'test', repeat('a',64), repeat('b',64), 0.5, 0)"""),
        rows,
    )
    await db.commit()
    engine = create_async_engine(pg_url)
    try:
        await asyncio.to_thread(command.downgrade, config, "0005_rls")
        await asyncio.to_thread(command.upgrade, config, "head")
        async with engine.begin() as conn:
            assert (
                await conn.execute(text("SELECT temperature FROM llm_calls"))
            ).scalar_one() == 0.5
            await conn.execute(text("UPDATE llm_calls SET temperature = NULL"))
        await asyncio.to_thread(command.downgrade, config, "0005_rls")
        async with engine.begin() as conn:
            assert (await conn.execute(text("SELECT temperature FROM llm_calls"))).scalar_one() == 0
            assert (
                await conn.execute(
                    text("""SELECT is_nullable FROM information_schema.columns
                WHERE table_name='llm_calls' AND column_name='temperature'""")
                )
            ).scalar_one() == "NO"
    finally:
        await asyncio.to_thread(command.upgrade, config, "head")
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM runs WHERE id=:run"), rows)
            await conn.execute(text("DELETE FROM packs WHERE id=:pack"), rows)
            await conn.execute(text("DELETE FROM teams WHERE id=:team"), rows)
            await conn.execute(text("DELETE FROM users WHERE id=:user"), rows)
            await conn.execute(text("DELETE FROM check_sets WHERE version=:checks"), rows)
        await engine.dispose()
