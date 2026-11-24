from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


def _alembic_config(pg_url: str) -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", pg_url.replace("%", "%%"))
    return config


def test_migration_requires_database_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src/cda/db/migrations"))

    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        command.upgrade(config, "head")


@pytest.mark.db
def test_upgrade_enables_vector_and_vector_column(pg_url: str) -> None:
    command.upgrade(_alembic_config(pg_url), "head")

    async def inspect_database() -> None:
        engine = create_async_engine(pg_url)
        async with engine.begin() as connection:
            extension = await connection.execute(
                text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
            )
            assert extension.scalar_one() == "vector"
            await connection.execute(text("CREATE TEMP TABLE vector_probe (embedding vector(3))"))
        await engine.dispose()

    import asyncio

    asyncio.run(inspect_database())


@pytest.mark.db
def test_downgrade_removes_vector_and_upgrade_succeeds_again(pg_url: str) -> None:
    config = _alembic_config(pg_url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    async def inspect_database() -> None:
        engine = create_async_engine(pg_url)
        async with engine.connect() as connection:
            extension = await connection.execute(
                text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
            )
            assert extension.scalar_one_or_none() is None
        await engine.dispose()

    import asyncio

    asyncio.run(inspect_database())
    command.upgrade(config, "head")


@pytest.mark.db
def test_upgrade_head_twice_is_noop(pg_url: str) -> None:
    config = _alembic_config(pg_url)

    command.upgrade(config, "head")
    command.upgrade(config, "head")


@pytest.mark.db
def test_llm_call_response_column(pg_url: str) -> None:
    # D89: calls recorded before 0012 stay NULL; downgrade to 0011 and back.
    config = _alembic_config(pg_url)
    command.upgrade(config, "head")
    command.downgrade(config, "0011_proposed_status")
    command.upgrade(config, "head")

    async def inspect_database() -> None:
        engine = create_async_engine(pg_url)
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT is_nullable, column_default, data_type FROM information_schema.columns"
                        " WHERE table_name = 'llm_calls' AND column_name = 'response'"
                    )
                )
            ).one()
        await engine.dispose()
        assert tuple(row) == ("YES", None, "jsonb")

    import asyncio

    asyncio.run(inspect_database())


@pytest.mark.db
def test_proposed_status_column_and_constraint(pg_url: str) -> None:
    # D88: findings stored before 0011 stay NULL; downgrade removes column and constraint.
    config = _alembic_config(pg_url)
    command.upgrade(config, "head")
    command.downgrade(config, "0010_run_settings")
    command.upgrade(config, "head")

    async def inspect_database() -> None:
        engine = create_async_engine(pg_url)
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT is_nullable, column_default, data_type FROM information_schema.columns"
                        " WHERE table_name = 'findings' AND column_name = 'proposed_status'"
                    )
                )
            ).one()
            check = (
                await connection.execute(
                    text(
                        "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                        " WHERE conname = 'ck_findings_proposed_status'"
                    )
                )
            ).scalar_one()
        await engine.dispose()
        assert tuple(row) == ("YES", None, "text")
        assert "needs_review" not in check and "partial_evidence" in check

    import asyncio

    asyncio.run(inspect_database())


@pytest.mark.db
def test_run_settings_column_is_nullable_without_default(pg_url: str) -> None:
    # D87: runs recorded before 0010 stay NULL ("not recorded"), never a guessed default.
    config = _alembic_config(pg_url)
    command.upgrade(config, "head")
    command.downgrade(config, "0009_salesforce")
    command.upgrade(config, "head")

    async def inspect_database() -> None:
        engine = create_async_engine(pg_url)
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT is_nullable, column_default, data_type FROM information_schema.columns"
                        " WHERE table_name = 'runs' AND column_name = 'settings'"
                    )
                )
            ).one()
        await engine.dispose()
        assert tuple(row) == ("YES", None, "jsonb")

    import asyncio

    asyncio.run(inspect_database())
