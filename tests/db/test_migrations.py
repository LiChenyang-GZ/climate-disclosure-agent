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
