"""Common completion protocol and argument validation."""

import json
from typing import Protocol

from pydantic import BaseModel, ValidationError

from cda.llm.types import LLMResponse, Message, ToolArgsError, ToolCall, ToolSpec


class LLMClient(Protocol):
    async def complete(
        self,
        *,
        messages: list[Message],
        tools: list[ToolSpec],
        model_key: str,
        seed: int | None = None,
        tool_choice: str = "auto",
        max_output_tokens: int = 2000,
    ) -> LLMResponse: ...


def parse_tool_call(call: ToolCall, tools: list[ToolSpec]) -> BaseModel:
    spec = next((tool for tool in tools if tool.name == call.name), None)
    if spec is None:
        raise ToolArgsError(call.name, "Unknown tool name")
    try:
        args = json.loads(call.arguments)
    except ValueError:
        raise ToolArgsError(call.name, "Invalid JSON arguments") from None
    try:
        return spec.args_model.model_validate(args)
    except ValidationError as exc:
        # Custom validator messages/context can contain input values as well.
        errors = [
            {
                "loc": err["loc"],
                "type": err["type"],
                "msg": "Invalid value" if err["type"] == "value_error" else err["msg"],
            }
            for err in exc.errors(include_input=False, include_context=False, include_url=False)
        ]
        raise ToolArgsError(call.name, json.dumps(errors)[:1000]) from None
