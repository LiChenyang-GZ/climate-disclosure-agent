import json
import math
import traceback
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest
from pydantic import BaseModel

from cda.llm.client import parse_tool_call
from cda.llm.cost import ModelTable, cost_usd, estimate_tokens, max_cost_usd
from cda.llm.fake import FakeLLM, FakeTurn
from cda.llm.fingerprint import make_fingerprint, messages_payload
from cda.llm.openai_client import OpenAIClient
from cda.llm.schema import strict_tool_schema
from cda.llm.types import (
    LLMAuthError,
    LLMBadRequest,
    LLMRetryExhausted,
    Message,
    ToolArgsError,
    ToolCall,
    ToolSpec,
    Usage,
)


class Child(BaseModel):
    count: int


class Args(BaseModel):
    children: list[Child]
    note: str | None = None


TOOLS = [ToolSpec("find", "Find evidence", Args)]
MESSAGES = [Message("system", "Shared"), Message("user", "Find evidence")]


def test_strict_schema():
    schema = strict_tool_schema(TOOLS[0])
    params = schema["function"]["parameters"]
    assert schema["function"]["strict"] is True
    for obj in [params, params["$defs"]["Child"]]:
        assert obj["additionalProperties"] is False
        assert obj["required"] == list(obj["properties"])
    assert params["properties"]["children"]["items"] == {"$ref": "#/$defs/Child"}
    assert {"type": "null"} in params["properties"]["note"]["anyOf"]
    assert "title" not in json.dumps(schema)
    assert "default" not in json.dumps(schema)


def test_non_nullable_default():
    class Bad(BaseModel):
        count: int = 1

    with pytest.raises(ValueError):
        strict_tool_schema(ToolSpec("bad", "", Bad))


def test_parse_valid():
    result = parse_tool_call(ToolCall("1", "find", '{"children":[{"count":1}]}'), TOOLS)
    assert isinstance(result, Args)
    assert result.children[0].count == 1


def test_parse_unknown():
    with pytest.raises(ToolArgsError):
        parse_tool_call(ToolCall("1", "unknown", "{}"), TOOLS)


def test_parse_invalid_json():
    with pytest.raises(ToolArgsError):
        parse_tool_call(ToolCall("1", "find", "{invalid"), TOOLS)


def test_parse_wrong_type():
    with pytest.raises(ToolArgsError) as exc:
        parse_tool_call(ToolCall("1", "find", '{"children":"PRIVATE_INPUT"}'), TOOLS)
    assert "children" in str(exc.value)
    assert "PRIVATE_INPUT" not in str(exc.value)
    assert len(exc.value.message) <= 1000


def reply(*, tool=False, cached=True):
    output = [{"type": "reasoning", "id": "rs_1", "summary": []}]
    if tool:
        output.append(
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "server_id",
                "name": "find",
                "arguments": '{"children":[]}',
            }
        )
    else:
        for i, text in enumerate(["Do", "ne"]):
            output.append(
                {
                    "type": "message",
                    "id": f"msg_{i}",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                }
            )
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 1,
        "model": "gpt-6.1-sol",
        "status": "completed",
        "output": output,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "input_tokens_details": {"cached_tokens": 40 if cached else 0},
            "output_tokens_details": {"reasoning_tokens": 10},
        },
    }


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    clients = []

    def build(script):
        requests, sleeps = [], []

        async def handler(request):
            assert request.url.path == "/v1/responses"
            requests.append(json.loads(request.content))
            item = script.pop(0)
            if isinstance(item, Exception):
                raise item
            status, headers, body = item
            return httpx.Response(status, headers=headers, json=body)

        async def sleep(delay):
            sleeps.append(delay)

        transport = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(transport)
        client = OpenAIClient(ModelTable.load(), sleep=sleep, http_client=transport)
        return client, requests, sleeps

    return build


