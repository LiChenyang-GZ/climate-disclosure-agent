import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import create_async_engine

from cda.agent.checkpoint import open_checkpointer
from cda.db.models import CheckSet, Document, Pack, Run, Team, Unit, User


@pytest.fixture
async def run_env(pg_url):
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", pg_url.replace("%", "%%"))
    await asyncio.to_thread(command.upgrade, config, "head")
    engine = create_async_engine(pg_url)
    user, team, pack, document, unit, rid = [uuid4() for _ in range(6)]
    version = str(uuid4())
    async with engine.begin() as c:
        await c.execute(
            insert(User).values(id=user, handle=str(user), role="lead", display_name="Test")
        )
        await c.execute(insert(Team).values(id=team, name=str(team)))
        await c.execute(
            insert(Pack).values(
                id=pack, company="Test", reporting_year=2026, team_id=team, created_by=user
            )
        )
        await c.execute(insert(CheckSet).values(version=version, yaml_sha256="a" * 64))
        await c.execute(
            insert(Document).values(
                id=document,
                pack_id=pack,
                kind="annual",
                filename="test.pdf",
                sha256="a" * 64,
                page_count=1,
                parser_version="test",
            )
        )
        await c.execute(
            insert(Unit).values(
                id=unit,
                document_id=document,
                kind="paragraph",
                seq=0,
                pdf_page=1,
                section_path="Climate",
                text="SECRET QUOTE",
                parser_version="test",
            )
        )
        await c.execute(
            insert(Run).values(
                id=rid,
                pack_id=pack,
                method="A",
                model_key="sol",
                check_set_version=version,
                prompt_sha256="a" * 64,
                budget_usd=1,
                status="queued",
                idempotency_key=str(rid),
                created_by=user,
            )
        )
    try:
        async with open_checkpointer(pg_url) as saver:
            deps = SimpleNamespace(embedder=None, emit=None, checkpointer=saver)
            yield engine, rid, deps, SimpleNamespace(unit=unit), user
    finally:
        async with engine.begin() as c:
            await c.execute(text("TRUNCATE users, teams, check_sets CASCADE"))
        await engine.dispose()
