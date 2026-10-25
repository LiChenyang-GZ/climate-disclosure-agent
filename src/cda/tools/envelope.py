"""Safe, uniform results for worker tools."""

from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator


class ToolResult(BaseModel):
    ok: bool
    category: Literal["found", "not_found", "bad_args", "timeout", "forbidden"]
    retryable: bool
    message: str = Field(max_length=300)
    data: dict[str, Any]

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.ok != (self.category in {"found", "not_found"}):
            raise ValueError("inconsistent category")
        return self


def found(data: dict[str, Any], message: str = "Evidence found.") -> ToolResult:
    return ToolResult(ok=True, category="found", retryable=False, message=message, data=data)


def not_found(
    message: str = "No matching evidence.", data: dict[str, Any] | None = None
) -> ToolResult:
    return ToolResult(
        ok=True, category="not_found", retryable=False, message=message, data=data or {}
    )


def bad_args(message: str) -> ToolResult:
    return ToolResult(ok=False, category="bad_args", retryable=False, message=message, data={})


def forbidden() -> ToolResult:
    return ToolResult(
        ok=False, category="forbidden", retryable=False, message="Access denied.", data={}
    )


def timeout() -> ToolResult:
    return ToolResult(
        ok=False, category="timeout", retryable=True, message="Tool timed out; try again.", data={}
    )