@pytest.mark.parametrize("choice", ["auto", "required", "find"])
async def test_request(harness, choice):
    client, requests, _ = harness([(200, {}, reply())])
    history = MESSAGES + [
        Message(
            "assistant", "Searching", [ToolCall("call_1", "find", '{"note":null,"children":[]}')]
        ),
        Message("tool", "Found", tool_call_id="call_1"),
    ]
    result = await client.complete(
        messages=history,
        tools=TOOLS,
        model_key="sol",
        seed=42,
        tool_choice=choice,
        max_output_tokens=123,
    )
    request = requests[0]
    assert request["model"] == "gpt-6.1-sol"
    assert not {"temperature", "seed", "messages", "top_p"} & request.keys()
    assert request["store"] is False
    assert request["max_output_tokens"] == 123
    assert request["parallel_tool_calls"] is False
    assert request["tools"] == [{"type": "function", **strict_tool_schema(TOOLS[0])["function"]}]
    assert request["input"] == [
        {"role": "system", "content": "Shared"},
        {"role": "user", "content": "Find evidence"},
        {"role": "assistant", "content": "Searching"},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "find",
            "arguments": '{"children":[],"note":null}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "Found"},
    ]
    assert request["tool_choice"] == (
        choice if choice != "find" else {"type": "function", "name": "find"}
    )
    assert result.fingerprint == make_fingerprint("gpt-6.1-sol", history, TOOLS, 42)
    import hashlib

    for field, payload in [("prompt_sha256", request["input"]), ("tools_sha256", request["tools"])]:
        assert (
            getattr(result.fingerprint, field)
            == hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )


@pytest.mark.parametrize("tool,cached", [(False, True), (True, False)])
async def test_response_and_no_tools(harness, tool, cached):
    client, requests, _ = harness([(200, {}, reply(tool=tool, cached=cached))])
    result = await client.complete(messages=MESSAGES, tools=[], model_key="sol")
    assert "tools" not in requests[0]
    assert "tool_choice" not in requests[0]
    assert result.usage == Usage(100, 20, 40 if cached else 0)
    assert result.system_fingerprint is None
    assert cost_usd(ModelTable.load()["sol"], result.usage) == (
        Decimal("0.000324") if cached else Decimal("0.000400")
    )
    assert result.model == "gpt-6.1-sol"
    assert result.latency_ms >= 0
    assert result.finish_reason == ("tool_calls" if tool else "completed")
    if tool:
        assert result.message.tool_calls == [ToolCall("server_id", "find", '{"children":[]}')]
    else:
        assert result.message.content == "Done"


async def test_rate_limit(harness):
    client, requests, sleeps = harness([(429, {}, {})] * 2 + [(200, {}, reply())])
    await client.complete(messages=MESSAGES, tools=[], model_key="sol")
    assert len(requests) == 3
    assert 1 <= sleeps[0] <= 1.25
    assert 2 <= sleeps[1] <= 2.25


async def test_retry_after(harness):
    client, _, sleeps = harness([(429, {"Retry-After": "7"}, {}), (200, {}, reply())])
    await client.complete(messages=MESSAGES, tools=[], model_key="sol")
    assert sleeps == [7]


async def test_server_exhausted(harness):
    client, requests, sleeps = harness([(500, {}, {})] * 4)
    with pytest.raises(LLMRetryExhausted) as exc:
        await client.complete(messages=MESSAGES, tools=[], model_key="sol")
    assert exc.value.status == 500
    assert len(requests) == 4
    assert len(sleeps) == 3


async def test_timeout(harness):
    client, requests, sleeps = harness([httpx.ReadTimeout("timeout"), (200, {}, reply())])
    await client.complete(messages=MESSAGES, tools=[], model_key="sol")
    assert len(requests) == 2
    assert len(sleeps) == 1


async def test_connection_error(harness):
    client, _, sleeps = harness([httpx.ConnectError("offline"), (200, {}, reply())])
    await client.complete(messages=MESSAGES, tools=[], model_key="sol")
    assert len(sleeps) == 1


