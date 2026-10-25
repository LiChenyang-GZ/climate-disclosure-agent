"""Provider-neutral LLM interface; importing this package needs no credentials."""

from cda.llm.client import LLMClient, parse_tool_call
from cda.llm.types import (
    LLMAuthError,
    LLMBadRequest,
    LLMError,
    LLMResponse,
    LLMRetryExhausted,
    Message,
    ToolArgsError,
    ToolCall,
    ToolSpec,
    Usage,
)

__all__ = [
    "LLMAuthError",
    "LLMBadRequest",
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "LLMRetryExhausted",
    "Message",
    "ToolArgsError",
    "ToolCall",
    "ToolSpec",
    "Usage",
    "parse_tool_call",
]
