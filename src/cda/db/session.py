from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from cda.config import Settings


def make_engine(url: str | None = None) -> AsyncEngine:
    database_url = url or Settings().database_url
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set")
    return create_async_engine(database_url)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
