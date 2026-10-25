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
    temperature: float
    seed: int | None


def messages_payload(messages: list[Message]) -> list[dict[str, Any]]:
    result = []
    for message in messages:
        row: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_call_id is not None:
            row["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            row["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": _canonical_arguments(call.arguments),
                    },
                }
                for call in message.tool_calls
            ]
        result.append(row)
    return result


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
    from cda.llm.schema import strict_tool_schema

    payload = messages_payload(messages)
    return RequestFingerprint(
        model, _hash(payload), _hash([strict_tool_schema(t) for t in tools]), 0.0, seed
    )