@pytest.mark.parametrize(
    "status,error",
    [
        (401, LLMAuthError),
        (403, LLMAuthError),
        (400, LLMBadRequest),
        (404, LLMBadRequest),
        (422, LLMBadRequest),
    ],
)
async def test_fail_fast(harness, status, error):
    client, requests, sleeps = harness([(status, {}, {"error": {"message": "private body"}})])
    with pytest.raises(error) as exc:
        await client.complete(messages=MESSAGES, tools=[], model_key="sol")
    assert len(requests) == 1
    assert sleeps == []
    text = "".join(traceback.format_exception(exc.value))
    assert "private body" not in text
    assert "test-placeholder" not in text
    assert "Find evidence" not in text


def test_fingerprint():
    baseline = make_fingerprint("sol", MESSAGES, TOOLS, 42)
    assert baseline == make_fingerprint("sol", MESSAGES, TOOLS, 42)
    for changed in [
        make_fingerprint("sol", [Message("user", "changed")], TOOLS, 42),
        make_fingerprint("sol", MESSAGES, [replace(TOOLS[0], description="new")], 42),
        make_fingerprint("luna", MESSAGES, TOOLS, 42),
    ]:
        assert changed != baseline
    assert baseline.temperature is None
    assert baseline.seed is None
    assert baseline == make_fingerprint("sol", MESSAGES, TOOLS, 43)
    a = Message("assistant", None, [ToolCall("1", "find", '{"b":2,"a":1}')])
    b = Message("assistant", None, [ToolCall("1", "find", '{"a":1,"b":2}')])
    assert make_fingerprint("sol", [a], [], None) == make_fingerprint("sol", [b], [], None)


def test_cost():
    price = ModelTable.load()["sol"]
    usage = Usage(1000, 200, 400)
    assert cost_usd(price, usage) == Decimal("0.003240")
    assert cost_usd(price, usage).as_tuple().exponent == -6
    assert max_cost_usd(price, prompt_tokens=1000, max_output_tokens=200) >= cost_usd(price, usage)


def test_unknown_model():
    with pytest.raises(KeyError, match="missing"):
        ModelTable.load()["missing"]


def test_estimate():
    messages = MESSAGES + [Message("assistant", None, [ToolCall("1", "find", "{}")])]
    chars = sum(len(m.content or "") for m in messages) + 2
    chars += len(json.dumps([strict_tool_schema(t) for t in TOOLS]))
    assert estimate_tokens(messages, TOOLS) == math.ceil(chars / 4) + 4 * len(messages)


async def test_fake():
    fake = FakeLLM(
        [
            FakeTurn(tool_calls=[("find", {"children": []}), ("find", "{bad")]),
            LLMAuthError("denied"),
        ],
        models=ModelTable.load(),
    )
    result = await fake.complete(messages=MESSAGES, tools=TOOLS, model_key="sol", seed=42)
    assert [c.id for c in result.message.tool_calls] == ["call_1", "call_2"]
    assert json.loads(result.message.tool_calls[0].arguments) == {"children": []}
    assert result.message.tool_calls[1].arguments == "{bad"
    assert result.finish_reason == "tool_calls"
    assert result.model == "gpt-6.1-sol"
    assert result.fingerprint == make_fingerprint(result.model, MESSAGES, TOOLS, 42)
    assert result.system_fingerprint == "fake"
    assert result.latency_ms == 0
    assert fake.requests[0]["messages"] == MESSAGES
    assert fake.requests[0]["tools"] == TOOLS
    assert fake.requests[0]["model_key"] == "sol"
    assert fake.requests[0]["seed"] == 42
    assert fake.requests[0]["tool_choice"] == "auto"
    with pytest.raises(LLMAuthError):
        await fake.complete(messages=[], tools=[], model_key="sol")
    with pytest.raises(AssertionError, match="FakeLLM script exhausted"):
        await fake.complete(messages=[], tools=[], model_key="sol")
    assert len(fake.requests) == 3


