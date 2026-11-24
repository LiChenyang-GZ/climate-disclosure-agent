"""Shorten old tool results in the prompt; the worker's own history stays complete (D15, D59)."""

import json
from typing import Any

from pydantic import ValidationError

from cda.llm.types import Message, ToolCall
from cda.tools.envelope import ToolResult

MAX_UNIT_IDS = 20
NOTE = "Older result shortened; call read_unit to see the text again."


def _tool_result(message: Message) -> ToolResult | None:
    """A tool message holding a ToolResult envelope; feedback messages are plain text."""
    if message.role != "tool" or not message.content:
        return None
    try:
        return ToolResult.model_validate_json(message.content)
    except ValidationError:
        return None


def _unit_ids(value: Any, out: list[str]) -> None:
    if isinstance(value, dict):
        uid = value.get("unit_id")
        if isinstance(uid, str) and uid not in out:
            out.append(uid)
        for child in value.values():
            _unit_ids(child, out)
    elif isinstance(value, list):
        for child in value:
            _unit_ids(child, out)


def _arguments(call: ToolCall | None) -> Any:
    if call is None:
        return None
    try:
        return json.loads(call.arguments)
    except ValueError:
        return call.arguments


def shorten(result: ToolResult, call: ToolCall | None) -> str:
    unit_ids: list[str] = []
    _unit_ids(result.data, unit_ids)
    return json.dumps(
        {
            "shortened": True,
            "tool": call.name if call else None,
            "args": _arguments(call),
            "category": result.category,
            "unit_ids": unit_ids[:MAX_UNIT_IDS],
            "note": NOTE,
        },
        ensure_ascii=False,
    )


def shorten_history(history: list[Message], keep: int) -> list[Message]:
    """Return a copy where all but the newest `keep` tool results are shortened."""
    if keep < 1:
        raise ValueError("keep must be at least 1")
    results = [i for i, m in enumerate(history) if _tool_result(m) is not None]
    old = set(results[:-keep])
    if not old:
        return list(history)
    calls = {c.id: c for m in history if m.role == "assistant" for c in m.tool_calls}
    out = []
    for i, message in enumerate(history):
        result = _tool_result(message) if i in old else None
        if result is None:
            out.append(message)
            continue
        call = calls.get(message.tool_call_id or "")
        out.append(Message("tool", shorten(result, call), tool_call_id=message.tool_call_id))
    return out
