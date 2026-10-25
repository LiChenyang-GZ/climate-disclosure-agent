"""Scripted network-free completion client for tests."""

import copy
import json
from dataclasses import dataclass, field
from typing import Any

from cda.llm.cost import ModelTable
from cda.llm.fingerprint import make_fingerprint
from cda.llm.types import LLMResponse, Message, ToolCall, ToolSpec, Usage


@dataclass(frozen=True)
class FakeTurn:
    content: str | None = None
    tool_calls: list[tuple[str, dict[str, Any] | str]] = field(default_factory=list)
    usage: Usage = field(default_factory=lambda: Usage(100, 20, 0))
    finish_reason: str | None = None


class FakeLLM:
    def __init__(
        self, script: list[FakeTurn | Exception], models: ModelTable | None = None
    ) -> None:
        self._script = iter(script)
        self._models = models
        self._call_id = 0
        self.requests: list[dict[str, Any]] = []

    async def complete(
        self,
        *,
        messages: list[Message],
        tools: list[ToolSpec],
        model_key: str,
        seed: int | None = None,
        tool_choice: str = "auto",
        max_output_tokens: int = 2000,
    ) -> LLMResponse:
        self.requests.append(
            copy.deepcopy(
                {
                    "messages": messages,
                    "tools": tools,
                    "model_key": model_key,
                    "seed": seed,
                    "tool_choice": tool_choice,
                    "max_output_tokens": max_output_tokens,
                }
            )
        )
        item = next(self._script, None)
        if item is None:
            raise AssertionError("FakeLLM script exhausted")
        if isinstance(item, Exception):
            raise item
        calls = []
        for name, args in item.tool_calls:
            self._call_id += 1
            calls.append(
                ToolCall(
                    f"call_{self._call_id}",
                    name,
                    args if isinstance(args, str) else json.dumps(args),
                )
            )
        model = self._models[model_key].model if self._models is not None else model_key
        return LLMResponse(
            Message("assistant", item.content, calls),
            item.finish_reason or ("tool_calls" if calls else "stop"),
            item.usage,
            model,
            "fake",
            0,
            make_fingerprint(model, messages, tools, seed),
        )
