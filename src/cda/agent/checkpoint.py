"""Postgres checkpoint lifecycle, independent of application migrations."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from sqlalchemy.engine import make_url


@asynccontextmanager
async def open_checkpointer(database_url: str) -> AsyncIterator[AsyncPostgresSaver]:
    url = make_url(database_url).set(drivername="postgresql")
    async with await AsyncConnection.connect(
        url.render_as_string(hide_password=False), autocommit=True
    ) as conn:
        await conn.execute("CREATE SCHEMA IF NOT EXISTS langgraph")
    url = url.update_query_dict({"options": "-csearch_path=langgraph"})
    async with AsyncPostgresSaver.from_conn_string(
        url.render_as_string(hide_password=False)
    ) as saver:
        await saver.setup()
        yield saver
