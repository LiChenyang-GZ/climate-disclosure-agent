"""Gemini adapter on the Google Gen AI SDK (generateContent), with our own bounded retries (D90)."""

import asyncio
import base64
import binascii
import json
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from google import genai
from google.genai import errors, types
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception, stop_after_attempt

from cda.llm.cost import ModelTable
from cda.llm.fingerprint import make_fingerprint, tools_payload
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

# Google's documented placeholder for a function call the model did not sign in this
# history (replayed run, older checkpoint). Sent verbatim as the thoughtSignature.
SKIP_SIGNATURE = "skip_thought_signature_validator"
_AUTH_STATUSES = {401, 402, 403}
_RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
_TRANSIENT = (httpx.TimeoutException, httpx.TransportError)


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, errors.APIError):
        return exc.code in _RETRY_STATUSES
    return isinstance(exc, _TRANSIENT)


def _wait(state: RetryCallState) -> float:
    exception = state.outcome.exception() if state.outcome else None
    response = getattr(exception, "response", None)
    headers = getattr(response, "headers", None)
    header = headers.get("Retry-After") if headers is not None else None
    if header is not None:
        try:
            delay = float(header)
            if delay >= 0:
                return min(delay, 30.0)
        except ValueError:
            pass
    return float(2 ** min(state.attempt_number - 1, 5)) + random.uniform(0, 0.25)


def encode_signature(raw: bytes | None) -> str | None:
    return base64.urlsafe_b64encode(raw).decode() if raw else None


def decode_signature(value: str | None) -> bytes:
    """The SDK sends bytes as URL-safe base64, so this round-trips the wire value."""
    try:
        return base64.urlsafe_b64decode(value or SKIP_SIGNATURE)
    except (binascii.Error, ValueError):
        return base64.urlsafe_b64decode(SKIP_SIGNATURE)


