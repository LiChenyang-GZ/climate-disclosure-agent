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
    assert stored["temperature"] is None
    assert stored["seed"] is None
    assert stored["run_id"] == rows["run"]
    assert stored["check_id"] == "C1"
    assert stored["step"] == 2
    assert stored["system_fingerprint"] == "fake"
    assert stored["latency_ms"] == 0
    assert stored["cost_usd"] == Decimal("0.123456")
    assert stored["response"] == {
        "content": "ok",
        "tool_calls": [],
        "finish_reason": "stop",
        "model": "sol",
    }


@pytest.mark.db
async def test_record_call_stores_tool_calls(db, rows):
    # D89: our own response shape (for replay), never the provider's raw payload.
    response = await FakeLLM(
        [FakeTurn(tool_calls=[("keyword_search", {"query": "climate"})])]
    ).complete(messages=[], tools=[], model_key="sol")
    await record_call(db, run_id=rows["run"], check_id="G1", step=1, response=response, cost=0)
    stored = (await db.execute(select(LLMCall.response))).scalar_one()
    assert stored == {
        "content": None,
        "tool_calls": [
            {"id": "call_1", "name": "keyword_search", "arguments": '{"query": "climate"}'}
        ],
        "finish_reason": "tool_calls",
        "model": "sol",
    }
