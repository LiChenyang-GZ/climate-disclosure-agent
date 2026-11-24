"""ReplayLLM without a database (D89)."""

import pytest

from cda.llm.fingerprint import make_fingerprint
from cda.llm.replay import RecordedCall, ReplayLLM, ReplayMiss
from cda.llm.types import LLMBadRequest, Message, Usage

MESSAGES = [Message("system", "s"), Message("user", "u")]
USAGE = Usage(10, 2, 0)


def recorded(content, usage=USAGE, messages=MESSAGES):
    fp = make_fingerprint("gpt-x", messages, [], None)
    return RecordedCall(
        fp.prompt_sha256,
        fp.tools_sha256,
        {
            "content": content,
            "tool_calls": [{"id": "call_1", "name": "keyword_search", "arguments": '{"q":1}'}],
            "finish_reason": "tool_calls",
            "model": "gpt-x",
        },
        usage,
        "fp",
    )


async def complete(llm, messages=MESSAGES):
    return await llm.complete(messages=messages, tools=[], model_key="sol")


async def test_identical_requests_answered_in_recorded_order():
    llm = ReplayLLM([recorded("first", Usage(10, 2, 0)), recorded("second", Usage(30, 4, 1))])
    one, two = await complete(llm), await complete(llm)
    assert (one.message.content, one.usage) == ("first", Usage(10, 2, 0))
    assert (two.message.content, two.usage) == ("second", Usage(30, 4, 1))
    assert one.message.tool_calls[0].name == "keyword_search"
    assert one.message.tool_calls[0].arguments == '{"q":1}'
    assert (one.finish_reason, one.model, one.system_fingerprint) == ("tool_calls", "gpt-x", "fp")
    assert one.fingerprint.prompt_sha256 == recorded("x").prompt_sha256
    with pytest.raises(ReplayMiss):
        await complete(llm)
    assert (llm.hits, llm.misses) == (2, 1)


async def test_unrecorded_request_is_a_bad_request_miss():
    llm = ReplayLLM([recorded("only")])
    with pytest.raises(LLMBadRequest):
        await complete(llm, [Message("system", "s"), Message("user", "changed")])
    assert llm.misses == 1
    # The recorded answer is still there for the matching request.
    assert (await complete(llm)).message.content == "only"
