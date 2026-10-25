import asyncio
import hashlib
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from cda.db.models import Document, DocumentPart, Unit
from cda.parsing.parts import Part, replace_parts
from cda.parsing.pdf import parse_pdf
from cda.parsing.store import ingest_pdf


def _alembic_config(pg_url):
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", pg_url.replace("%", "%%"))
    return config


pytestmark = pytest.mark.db


@pytest.mark.parametrize(
    "column,value,constraint",
    [
        ("first_pdf_page", 0, "ck_document_parts_first_pdf_page"),
        ("last_pdf_page", 0, "ck_document_parts_range"),
        ("kind", "climate", "ck_document_parts_kind"),
    ],
)
async def test_part_constraints(db, rows, column, value, constraint):
    values = {
        "document": rows["document"],
        "kind": "annual",
        "first_pdf_page": 1,
        "last_pdf_page": 1,
    }
    values[column] = value
    with pytest.raises(DBAPIError, match=constraint):
        async with db.begin_nested():
            await db.execute(
                text(
                    "INSERT INTO document_parts(document_id,kind,first_pdf_page,last_pdf_page) VALUES (:document,:kind,:first_pdf_page,:last_pdf_page)"
                ),
                values,
            )


async def test_half_constraint(db, rows):
    with pytest.raises(DBAPIError, match="ck_units_page_half"):
        async with db.begin_nested():
            await db.execute(
                text(
                    "INSERT INTO units(document_id,kind,pdf_page,page_half,section_path,text,parser_version) VALUES (:document,'paragraph',1,'middle','','body','v1')"
                ),
                rows,
            )
    for half in [None, "left"]:
        await db.execute(
            text(
                "INSERT INTO units(document_id,kind,pdf_page,page_half,section_path,text,parser_version) VALUES (:document,'paragraph',1,:half,'','body','v1')"
            ),
            dict(rows, half=half),
        )


async def test_half_immutable(db, rows):
    with pytest.raises(DBAPIError, match="immutable"):
        async with db.begin_nested():
            await db.execute(text("UPDATE units SET page_half='left' WHERE id=:unit"), rows)
    await db.execute(
        text("UPDATE units SET embedding=:embedding WHERE id=:unit"),
        dict(rows, embedding="[" + ",".join(["0"] * 384) + "]"),
    )


async def test_replace_parts(db, rows):
    with pytest.raises(ValueError):
        await replace_parts(
            db, rows["document"], [Part(kind="annual", first_pdf_page=1, last_pdf_page=2)]
        )
    assert (await db.execute(select(DocumentPart.id))).all() == []
    await replace_parts(
        db, rows["document"], [Part(kind="annual", first_pdf_page=1, last_pdf_page=1)]
    )
    await replace_parts(
        db, rows["document"], [Part(kind="other", first_pdf_page=1, last_pdf_page=1)]
    )
    assert (await db.execute(select(DocumentPart.kind))).scalars().all() == ["other"]


async def test_ingest_and_duplicate(db, rows, tmp_path):
    import pymupdf

    path = tmp_path / "generated.pdf"
    with pymupdf.open() as doc:
        for _ in range(3):
            p = doc.new_page()
            p.insert_text((30, 100), "Climate", fontsize=20)
            p.insert_text((30, 150), "Body paragraph here.", fontsize=10)
        doc.save(path)
    document = await ingest_pdf(db, pack_id=rows["pack"], kind="annual", path=path)
    row = (await db.execute(select(Document.__table__).where(Document.id == document))).one()
    assert row.sha256 == hashlib.sha256(path.read_bytes()).hexdigest() and row.page_count == 3
    units = (await db.execute(select(Unit.__table__).where(Unit.document_id == document))).all()
    assert len(units) == len(parse_pdf(path).units)
    for unit in units:
        if unit.parent_id:
            assert any(p.id == unit.parent_id and p.kind == "section" for p in units)
    with pytest.raises(IntegrityError):
        await ingest_pdf(db, pack_id=rows["pack"], kind="annual", path=path)
    assert len(
        (await db.execute(select(Unit.id).where(Unit.document_id == document))).all()
    ) == len(units)


def test_roundtrip(pg_url):
    from sqlalchemy.ext.asyncio import create_async_engine

    config = _alembic_config(pg_url)
    command.upgrade(config, "head")
    command.downgrade(config, "0002_schema")

    async def check():
        engine = create_async_engine(pg_url)
        async with engine.begin() as conn:
            assert (
                await conn.execute(text("SELECT to_regclass('document_parts')"))
            ).scalar() is None
            assert not (
                await conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.columns WHERE table_name='units' AND column_name='page_half'"
                    )
                )
            ).all()
            body = (
                await conn.execute(
                    text("SELECT prosrc FROM pg_proc WHERE proname='reject_unit_mutation'")
                )
            ).scalar_one()
            assert "OLD.text IS DISTINCT FROM NEW.text" in body
            assert "page_half" not in body
            user = (
                await conn.execute(
                    text(
                        "INSERT INTO users(handle,role,display_name) VALUES ('roundtrip','lead','Roundtrip') RETURNING id"
                    )
                )
            ).scalar_one()
            team = (
                await conn.execute(
                    text("INSERT INTO teams(name) VALUES ('Roundtrip') RETURNING id")
                )
            ).scalar_one()
            pack = (
                await conn.execute(
                    text(
                        "INSERT INTO packs(company,reporting_year,team_id,created_by) VALUES ('Roundtrip',2026,:team,:user) RETURNING id"
                    ),
                    {"team": team, "user": user},
                )
            ).scalar_one()
            document = (
                await conn.execute(
                    text(
                        "INSERT INTO documents(pack_id,kind,filename,sha256,page_count,parser_version) VALUES (:pack,'annual','test.pdf',repeat('d',64),1,'v1') RETURNING id"
                    ),
                    {"pack": pack},
                )
            ).scalar_one()
            unit = (
                await conn.execute(
                    text(
                        "INSERT INTO units(document_id,kind,pdf_page,section_path,text,parser_version) VALUES (:doc,'paragraph',1,'','body','v1') RETURNING id"
                    ),
                    {"doc": document},
                )
            ).scalar_one()
            with pytest.raises(DBAPIError, match="immutable"):
                async with conn.begin_nested():
                    await conn.execute(
                        text("UPDATE units SET text='changed' WHERE id=:unit"), {"unit": unit}
                    )
            await conn.rollback()
        await engine.dispose()

    asyncio.run(check())
    command.upgrade(config, "head")
