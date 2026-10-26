import asyncio

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from cda.api.app import create_app
from cda.api.seed import seed
from cda.config import Settings
from cda.indexing.embed import HashEmbedder


@pytest.fixture
async def api(pg_url, tmp_path):
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", pg_url.replace("%", "%%"))
    await asyncio.to_thread(command.upgrade, config, "head")
    engine = create_async_engine(pg_url)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE users, teams, check_sets, audit_log CASCADE"))
    await seed(engine)
    app = create_app(
        engine=engine,
        embedder=HashEmbedder(),
        settings=Settings(cda_upload_dir=tmp_path, cda_max_upload_mb=1),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        headers={"X-Demo-User": "sarah"},
    ) as client:
        yield client, engine, app
    # Leave no demo users behind for later suites that seed their own.
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE users, teams, check_sets, audit_log CASCADE"))
    await engine.dispose()
