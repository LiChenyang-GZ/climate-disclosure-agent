from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from sqlalchemy import insert, select

from cda.db.models import DocumentPart, Pack, Team, Unit, User
from cda.parsing.parts import load_parts_file, replace_parts, resolve_part
from cda.parsing.store import ingest_pdf
from cda.synth.answer_key import AnswerKey
from cda.synth.wattlebrook import build_pack


@pytest.mark.db
async def test_stored_evidence_parts(db, tmp_path: Path):
    build_pack(tmp_path)
    parts = load_parts_file(tmp_path / "parts.yaml")
    key = AnswerKey.model_validate(
        yaml.safe_load((tmp_path / "answer_key.yaml").read_text(encoding="utf-8"))
    )
    team = (await db.execute(insert(Team).values(name="Synthetic").returning(Team.id))).scalar_one()
    user = (
        await db.execute(
            insert(User)
            .values(handle=str(uuid4()), role="lead", display_name="Synthetic")
            .returning(User.id)
        )
    ).scalar_one()
    pack = (
        await db.execute(
            insert(Pack)
            .values(
                company="Wattlebrook Energy Limited",
                reporting_year=2026,
                team_id=team,
                created_by=user,
            )
            .returning(Pack.id)
        )
    ).scalar_one()
    for doc in parts.documents:
        doc_id = await ingest_pdf(db, pack_id=pack, kind=doc.kind, path=tmp_path / doc.filename)
        await replace_parts(db, doc_id, doc.parts)
        stored_parts = (
            await db.execute(
                select(
                    DocumentPart.kind,
                    DocumentPart.first_pdf_page,
                    DocumentPart.last_pdf_page,
                    DocumentPart.label,
                ).where(DocumentPart.document_id == doc_id)
            )
        ).all()
        pages = set(
            (await db.execute(select(Unit.pdf_page).where(Unit.document_id == doc_id))).scalars()
        )
        for cell in key.cells:
            for e in cell.evidence:
                if e.document == doc.filename:
                    assert e.pdf_page in pages
                    assert resolve_part(stored_parts, doc.kind, e.pdf_page).kind == e.part
