"""Gemini adapter against a mocked Gemini REST endpoint (D90); no real calls."""

import base64
import json
import traceback

import httpx
import pytest
from pydantic import BaseModel

from cda.llm.calls import response_payload
from cda.llm.cost import ModelPrice, ModelTable, cost_usd
from cda.llm.fingerprint import make_fingerprint
from cda.llm.gemini_client import SKIP_SIGNATURE, GeminiClient
from cda.llm.schema import strict_tool_schema
from cda.llm.types import (
    LLMAuthError,
    LLMBadRequest,
    LLMResponse,
    LLMRetryExhausted,
    Message,
    ToolCall,
    ToolSpec,
    Usage,
)


class Child(BaseModel):
    count: int


class Args(BaseModel):
    children: list[Child]
    note: str | None = None


TOOLS = [ToolSpec("find", "Find evidence", Args), ToolSpec("submit", "Submit", Child)]
MESSAGES = [Message("system", "Shared"), Message("user", "Find evidence")]
SIG = base64.b64encode(b"model-signature-1").decode()


def reply(*, calls=0, text=None, finish="STOP", signature=True, usage=True):
    parts = [{"text": "thinking summary", "thought": True}]
    if text is not None:
        parts.append({"text": text})
    for i in range(calls):
        part = {"functionCall": {"id": f"fc_{i}", "name": "find", "args": {"children": []}}}
        if i == 0 and signature:
            part["thoughtSignature"] = SIG
        parts.append(part)
    body = {
        "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}],
        "modelVersion": "gemini-3.8-flash-001",
    }
    if usage:
        body["usageMetadata"] = {
            "promptTokenCount": 100,
            "candidatesTokenCount": 15,
            "thoughtsTokenCount": 5,
            "cachedContentTokenCount": 40,
        }
    return body


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-placeholder")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)

    def build(script):
        requests, sleeps = [], []

        async def handler(request):
            assert request.url.path.endswith(":generateContent")
            requests.append(
                {
                    "path": request.url.path,
                    "body": json.loads(request.content),
                    "headers": request.headers,
                }
            )
            item = script.pop(0)
            if isinstance(item, Exception):
                raise item
            status, headers, body = item
            return httpx.Response(status, headers=headers, json=body)

        async def sleep(delay):
            sleeps.append(delay)

        transport = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = GeminiClient(ModelTable.load(), sleep=sleep, http_client=transport)
        return client, requests, sleeps

    return build


async def test_request_mapping(harness):
    client, requests, _ = harness([(200, {}, reply(text="ok"))])
    history = MESSAGES + [
        Message(
            "assistant",
            "Searching",
            [ToolCall("call_1", "find", '{"note":null,"children":[]}', SIG)],
        ),
        Message("tool", '{"hits": [{"unit_id": "u1"}]}', tool_call_id="call_1"),
        Message("assistant", None, [ToolCall("call_2", "find", '{"children":[]}')]),
        Message("tool", "shortened: 3 hits", tool_call_id="call_2"),
        Message("system", "Second system note"),
    ]
    await client.complete(
        messages=history, tools=TOOLS, model_key="flash", seed=7, max_output_tokens=321
    )
    request = requests[0]
    assert request["path"].endswith("/models/gemini-3.8-flash:generateContent")
    body = request["body"]
    assert body["systemInstruction"]["parts"] == [{"text": "Shared\n\nSecond system note"}]
    assert body["contents"] == [
        {"role": "user", "parts": [{"text": "Find evidence"}]},
        {
            "role": "model",
            "parts": [
                {"text": "Searching"},
                {
                    "functionCall": {
                        "id": "call_1",
                        "name": "find",
                        "args": {"note": None, "children": []},
                    },
                    "thoughtSignature": SIG,
                },
            ],
        },
        {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "id": "call_1",
                        "name": "find",
                        "response": {"hits": [{"unit_id": "u1"}]},
                    }
                }
            ],
        },
        {
            "role": "model",
            "parts": [
                {
                    "functionCall": {"id": "call_2", "name": "find", "args": {"children": []}},
                    # No signature in our history (e.g. a replayed call): documented placeholder.
                    "thoughtSignature": SKIP_SIGNATURE,
                }
            ],
        },
        {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "id": "call_2",
                        "name": "find",
                        "response": {"result": "shortened: 3 hits"},
                    }
                }
            ],
        },
    ]
    declarations = body["tools"][0]["functionDeclarations"]
    assert [d["name"] for d in declarations] == ["find", "submit"]
    assert declarations[0]["description"] == "Find evidence"
    assert (
        declarations[0]["parameters_json_schema"]
        == strict_tool_schema(TOOLS[0])["function"]["parameters"]
    )
    generation = body["generationConfig"]
    assert generation["maxOutputTokens"] == 321
    assert generation["thinkingConfig"] == {"thinking_level": "LOW"}
    assert not {"temperature", "seed"} & generation.keys()
    assert "test-placeholder" not in json.dumps(body)