async def test_fake_text():
    fake = FakeLLM([FakeTurn(content="hello")])
    response = await fake.complete(messages=[], tools=[], model_key="key")
    assert response.model == "key"
    assert response.message.content == "hello"
    assert response.finish_reason == "stop"


def test_fingerprint_hashes_wire_messages():
    import hashlib

    messages = [Message("assistant", None, [ToolCall("1", "find", '{"b": 2, "a": 1}')])]
    payload = messages_payload(messages)
    expected = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert make_fingerprint("sol", messages, [], None).prompt_sha256 == expected


async def test_import_without_api_key(monkeypatch):
    import importlib

    import cda.llm

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    importlib.reload(cda.llm)


async def test_missing_key_fails_only_on_complete(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json=reply())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        client = OpenAIClient(ModelTable.load(), http_client=transport)
        with pytest.raises(LLMAuthError):
            await client.complete(messages=MESSAGES, tools=[], model_key="sol")
    assert requests == []


@pytest.mark.parametrize("header,expected", [("100", 30), ("invalid", None), ("-1", None)])
async def test_retry_after_bounds(harness, header, expected):
    client, _, sleeps = harness([(429, {"Retry-After": header}, {}), (200, {}, reply())])
    await client.complete(messages=MESSAGES, tools=[], model_key="sol")
    if expected is None:
        assert 1 <= sleeps[0] <= 1.25
    else:
        assert sleeps == [expected]


async def test_zero_retries(harness):
    client, requests, sleeps = harness([(500, {}, {})])
    client._max_retries = 0
    with pytest.raises(LLMRetryExhausted):
        await client.complete(messages=[], tools=[], model_key="sol")
    assert len(requests) == 1
    assert sleeps == []


def test_non_nullable_factory_default():
    from pydantic import Field

    class Bad(BaseModel):
        children: list[Child] = Field(default_factory=list)

    with pytest.raises(ValueError):
        strict_tool_schema(ToolSpec("bad", "", Bad))


async def test_messages_keep_tool_history(harness):
    history = MESSAGES + [
        Message("assistant", None, [ToolCall("call_1", "find", '{"children":[]}')]),
        Message("tool", "Found evidence", tool_call_id="call_1"),
    ]
    client, requests, _ = harness([(200, {}, reply())])
    await client.complete(messages=history, tools=TOOLS, model_key="sol")
    assert requests[0]["input"] == messages_payload(history)
    assert requests[0]["input"][-1] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "Found evidence",
    }
    assert requests[0]["input"][-2] == {
        "type": "function_call",
        "call_id": "call_1",
        "name": "find",
        "arguments": '{"children":[]}',
    }


@pytest.mark.parametrize(("key", "expected"), [("luna", "none"), ("sol", "low"), ("unset", None)])
async def test_reasoning_effort_from_model_config(harness, key, expected):
    client, requests, _ = harness([(200, {}, reply())])
    if key == "unset":
        client._models.root[key] = client._models["sol"].model_copy(
            update={"reasoning_effort": None}
        )
    await client.complete(messages=MESSAGES, tools=TOOLS, model_key=key, seed=1)
    assert requests[0].get("reasoning") == ({"effort": expected} if expected else None)


async def test_incomplete(harness):
    body = reply()
    body.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
    client, _, _ = harness([(200, {}, body)])
    result = await client.complete(messages=MESSAGES, tools=[], model_key="sol")
    assert result.finish_reason == "max_output_tokens"


async def test_three_server_errors(harness):
    client, requests, sleeps = harness([(500, {}, {})] * 3)
    client._max_retries = 2
    with pytest.raises(LLMRetryExhausted):
        await client.complete(messages=MESSAGES, tools=[], model_key="sol")
    assert len(requests) == 3
    assert len(sleeps) == 2
