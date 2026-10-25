"""Provider-neutral LLM request and response types."""

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from cda.llm.fingerprint import RequestFingerprint


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]


@dataclass(frozen=True)
class Usage:
    tokens_in: int
    tokens_out: int
    tokens_cached: int


@dataclass(frozen=True)
class LLMResponse:
    message: Message
    finish_reason: str
    usage: Usage
    model: str
    system_fingerprint: str | None
    latency_ms: int
    fingerprint: RequestFingerprint


class LLMError(Exception):
    """A safe, provider-neutral error."""


class LLMAuthError(LLMError):
    pass


class LLMBadRequest(LLMError):
    pass


class LLMRetryExhausted(LLMError):
    def __init__(self, status: int | None = None, kind: str = "unknown") -> None:
        self.status = status
        self.kind = kind
        super().__init__(f"LLM retries exhausted ({kind}, status={status})")


class ToolArgsError(LLMError):
    def __init__(self, tool_name: str, message: str) -> None:
        self.tool_name = tool_name
        self.message = message[:1000]
        super().__init__(f"{tool_name}: {self.message}")
