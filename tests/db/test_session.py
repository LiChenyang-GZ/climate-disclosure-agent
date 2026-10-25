from pathlib import Path

import pytest
from sqlalchemy import text

from cda.db.session import make_engine, make_sessionmaker


def test_make_engine_requires_database_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        make_engine()


@pytest.mark.db
@pytest.mark.asyncio
async def test_session_can_query_postgres(pg_url: str) -> None:
    engine = make_engine(pg_url)
    sessionmaker = make_sessionmaker(engine)

    async with sessionmaker() as session:
        result = await session.execute(text("SELECT 1"))
        assert result.scalar_one() == 1

    await engine.dispose()