async def test_consecutive_tool_results_share_one_content(harness):
    client, requests, _ = harness([(200, {}, reply(text="ok"))])
    history = MESSAGES + [
        Message(
            "assistant",
            None,
            [ToolCall("a", "find", "{}", SIG), ToolCall("b", "submit", '{"count":1}')],
        ),
        Message("tool", "one", tool_call_id="a"),
        Message("tool", "two", tool_call_id="b"),
    ]
    await client.complete(messages=history, tools=TOOLS, model_key="flash")
    contents = requests[0]["body"]["contents"]
    model_parts = contents[1]["parts"]
    assert "thoughtSignature" in model_parts[0] and "thoughtSignature" not in model_parts[1]
    assert [p["functionResponse"]["name"] for p in contents[2]["parts"]] == ["find", "submit"]
    assert len(contents) == 3


@pytest.mark.parametrize(
    "choice,expected",
    [
        ("auto", {"mode": "AUTO"}),
        ("required", {"mode": "ANY"}),
        ("submit", {"mode": "ANY", "allowedFunctionNames": ["submit"]}),
    ],
)
async def test_tool_choice(harness, choice, expected):
    client, requests, _ = harness([(200, {}, reply(text="ok"))])
    await client.complete(messages=MESSAGES, tools=TOOLS, model_key="flash", tool_choice=choice)
    assert requests[0]["body"]["toolConfig"] == {"functionCallingConfig": expected}


async def test_no_tools_and_lite_thinking(harness):
    client, requests, _ = harness([(200, {}, reply(text="Done"))])
    result = await client.complete(messages=MESSAGES, tools=[], model_key="lite")
    body = requests[0]["body"]
    assert "tools" not in body and "toolConfig" not in body
    assert body["generationConfig"]["thinkingConfig"] == {"thinking_level": "MINIMAL"}
    assert requests[0]["path"].endswith("/models/gemini-3.5-flash-lite:generateContent")
    assert result.message.content == "Done"
    assert result.finish_reason == "completed"


async def test_response_mapping(harness):
    client, _, _ = harness([(200, {}, reply(calls=2, text="Let me look"))])
    result = await client.complete(messages=MESSAGES, tools=TOOLS, model_key="flash")
    # Only the first call is kept (like parallel_tool_calls=False), with its signature.
    assert result.message.tool_calls == [ToolCall("fc_0", "find", '{"children": []}')]
    assert base64.urlsafe_b64decode(result.message.tool_calls[0].signature) == b"model-signature-1"
    assert result.message.content == "Let me look"  # thought parts dropped
    assert result.finish_reason == "tool_calls"
    assert result.usage == Usage(100, 20, 40)  # output = candidates + thinking
    assert result.model == "gemini-3.8-flash-001"
    assert result.system_fingerprint is None
    assert cost_usd(ModelTable.load()["flash"], result.usage) > 0


async def test_signature_round_trip(harness):
    client, requests, _ = harness([(200, {}, reply(calls=1)), (200, {}, reply(text="ok"))])
    first = await client.complete(messages=MESSAGES, tools=TOOLS, model_key="flash")
    call = first.message.tool_calls[0]
    history = MESSAGES + [first.message, Message("tool", "{}", tool_call_id=call.id)]
    await client.complete(messages=history, tools=TOOLS, model_key="flash")
    sent = requests[1]["body"]["contents"][1]["parts"][0]
    assert sent["functionCall"]["id"] == "fc_0"
    assert base64.urlsafe_b64decode(sent["thoughtSignature"]) == b"model-signature-1"


