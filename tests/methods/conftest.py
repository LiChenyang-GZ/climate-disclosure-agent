import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from cda.agent.checkpoint import open_checkpointer
from cda.agent.prompts import prompt_sha256
from cda.agent.run_graph import RunDeps
from cda.checks.loader import load_check_set
from cda.db.models import CheckSet, Document, DocumentPart, Pack, Run, Team, TeamMember, Unit, User
from cda.indexing.embed import HashEmbedder
from cda.llm.cost import ModelTable
from cda.llm.fake import FakeLLM, FakeTurn
from cda.tools.context import ToolContext


def _alembic_config(pg_url: str) -> Config:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", pg_url.replace("%", "%%"))
    return config


@pytest.fixture
async def db(pg_url: str) -> AsyncIterator[AsyncConnection]:
    await asyncio.to_thread(command.upgrade, _alembic_config(pg_url), "head")
    engine = create_async_engine(pg_url)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        yield connection
        await transaction.rollback()
    await engine.dispose()


@pytest.fixture
async def setup(db):
    user, alex, team, other_team, pack, other_pack, same_pack = [uuid4() for _ in range(7)]
    await db.execute(
        insert(User),
        [
            {"id": user, "handle": "sarah", "role": "lead", "display_name": "Sarah"},
            {"id": alex, "handle": "alex", "role": "lead", "display_name": "Alex"},
        ],
    )
    await db.execute(insert(Team), [{"id": team, "name": "A"}, {"id": other_team, "name": "B"}])
    await db.execute(
        insert(TeamMember),
        [{"team_id": team, "user_id": user}, {"team_id": other_team, "user_id": alex}],
    )
    await db.execute(
        insert(Pack),
        [
            {"id": p, "company": str(p), "reporting_year": 2026, "team_id": t, "created_by": u}
            for p, t, u in [
                (pack, team, user),
                (other_pack, other_team, alex),
                (same_pack, team, user),
            ]
        ],
    )
    annual, other, deleted, foreign, same = [uuid4() for _ in range(5)]
    for i, (d, p) in enumerate(
        [(annual, pack), (other, pack), (deleted, pack), (foreign, other_pack), (same, same_pack)]
    ):
        await db.execute(
            insert(Document).values(
                id=d,
                pack_id=p,
                kind="annual" if i == 0 else "other",
                filename=f"{i}.pdf",
                sha256=str(i) * 64,
                page_count=6,
                parser_version="v1",
            )
        )
    await db.execute(text("UPDATE documents SET deleted_at=now() WHERE id=:id"), {"id": deleted})
    part = uuid4()
    await db.execute(
        insert(DocumentPart).values(
            id=part, document_id=annual, kind="sustainability", first_pdf_page=3, last_pdf_page=5
        )
    )
    units = []
    for d in [annual, other, deleted, foreign, same]:
        for seq in range(6):
            uid = uuid4()
            units.append((d, uid))
            await db.execute(
                insert(Unit).values(
                    id=uid,
                    document_id=d,
                    seq=seq,
                    kind="paragraph",
                    pdf_page=seq + 1,
                    section_path="Climate",
                    text="climate risk " * (2 if seq == 2 else 1),
                    context_header="report > Climate",
                    parser_version="v1",
                    embedding=HashEmbedder().embed_query("climate risk"),
                )
            )
    ctx = ToolContext(db, user, pack, HashEmbedder())
    return {
        "ctx": ctx,
        "alex": alex,
        "annual": annual,
        "other": other,
        "deleted": deleted,
        "foreign": foreign,
        "same": same,
        "part": str(part),
        "units": units,
        "team": team,
    }


@pytest.fixture
async def run_env(pg_url):
    await asyncio.to_thread(command.upgrade, _alembic_config(pg_url), "head")
    engine = create_async_engine(pg_url, pool_size=12, max_overflow=8)
    user, team, pack, doc, unit, rid = [uuid4() for _ in range(6)]
    cs = load_check_set()
    async with engine.begin() as c:
        await c.execute(
            insert(User).values(id=user, handle="runner", role="lead", display_name="R")
        )
        await c.execute(insert(Team).values(id=team, name="Run team"))
        await c.execute(insert(TeamMember).values(team_id=team, user_id=user))
        await c.execute(
            insert(Pack).values(
                id=pack, company="Test", reporting_year=2026, team_id=team, created_by=user
            )
        )
        await c.execute(
            insert(Document).values(
                id=doc,
                pack_id=pack,
                kind="annual",
                filename="report.pdf",
                sha256="a" * 64,
                page_count=1,
                parser_version="test",
            )
        )
        await c.execute(
            insert(Unit).values(
                id=unit,
                document_id=doc,
                kind="paragraph",
                seq=0,
                pdf_page=1,
                section_path="Climate",
                text="climate risk",
                parser_version="test",
                context_header="report > Climate",
                embedding=HashEmbedder().embed_query("climate risk"),
            )
        )
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        await c.execute(
            pg_insert(CheckSet)
            .values(version=cs.version, yaml_sha256=cs.sha256)
            .on_conflict_do_nothing()
        )
        await c.execute(
            insert(Run).values(
                id=rid,
                pack_id=pack,
                method="A",
                model_key="sol",
                check_set_version=cs.version,
                prompt_sha256=prompt_sha256(),
                budget_usd=100,
                status="queued",
                idempotency_key=str(rid),
                created_by=user,
            )
        )
    try:
        async with open_checkpointer(pg_url) as saver:
            llm = FakeLLM(
                [
                    FakeTurn(
                        tool_calls=[
                            (
                                "submit_finding",
                                {
                                    "status": "evidence_found",
                                    "rationale": "Found",
                                    "missing_aspects": [],
                                    "relief_applied": False,
                                    "relief_id": None,
                                    "evidence": [
                                        {
                                            "unit_id": str(unit),
                                            "quote": "climate risk",
                                            "role": "supports",
                                        }
                                    ],
                                    "facts": [],
                                },
                            )
                        ]
                    )
                    for _ in range(2)
                ]
            )
            deps = RunDeps(
                llm=llm, models=ModelTable.load(), embedder=HashEmbedder(), checkpointer=saver
            )
            yield engine, rid, deps, llm, user
    finally:
        async with engine.begin() as c:
            await c.execute(text("TRUNCATE users, teams, check_sets CASCADE"))
            await c.execute(
                text(
                    "TRUNCATE langgraph.checkpoints, langgraph.checkpoint_blobs, "
                    "langgraph.checkpoint_writes"
                )
            )
        await engine.dispose()
