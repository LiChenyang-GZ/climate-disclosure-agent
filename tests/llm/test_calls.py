from dataclasses import asdict
from decimal import Decimal

import pytest
from sqlalchemy import select

from cda.db.models import LLMCall
from cda.llm.calls import record_call
from cda.llm.fake import FakeLLM, FakeTurn


@pytest.mark.db
async def test_record_call(db, rows):
    response = await FakeLLM([FakeTurn(content="ok")]).complete(
        messages=[], tools=[], model_key="sol", seed=42
    )
    await record_call(
        db, run_id=rows["run"], check_id="C1", step=2, response=response, cost=Decimal("0.123456")
    )
    stored = (await db.execute(select(LLMCall))).mappings().one()
    for key, value in asdict(response.fingerprint).items():
        assert stored[key] == value
    for key, value in asdict(response.usage).items():
        assert stored[key] == value
    assert stored["run_id"] == rows["run"]
    assert stored["check_id"] == "C1"
    assert stored["step"] == 2
    assert stored["system_fingerprint"] == "fake"
    assert stored["latency_ms"] == 0
    assert stored["cost_usd"] == Decimal("0.123456")
