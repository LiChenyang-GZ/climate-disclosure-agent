"""Answer model requests from a recorded run, without calling the model (D89)."""

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from cda.db.models import LLMCall
from cda.llm.fingerprint import make_fingerprint
from cda.llm.types import LLMBadRequest, LLMResponse, Message, ToolCall, ToolSpec, Usage


class ReplayMiss(LLMBadRequest):
    """No recorded answer for this request; the check ends error / llm_rejected."""


@dataclass(frozen=True)
class RecordedCall:
    prompt_sha256: str
    tools_sha256: str
    response: dict[str, Any]
    usage: Usage
    system_fingerprint: str | None


class ReplayLLM:
    """Key = (prompt_sha256, tools_sha256) as make_fingerprint computes it.

    Several recorded calls with the same key are answered in recorded order.
    """

    def __init__(self, calls: Iterable[RecordedCall]) -> None:
        self._answers: dict[tuple[str, str], deque[RecordedCall]] = {}
        for call in calls:
            self._answers.setdefault((call.prompt_sha256, call.tools_sha256), deque()).append(call)
        self.misses = 0
        self.hits = 0

    @classmethod
    async def from_run(cls, conn: AsyncConnection, run_id: UUID) -> "ReplayLLM":
        rows = (
            await conn.execute(
                select(
                    LLMCall.prompt_sha256,
                    LLMCall.tools_sha256,
                    LLMCall.response,
                    LLMCall.tokens_in,
                    LLMCall.tokens_out,
                    LLMCall.tokens_cached,
                    LLMCall.system_fingerprint,
                )
                .where(LLMCall.run_id == run_id, LLMCall.response.is_not(None))
                # created_at is the transaction start, equal within one check: order by step.
                .order_by(LLMCall.check_id, LLMCall.step, LLMCall.created_at, LLMCall.id)
            )
        ).all()
        return cls(
            RecordedCall(
                r.prompt_sha256,
                r.tools_sha256,
                r.response,
                Usage(r.tokens_in, r.tokens_out, r.tokens_cached),
                r.system_fingerprint,
            )
            for r in rows
        )

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
        fingerprint = make_fingerprint(model_key, messages, tools, seed)
        queue = self._answers.get((fingerprint.prompt_sha256, fingerprint.tools_sha256))
        if not queue:
            self.misses += 1
            raise ReplayMiss("No recorded response for this request")
        call = queue.popleft()
        self.hits += 1
        data = call.response
        message = Message(
            "assistant",
            data.get("content"),
            [ToolCall(c["id"], c["name"], c["arguments"]) for c in data.get("tool_calls", [])],
        )
        model = data.get("model") or model_key
        return LLMResponse(
            message,
            data.get("finish_reason") or ("tool_calls" if message.tool_calls else "stop"),
            call.usage,
            model,
            call.system_fingerprint,
            0,
            make_fingerprint(model, messages, tools, seed),
        )
