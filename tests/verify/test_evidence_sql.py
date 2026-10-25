import warnings
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SADeprecationWarning

from cda.verify.evidence import EvidenceItem, verify_evidence


async def test_next_query_is_bounded_and_has_no_deprecation_warnings(monkeypatch):
    @asynccontextmanager
    async def scope(*args):
        yield

    monkeypatch.setattr("cda.verify.evidence.tool_scope", scope)
    result = MagicMock()
    result.mappings.return_value = []
    conn = AsyncMock()
    conn.execute.return_value = result
    with warnings.catch_warnings():
        warnings.simplefilter("error", SADeprecationWarning)
        await verify_evidence(
            conn,
            user_id=uuid4(),
            pack_id=uuid4(),
            items=[EvidenceItem(uuid4(), "climate disclosures", "supports")],
        )
        sql = str(conn.execute.call_args_list[1].args[0].compile(dialect=postgresql.dialect()))
    assert conn.execute.await_count == 2
    assert "JOIN LATERAL" in sql
    assert "ORDER BY units.seq" in sql
    assert "LIMIT" in sql
    assert "DISTINCT" not in sql