@pytest.mark.parametrize(
    "finish,expected", [("MAX_TOKENS", "max_output_tokens"), ("SAFETY", "safety")]
)
async def test_finish_reasons(harness, finish, expected):
    client, _, _ = harness([(200, {}, reply(text="partial", finish=finish))])
    result = await client.complete(messages=MESSAGES, tools=[], model_key="flash")
    assert result.finish_reason == expected


async def test_missing_usage(harness):
    client, _, _ = harness([(200, {}, reply(text="x", usage=False))])
    result = await client.complete(messages=MESSAGES, tools=[], model_key="flash")
    assert result.usage == Usage(0, 0, 0)


def test_signature_kept_out_of_fingerprint_and_stored_response():
    signed = Message("assistant", None, [ToolCall("1", "find", "{}", SIG)])
    plain = Message("assistant", None, [ToolCall("1", "find", "{}")])
    assert make_fingerprint("m", [signed], TOOLS, None) == make_fingerprint(
        "m", [plain], TOOLS, None
    )
    fingerprint = make_fingerprint("m", [], [], None)
    stored = response_payload(
        LLMResponse(signed, "tool_calls", Usage(1, 1, 0), "m", None, 0, fingerprint)
    )
    assert SIG not in json.dumps(stored)
    assert stored == response_payload(
        LLMResponse(plain, "tool_calls", Usage(1, 1, 0), "m", None, 0, fingerprint)
    )


async def test_rate_limit_then_success(harness):
    client, requests, sleeps = harness(
        [
            (429, {"Retry-After": "7"}, {"error": {"code": 429}}),
            (503, {}, {}),
            (200, {}, reply(text="ok")),
        ]
    )
    await client.complete(messages=MESSAGES, tools=[], model_key="flash")
    assert len(requests) == 3
    assert sleeps[0] == 7
    assert 2 <= sleeps[1] <= 2.25


@pytest.mark.parametrize("status", [429, 500, 503, 504])
async def test_retries_exhausted(harness, status):
    client, requests, sleeps = harness([(status, {}, {})] * 4)
    with pytest.raises(LLMRetryExhausted) as exc:
        await client.complete(messages=MESSAGES, tools=[], model_key="flash")
    assert exc.value.status == status
    assert len(requests) == 4
    assert len(sleeps) == 3


async def test_timeout_exhausted(harness):
    client, requests, _ = harness([httpx.ReadTimeout("slow")] * 4)
    with pytest.raises(LLMRetryExhausted) as exc:
        await client.complete(messages=MESSAGES, tools=[], model_key="flash")
    assert exc.value.kind == "ReadTimeout"
    assert len(requests) == 4


@pytest.mark.parametrize(
    "status,error",
    [
        (401, LLMAuthError),
        (402, LLMAuthError),
        (403, LLMAuthError),
        (400, LLMBadRequest),
        (404, LLMBadRequest),
    ],
)
async def test_fail_fast(harness, status, error):
    client, requests, sleeps = harness(
        [(status, {}, {"error": {"code": status, "message": "private body", "status": "X"}})]
    )
    with pytest.raises(error) as exc:
        await client.complete(messages=MESSAGES, tools=[], model_key="flash")
    assert len(requests) == 1
    assert sleeps == []
    assert exc.value.__cause__ is None and exc.value.__suppress_context__
    text = "".join(traceback.format_exception(exc.value))
    for secret in ("private body", "test-placeholder", "Find evidence"):
        assert secret not in text


async def test_missing_key_fails_only_on_complete(monkeypatch):
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI"):
        monkeypatch.delenv(name, raising=False)
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json=reply(text="x"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        client = GeminiClient(ModelTable.load(), http_client=transport)
        with pytest.raises(LLMAuthError):
            await client.complete(messages=MESSAGES, tools=[], model_key="flash")
    assert requests == []


def test_provider_options_validated():
    with pytest.raises(ValueError, match="reasoning_effort is for OpenAI"):
        ModelPrice(
            provider="gemini", model="g", input=1, cached_input=1, output=1, reasoning_effort="low"
        )
    with pytest.raises(ValueError, match="thinking_level is for Gemini"):
        ModelPrice(model="o", input=1, cached_input=1, output=1, thinking_level="low")
    table = ModelTable.load()
    assert table["luna"].provider == "openai"
    assert (table["flash"].provider, table["lite"].provider) == ("gemini", "gemini")
