import asyncio
import os
import selectors
import sys
from collections.abc import Callable, Iterator

import pytest
from docker.errors import DockerException
from testcontainers.community.postgres import PostgresContainer


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    try:
        container = PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg")
        container.start()
    except DockerException as exc:
        if os.environ.get("CDA_REQUIRE_DOCKER") == "1":
            pytest.fail(f"Docker not available: {exc}")
        pytest.skip("Docker not available")

    try:
        # 127.0.0.1, not localhost: psycopg on the selector loop can stall on ::1 (Windows).
        yield container.get_connection_url().replace("@localhost:", "@127.0.0.1:")
    finally:
        container.stop()


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> dict[str, Callable[[], asyncio.AbstractEventLoop]]:
    # psycopg (LangGraph's Postgres checkpointer) cannot run on Windows' default Proactor loop.
    if sys.platform == "win32":
        return {"selector": lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())}
    return {"default": asyncio.new_event_loop}
