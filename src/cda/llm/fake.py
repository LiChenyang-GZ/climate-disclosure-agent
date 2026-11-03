"""Scripted network-free completion client for tests."""

import asyncio
import copy
import json
import re
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


class DemoFakeLLM:
    """Unscripted fake for local smoke runs (D83): up to three keyword searches, then a finding.

    It cites the start of the first hit's snippet, so quote verification passes on
    real packs. Never used for real checks; the worker logs a warning when it is on.
    """

    def __init__(self, models: ModelTable, *, delay_s: float = 0.0) -> None:
        self._models = models
        self._delay_s = delay_s

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
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        names = {t.name for t in tools}
        searches = sum(m.role == "tool" for m in messages)
        queries = [_check_title(messages), "climate risk", "emissions targets"]
        if (
            "keyword_search" in names
            and tool_choice != "submit_finding"
            and searches < len(queries)
            and _first_hit(messages) is None
        ):
            turn = FakeTurn(
                tool_calls=[("keyword_search", {"query": queries[searches]})],
                usage=Usage(1500, 40, 0),
            )
        else:
            turn = FakeTurn(
                tool_calls=[("submit_finding", _finding(messages))], usage=Usage(2000, 200, 0)
            )
        return await FakeLLM([turn], self._models).complete(
            messages=messages,
            tools=tools,
            model_key=model_key,
            seed=seed,
            tool_choice=tool_choice,
            max_output_tokens=max_output_tokens,
        )


def _check_title(messages: list[Message]) -> str:
    for m in messages:
        found = re.search(r"Check \w+: ([^\n]+)", m.content or "")
        if found:
            return " ".join(found[1].split()[:6])
    return "climate risk"


def _first_hit(messages: list[Message]) -> dict[str, Any] | None:
    for m in messages:
        if m.role != "tool" or not m.content:
            continue
        try:
            hits = json.loads(m.content).get("data", {}).get("hits", [])
        except (ValueError, AttributeError):
            continue
        if hits:
            return dict(hits[0])
    return None


def _finding(messages: list[Message]) -> dict[str, Any]:
    hit = _first_hit(messages)
    if hit is None:
        return {
            "status": "no_evidence_found",
            "rationale": "Fake model: no search hits.",
            "missing_aspects": [],
            "relief_applied": False,
            "relief_id": None,
            "evidence": [],
            "facts": [],
        }
    # Drop words the snippet's "…" markers may have cut in half.
    snippet = hit["snippet"]
    words = snippet.strip("…").split()
    words = words[1 if snippet.startswith("…") else 0 : -1 if snippet.endswith("…") else None]
    return {
        "status": "evidence_found",
        "rationale": "Fake model: cites the first search hit.",
        "missing_aspects": [],
        "relief_applied": False,
        "relief_id": None,
        "evidence": [{"unit_id": hit["unit_id"], "quote": " ".join(words[:8]), "role": "supports"}],
        "facts": [],
    }