def _arguments(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _tool_response(content: str | None) -> dict[str, Any]:
    try:
        value = json.loads(content or "")
    except ValueError:
        value = None
    return value if isinstance(value, dict) else {"result": content}


def build_request(
    messages: list[Message],
) -> tuple[str | None, list[types.Content]]:
    """Map our provider-neutral history to Gemini contents and a system instruction."""
    system = [m.content for m in messages if m.role == "system" and m.content]
    names: dict[str, str] = {}
    contents: list[types.Content] = []
    for message in messages:
        if message.role == "system":
            continue
        if message.role == "tool":
            part = types.Part(
                function_response=types.FunctionResponse(
                    id=message.tool_call_id,
                    name=names.get(message.tool_call_id or "", ""),
                    response=_tool_response(message.content),
                )
            )
            last = contents[-1] if contents else None
            if (
                last is not None
                and last.role == "user"
                and last.parts
                and all(p.function_response is not None for p in last.parts)
            ):
                last.parts.append(part)
            else:
                contents.append(types.Content(role="user", parts=[part]))
            continue
        if message.role == "user":
            contents.append(types.Content(role="user", parts=[types.Part(text=message.content)]))
            continue
        parts: list[types.Part] = []
        if message.content:
            parts.append(types.Part(text=message.content))
        for index, call in enumerate(message.tool_calls):
            names[call.id] = call.name
            parts.append(
                types.Part(
                    function_call=types.FunctionCall(
                        id=call.id, name=call.name, args=_arguments(call.arguments)
                    ),
                    # Gemini signs the first call of each step; it must come back on it.
                    thought_signature=decode_signature(call.signature) if index == 0 else None,
                )
            )
        if parts:
            contents.append(types.Content(role="model", parts=parts))
    return ("\n\n".join(system) if system else None), contents


def build_tools(tools: list[ToolSpec], tool_choice: str) -> dict[str, Any]:
    if not tools:
        return {}
    declarations = [
        types.FunctionDeclaration(
            name=item["name"],
            description=item["description"],
            parameters_json_schema=item["parameters"],
        )
        for item in tools_payload(tools)
    ]
    if tool_choice == "auto":
        mode = types.FunctionCallingConfig(mode=types.FunctionCallingConfigMode.AUTO)
    elif tool_choice == "required":
        mode = types.FunctionCallingConfig(mode=types.FunctionCallingConfigMode.ANY)
    else:
        mode = types.FunctionCallingConfig(
            mode=types.FunctionCallingConfigMode.ANY, allowed_function_names=[tool_choice]
        )
    return {
        "tools": [types.Tool(function_declarations=declarations)],
        "tool_config": types.ToolConfig(function_calling_config=mode),
    }


def _finish_reason(candidate: types.Candidate | None, has_calls: bool) -> str:
    if has_calls:
        return "tool_calls"
    reason = candidate.finish_reason if candidate else None
    if reason is None or reason == types.FinishReason.STOP:
        return "completed"
    if reason == types.FinishReason.MAX_TOKENS:
        return "max_output_tokens"
    return str(reason.value).lower()


class GeminiClient:
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
        self._client: genai.Client | None = None

    def _connect(self) -> genai.Client:
        if self._client is None:
            # The SDK reads GEMINI_API_KEY / GOOGLE_API_KEY, or the Vertex AI settings,
            # itself; no key passes through this code. SDK retries stay off (ours below).
            options = types.HttpOptions(
                timeout=int(self._timeout_s * 1000), httpx_async_client=self._http_client
            )
            failed = False
            try:
                self._client = genai.Client(http_options=options)
            except (ValueError, errors.APIError):
                failed = True
            if failed:
                raise LLMAuthError("LLM authentication or permission denied") from None
        assert self._client is not None
        return self._client

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
        client = self._connect()
        price = self._models[model_key]
        model = price.model
        system, contents = build_request(messages)
        config = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_output_tokens,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            thinking_config=(
                types.ThinkingConfig(
                    thinking_level=types.ThinkingLevel(price.thinking_level.upper())
                )
                if price.thinking_level
                else None
            ),
            **build_tools(tools, tool_choice),
        )
        started = time.perf_counter()
        error: LLMError | None = None
        try:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception(_retryable),
                stop=stop_after_attempt(self._max_retries + 1),
                wait=_wait,
                sleep=self._sleep,
                reraise=True,
            ):
                with attempt:
                    reply = await client.aio.models.generate_content(
                        model=model, contents=contents, config=config
                    )
        except errors.APIError as exc:
            code = exc.code
            if code in _AUTH_STATUSES:
                error = LLMAuthError("LLM authentication or permission denied")
            elif code in _RETRY_STATUSES:
                error = LLMRetryExhausted(code, exc.status or "APIError")
            else:
                error = LLMBadRequest(f"LLM request rejected (status={code})")
        except _TRANSIENT as exc:
            error = LLMRetryExhausted(None, type(exc).__name__)
        if error is not None:
            # Raise outside the SDK exception handler: no sensitive chained context.
            raise error from None
        candidate = reply.candidates[0] if reply.candidates else None
        parts = (candidate.content.parts if candidate and candidate.content else None) or []
        calls = [
            ToolCall(
                part.function_call.id or f"call_{index}",
                part.function_call.name or "",
                json.dumps(part.function_call.args or {}),
                encode_signature(part.thought_signature),
            )
            for index, part in enumerate(parts)
            if part.function_call is not None
        ][:1]  # D90: like parallel_tool_calls=False; the worker uses only the first call.
        text = "".join(part.text for part in parts if part.text and not part.thought)
        usage = reply.usage_metadata
        return LLMResponse(
            message=Message("assistant", text or None, calls),
            finish_reason=_finish_reason(candidate, bool(calls)),
            usage=Usage(
                (usage.prompt_token_count or 0) if usage else 0,
                ((usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0))
                if usage
                else 0,
                (usage.cached_content_token_count or 0) if usage else 0,
            ),
            model=reply.model_version or model,
            system_fingerprint=None,
            latency_ms=int((time.perf_counter() - started) * 1000),
            fingerprint=make_fingerprint(model, messages, tools, seed),
        )
