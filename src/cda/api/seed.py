"""Idempotent demo identities and team membership."""

import asyncio

from sqlalchemy import insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from cda.db.models import AuditLog, Team, TeamMember, User
from cda.db.session import make_engine


async def seed(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(202020)"))
        users = {}
        for handle, role, name in [
            ("sarah", "lead", "Sarah Chen"),
            ("alex", "analyst", "Alex Nguyen"),
        ]:
            result = await conn.execute(
                pg_insert(User)
                .values(handle=handle, role=role, display_name=name)
                .on_conflict_do_nothing(index_elements=["handle"])
                .returning(User.id)
            )
            uid = result.scalar_one_or_none()
            if uid is not None:
                await conn.execute(
                    insert(AuditLog).values(
                        actor=handle, action="seed_user", target=str(uid), detail={}
                    )
                )
            users[handle] = (
                await conn.execute(select(User.id).where(User.handle == handle))
            ).scalar_one()
        for name, handles in [
            ("Energy & Resources", ["sarah", "alex"]),
            ("Financial Services", ["sarah"]),
        ]:
            result = await conn.execute(
                pg_insert(Team)
                .values(name=name)
                .on_conflict_do_nothing(index_elements=["name"])
                .returning(Team.id)
            )
            tid = result.scalar_one_or_none()
            if tid is not None:
                await conn.execute(
                    insert(AuditLog).values(
                        actor="sarah", action="seed_team", target=str(tid), detail={}
                    )
                )
            tid = (await conn.execute(select(Team.id).where(Team.name == name))).scalar_one()
            for handle in handles:
                result = await conn.execute(
                    pg_insert(TeamMember)
                    .values(team_id=tid, user_id=users[handle])
                    .on_conflict_do_nothing()
                    .returning(TeamMember.user_id)
                )
                if result.scalar_one_or_none() is not None:
                    await conn.execute(
                        insert(AuditLog).values(
                            actor=handle, action="seed_membership", target=str(tid), detail={}
                        )
                    )


async def main() -> None:
    engine = make_engine()
    try:
        await seed(engine)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
