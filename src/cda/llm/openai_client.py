"""OpenAI Chat Completions adapter with bounded application-owned retries."""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

import httpx
import openai
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception_type, stop_after_attempt

from cda.llm.cost import ModelTable
from cda.llm.fingerprint import make_fingerprint, messages_payload
from cda.llm.schema import strict_tool_schema
from cda.llm.types import (
    LLMAuthError,
    LLMBadRequest,
    LLMError,
    LLMResponse,
    LLMRetryExhausted,
    Message,
    ToolCall,
    ToolSpec,
    Usage,
)

_RETRYABLE = (
    openai.RateLimitError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.InternalServerError,
)


def _wait(state: RetryCallState) -> float:
    exception = state.outcome.exception() if state.outcome else None
    if isinstance(exception, openai.APIStatusError):
        header = exception.response.headers.get("Retry-After")
        if header is not None:
            try:
                delay = float(header)
                if delay >= 0:
                    return min(delay, 30.0)
            except ValueError:
                pass
    return float(2 ** min(state.attempt_number - 1, 5)) + random.uniform(0, 0.25)


class OpenAIClient:
    def __init__(
        self,
        models: ModelTable,
        *,
        timeout_s: float = 60.0,
        max_retries: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be nonnegative")
        self._models = models
        self._max_retries = max_retries
        self._sleep = sleep
        self._timeout_s = timeout_s
        self._http_client = http_client
        self._client: openai.AsyncOpenAI | None = None

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
        if self._client is None:
            auth_failed = False
            try:
                self._client = openai.AsyncOpenAI(
                    max_retries=0, timeout=self._timeout_s, http_client=self._http_client
                )
            except openai.OpenAIError:
                auth_failed = True
            if auth_failed:
                raise LLMAuthError("LLM authentication or permission denied") from None
        assert self._client is not None
        model = self._models[model_key].model
        schemas = [strict_tool_schema(tool) for tool in tools]
        kwargs: dict[str, Any] = {}
        if tools:
            kwargs["tools"] = cast(list[ChatCompletionToolParam], schemas)
            kwargs["parallel_tool_calls"] = False
            kwargs["tool_choice"] = (
                tool_choice
                if tool_choice in {"auto", "required"}
                else {"type": "function", "function": {"name": tool_choice}}
            )
        started = time.perf_counter()
        error: LLMError | None = None
        try:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception_type(_RETRYABLE),
                stop=stop_after_attempt(self._max_retries + 1),
                wait=_wait,
                sleep=self._sleep,
                reraise=True,
            ):
                with attempt:
                    completion = await self._client.chat.completions.create(
                        model=model,
                        messages=cast(list[ChatCompletionMessageParam], messages_payload(messages)),
                        temperature=0,
                        seed=seed,
                        max_completion_tokens=max_output_tokens,
                        **kwargs,
                    )
        except (openai.AuthenticationError, openai.PermissionDeniedError):
            error = LLMAuthError("LLM authentication or permission denied")
        except _RETRYABLE as exc:
            error = LLMRetryExhausted(getattr(exc, "status_code", None), type(exc).__name__)
        except openai.APIStatusError as exc:
            error = LLMBadRequest(f"LLM request rejected (status={exc.status_code})")
        if error is not None:
            # Raise outside the SDK exception handler: no sensitive chained context.
            raise error from None
        choice = completion.choices[0]
        calls = [
            ToolCall(call.id, call.function.name, call.function.arguments)
            for call in choice.message.tool_calls or []
            if call.type == "function"
        ]
        usage = completion.usage
        details = usage.prompt_tokens_details if usage else None
        return LLMResponse(
            message=Message("assistant", choice.message.content, calls),
            finish_reason=choice.finish_reason,
            usage=Usage(
                usage.prompt_tokens if usage else 0,
                usage.completion_tokens if usage else 0,
                (details.cached_tokens or 0) if details else 0,
            ),
            model=completion.model,
            system_fingerprint=completion.system_fingerprint,
            latency_ms=int((time.perf_counter() - started) * 1000),
            fingerprint=make_fingerprint(model, messages, tools, seed),
        )
