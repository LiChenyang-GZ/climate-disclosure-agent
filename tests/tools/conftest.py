from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
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
