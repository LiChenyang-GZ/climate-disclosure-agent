import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine


def config(pg_url):
    cfg = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", pg_url.replace("%", "%%"))
    return cfg


def test_migration_roundtrip(pg_url):
    cfg = config(pg_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0003_document_parts")

    async def inspect_state():
        engine = create_async_engine(pg_url)
        async with engine.connect() as conn:
            cols = await conn.run_sync(lambda c: inspect(c).get_columns("units"))
            body = (
                await conn.execute(
                    text("SELECT prosrc FROM pg_proc WHERE proname='reject_unit_mutation'")
                )
            ).scalar_one()
        await engine.dispose()
        return cols, body

    _, previous = asyncio.run(inspect_state())
    command.upgrade(cfg, "head")
    cols, body = asyncio.run(inspect_state())
    assert next(c for c in cols if c["name"] == "seq")["nullable"] is False
    assert "OLD.seq IS DISTINCT FROM NEW.seq" in body
    command.downgrade(cfg, "0003_document_parts")
    cols, body = asyncio.run(inspect_state())
    assert "seq" not in {c["name"] for c in cols} and body == previous
    command.upgrade(cfg, "head")


def test_migration_existing_rows_requires_reparse(pg_url):
    cfg = config(pg_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0003_document_parts")

    async def seed_or_cleanup(clean=False):
        engine = create_async_engine(pg_url)
        async with engine.begin() as conn:
            if clean:
                for table in ["units", "documents", "packs", "teams", "users"]:
                    await conn.execute(text(f"DELETE FROM {table}"))
            else:
                user = (
                    await conn.execute(
                        text(
                            "INSERT INTO users(handle,role,display_name) VALUES ('migration','lead','Migration') RETURNING id"
                        )
                    )
                ).scalar_one()
                team = (
                    await conn.execute(
                        text("INSERT INTO teams(name) VALUES ('Migration') RETURNING id")
                    )
                ).scalar_one()
                pack = (
                    await conn.execute(
                        text(
                            "INSERT INTO packs(company,reporting_year,team_id,created_by) VALUES ('Migration',2026,:team,:user) RETURNING id"
                        ),
                        {"team": team, "user": user},
                    )
                ).scalar_one()
                doc = (
                    await conn.execute(
                        text(
                            "INSERT INTO documents(pack_id,kind,filename,sha256,page_count,parser_version) VALUES (:pack,'other','a.pdf',repeat('f',64),1,'v1') RETURNING id"
                        ),
                        {"pack": pack},
                    )
                ).scalar_one()
                await conn.execute(
                    text(
                        "INSERT INTO units(document_id,kind,pdf_page,section_path,text,parser_version) VALUES (:doc,'paragraph',1,'','body','v1')"
                    ),
                    {"doc": doc},
                )
        await engine.dispose()

    try:
        asyncio.run(seed_or_cleanup())
        with pytest.raises(Exception, match="re-parse required: units has rows without seq"):
            command.upgrade(cfg, "head")
    finally:
        asyncio.run(seed_or_cleanup(True))
        command.upgrade(cfg, "head")
