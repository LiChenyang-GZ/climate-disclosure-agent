"""Idempotent ingestion of a local evaluation pack."""

import hashlib
from pathlib import Path
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from cda.db.models import Document, Pack, Team, TeamMember, User
from cda.eval.answer_key import load_answer_key
from cda.indexing.embed import Embedder
from cda.indexing.index import index_document
from cda.parsing.parts import load_parts_file, replace_parts
from cda.parsing.store import ingest_pdf


async def ingest_pack(
    engine: AsyncEngine,
    folder: str | Path,
    *,
    team_handle: str = "evaluation",
    user_handle: str = "eval",
    embedder: Embedder,
) -> UUID:
    folder = Path(folder)
    key = load_answer_key(folder / "answer_key.yaml")
    parts = load_parts_file(folder / "parts.yaml")
    hashes = {
        hashlib.sha256((folder / d.filename).read_bytes()).hexdigest() for d in parts.documents
    }
    async with engine.begin() as conn:
        await conn.execute(pg_insert(Team).values(name=team_handle).on_conflict_do_nothing())
        tid = (await conn.execute(select(Team.id).where(Team.name == team_handle))).scalar_one()
        await conn.execute(
            pg_insert(User)
            .values(handle=user_handle, role="lead", display_name="Evaluation")
            .on_conflict_do_nothing()
        )
        uid = (await conn.execute(select(User.id).where(User.handle == user_handle))).scalar_one()
        await conn.execute(
            pg_insert(TeamMember).values(team_id=tid, user_id=uid).on_conflict_do_nothing()
        )
        candidates = (
            await conn.scalars(
                select(Pack.id).where(Pack.team_id == tid, Pack.company == key.company)
            )
        ).all()
        for pid in candidates:
            existing = set(
                (
                    await conn.scalars(
                        select(Document.sha256).where(
                            Document.pack_id == pid, Document.deleted_at.is_(None)
                        )
                    )
                ).all()
            )
            if existing == hashes:
                return pid
        pid = (
            await conn.execute(
                insert(Pack)
                .values(company=key.company, reporting_year=2026, team_id=tid, created_by=uid)
                .returning(Pack.id)
            )
        ).scalar_one()
        for doc in parts.documents:
            did = await ingest_pdf(conn, pack_id=pid, kind=doc.kind, path=folder / doc.filename)
            await replace_parts(conn, did, doc.parts)
            await index_document(conn, did, embedder)
        return pid
