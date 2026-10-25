import os
from collections.abc import Iterator

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
        yield container.get_connection_url()
    finally:
        container.stop()
