"""Stable request fingerprints and shared wire serialization."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cda.llm.types import Message, ToolSpec


@dataclass(frozen=True)
class RequestFingerprint:
    model: str
    prompt_sha256: str
    tools_sha256: str
    temperature: float | None
    seed: int | None


def messages_payload(messages: list[Message]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "tool":
            result.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": message.content,
                }
            )
            continue
        if message.role != "assistant" or message.content is not None:
            result.append({"role": message.role, "content": message.content})
        for call in message.tool_calls:
            result.append(
                {
                    "type": "function_call",
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": _canonical_arguments(call.arguments),
                }
            )
    return result


def tools_payload(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    from cda.llm.schema import strict_tool_schema

    return [{"type": "function", **strict_tool_schema(tool)["function"]} for tool in tools]


def _canonical_arguments(raw: str) -> str:
    # Canonicalize before sending so hashes match the wire and ignore object key order.
    try:
        value = json.loads(raw)
    except ValueError:
        return raw
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def make_fingerprint(
    model: str, messages: list[Message], tools: list[ToolSpec], seed: int | None
) -> RequestFingerprint:
    payload = messages_payload(messages)
    return RequestFingerprint(model, _hash(payload), _hash(tools_payload(tools)), None, None)
