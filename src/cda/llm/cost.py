"""Configured model prices and conservative cost estimates."""

import json
import math
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, RootModel

from cda.llm.schema import strict_tool_schema
from cda.llm.types import Message, ToolSpec, Usage


class ModelPrice(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    model: str
    input: Decimal = Field(ge=0, allow_inf_nan=False)
    cached_input: Decimal = Field(ge=0, allow_inf_nan=False)
    output: Decimal = Field(ge=0, allow_inf_nan=False)
    # Sent as reasoning_effort when set. gpt-6-luna only accepts function tools on Chat
    # Completions with reasoning turned off ("none").
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh"] | None = None


class ModelTable(RootModel[dict[str, ModelPrice]]):
    @classmethod
    def load(cls, path: str | Path = "config/models.yaml") -> "ModelTable":
        with Path(path).open(encoding="utf-8") as file:
            return cls.model_validate(yaml.safe_load(file))

    def __getitem__(self, key: str) -> ModelPrice:
        return self.root[key]


def cost_usd(price: ModelPrice, usage: Usage) -> Decimal:
    total = (
        (usage.tokens_in - usage.tokens_cached) * price.input
        + usage.tokens_cached * price.cached_input
        + usage.tokens_out * price.output
    )
    return (total / Decimal(1_000_000)).quantize(Decimal("0.000001"))


def max_cost_usd(price: ModelPrice, *, prompt_tokens: int, max_output_tokens: int) -> Decimal:
    return cost_usd(price, Usage(prompt_tokens, max_output_tokens, 0))


def estimate_tokens(messages: list[Message], tools: list[ToolSpec]) -> int:
    chars = sum(
        len(m.content or "") + sum(len(c.arguments) for c in m.tool_calls) for m in messages
    )
    chars += len(json.dumps([strict_tool_schema(tool) for tool in tools]))
    return math.ceil(chars / 4) + 4 * len(messages)
