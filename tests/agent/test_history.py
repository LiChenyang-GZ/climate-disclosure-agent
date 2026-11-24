"""T30: shorten old tool results in the prompt (D15, D59); s.history stays complete."""

import copy
import json
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from cda.agent.budget import MemoryBudget
from cda.agent.history import MAX_UNIT_IDS, NOTE, shorten_history
from cda.agent.run_settings import RunSettings
from cda.agent.state import WorkerState
from cda.agent.worker_graph import WorkerDeps, build_graph
from cda.checks.loader import load_check_set
from cda.llm.cost import ModelTable, estimate_tokens
from cda.llm.fake import FakeLLM, FakeTurn
from cda.llm.types import Message, ToolCall, Usage
from cda.tools.context import ToolContext
from cda.tools.envelope import bad_args, found, not_found

SECRET = "SECRET UNIT TEXT that must not survive shortening"


def ids(n, prefix="u"):
    return [f"{prefix}{i}" for i in range(n)]


def exchange(n, tool="keyword_search", args=None, result=None):
    """One assistant tool call and its ToolResult message."""
    call = ToolCall(f"call_{n}", tool, json.dumps(args or {"query": f"q{n}"}))
    result = result or found({"hits": [{"unit_id": f"u{n}a", "snippet": SECRET}]})
    return [
        Message("assistant", None, [call]),
        Message("tool", result.model_dump_json(), tool_call_id=call.id),
    ]


def six_results():
    history = []
    for n in range(6):
        if n % 2:
            history += exchange(
                n,
                "read_unit",
                {"unit_id": f"u{n}", "mode": "unit", "neighbours": 1},
                found(
                    {
                        "units": [
                            {"unit_id": f"u{n}", "text": SECRET},
                            {"unit_id": f"u{n}n", "text": SECRET},
                        ]
                    }
                ),
            )
        else:
            history += exchange(n)
    return history


def test_newest_three_unchanged_older_three_shortened():
    history = six_results()
    out = shorten_history(history, 3)
    tool_messages = [(i, m) for i, m in enumerate(out) if m.role == "tool"]
    assert len(tool_messages) == 6
    for i, m in tool_messages[3:]:
        assert m == history[i]  # byte-for-byte
    expected = [
        ("keyword_search", {"query": "q0"}, ["u0a"]),
        ("read_unit", {"unit_id": "u1", "mode": "unit", "neighbours": 1}, ["u1", "u1n"]),
        ("keyword_search", {"query": "q2"}, ["u2a"]),
    ]
    for (i, m), (tool, args, unit_ids) in zip(tool_messages[:3], expected, strict=True):
        assert m.tool_call_id == history[i].tool_call_id
        assert json.loads(m.content) == {
            "shortened": True,
            "tool": tool,
            "args": args,
            "category": "found",
            "unit_ids": unit_ids,
            "note": NOTE,
        }
    # Assistant messages are untouched.
    assert [m for m in out if m.role != "tool"] == [m for m in history if m.role != "tool"]


@pytest.mark.parametrize(
    "feedback",
    [
        "Invalid JSON arguments for keyword_search; call the tool again with a JSON object.",
        json.dumps([{"unit_id": "u0a", "reason": "quote_not_found"}])
        + "; call submit_finding again with corrected quotes",
        "submit_finding rejected: Partial evidence needs missing aspects; call submit_finding again",
        "Expected exactly one tool call; call submit_finding again",
    ],
)
def test_feedback_messages_never_shortened(feedback):
    call = ToolCall("call_fb", "submit_finding", "{}")
    history = [
        Message("assistant", None, [call]),
        Message("tool", feedback, tool_call_id=call.id),
        Message("user", feedback),
        *six_results(),
    ]
    out = shorten_history(history, 1)
    assert out[:3] == history[:3]
    assert (
        sum(json.loads(m.content).get("shortened", False) for m in out[3:] if m.role == "tool") == 5
    )


def test_input_history_not_mutated():
    history = six_results()
    before = copy.deepcopy(history)
    shorten_history(history, 1)
    assert history == before


def test_unit_ids_capped_at_twenty_and_empty_for_not_found():
    many = found({"hits": [{"unit_id": u, "snippet": SECRET} for u in ids(25)]})
    history = [
        *exchange(0, result=many),
        *exchange(1, result=not_found()),
        *exchange(2, result=bad_args("Repeated query.")),
        *exchange(3),
    ]
    out = [json.loads(m.content) for m in shorten_history(history, 1) if m.role == "tool"][:3]
    assert out[0]["unit_ids"] == ids(25)[:MAX_UNIT_IDS] == ids(20)
    assert (out[1]["category"], out[1]["unit_ids"]) == ("not_found", [])
    assert (out[2]["category"], out[2]["unit_ids"]) == ("bad_args", [])


def test_keep_larger_than_results_shortens_nothing():
    history = six_results()
    assert shorten_history(history, 6) == history
    assert shorten_history(history, 50) == history
    assert shorten_history([], 3) == []


@pytest.mark.parametrize("keep", [0, -1])
def test_keep_below_one_rejected(keep):
    assert RunSettings.model_validate(_deps_defaults()).keep_tool_results == 3
    with pytest.raises(ValidationError):
        RunSettings.model_validate({**_deps_defaults(), "keep_tool_results": keep})
    with pytest.raises(ValueError):
        shorten_history(six_results(), keep)


def _deps_defaults():
    from cda.agent.run_graph import RunDeps

    return {name: getattr(RunDeps, name) for name in RunSettings.model_fields}


def test_settings_default_is_three():
    assert _deps_defaults()["keep_tool_results"] == 3
    assert WorkerDeps.__dataclass_fields__["keep_tool_results"].default == 3


def test_shortened_result_never_contains_unit_text():
    out = shorten_history(six_results(), 1)
    shortened = [m.content for m in out if m.role == "tool" and '"shortened"' in m.content]
    assert len(shortened) == 5
    assert all(SECRET not in text for text in shortened)


def test_missing_tool_call_still_shortens():
    # A result whose assistant call is not in the history keeps a null tool and args.
    result = found({"hits": [{"unit_id": "x", "snippet": SECRET}]})
    history = [Message("tool", result.model_dump_json(), tool_call_id="gone"), *exchange(1)]
    first = json.loads(shorten_history(history, 1)[0].content)
    assert (first["tool"], first["args"], first["unit_ids"]) == (None, None, ["x"])


# ---- worker -------------------------------------------------------------------------

BIG = "climate risk " + "x" * 16_000  # about 4,000 tokens per search result


class SizedLLM(FakeLLM):
    """Scripted replies whose input tokens are the size of the prompt actually sent."""

    async def complete(self, **kw):
        response = await super().complete(**kw)
        tokens_in = estimate_tokens(kw["messages"], kw["tools"])
        return replace(response, usage=Usage(tokens_in, 20, 0))


def worker(monkeypatch, keep, searches=10, token_cap=150_000):
    import cda.agent.worker_graph as w

    uid = str(uuid4())

    async def tool(ctx, name, args):
        if name == "list_documents":
            return found({"documents": [{"parts": [{"part_id": "s", "label": "S"}]}]})
        ctx.coverage.record(name, args["query"], {"s"})
        return found({"hits": [{"unit_id": uid, "snippet": BIG}]})

    async def verify(conn, **kw):
        from cda.verify.evidence import EvidenceCheck

        return [
            EvidenceCheck(i.unit_id, i.quote, i.role, True, "ok", False, 1, uuid4())
            for i in kw["items"]
        ]

    monkeypatch.setattr(w, "run_tool", tool)
    monkeypatch.setattr(w, "verify_evidence", verify)
    monkeypatch.setattr(w, "record_call", AsyncMock())
    submit = {
        "status": "evidence_found",
        "rationale": "Located",
        "missing_aspects": [],
        "relief_applied": False,
        "relief_id": None,
        "evidence": [{"unit_id": uid, "quote": "climate risk", "role": "supports"}],
        "facts": [],
    }
    script = [
        *[FakeTurn(tool_calls=[("keyword_search", {"query": f"q{i}"})]) for i in range(searches)],
        FakeTurn(tool_calls=[("submit_finding", submit)]),
    ]
    llm = SizedLLM(script)
    deps = WorkerDeps(
        llm=llm,
        models=ModelTable.load(),
        model_key="sol",
        seed=1,
        conn=AsyncMock(),
        user_id=uuid4(),
        pack_id=uuid4(),
        run_id=uuid4(),
        method="A",
        embedder=None,
        budget=MemoryBudget(Decimal(100)),
        token_cap=token_cap,
        keep_tool_results=keep,
    )
    cs = load_check_set()
    state = WorkerState(ToolContext(deps.conn, deps.user_id, deps.pack_id, None, 10.0))
    return deps, llm, cs.checks[0], cs, state


async def invoke(deps, check, cs, state):
    await build_graph(deps, check, cs).ainvoke({"worker": state}, config={"recursion_limit": 300})
    assert state.result is not None
    return state.result


async def test_shortening_reaches_submit_before_the_token_cap(monkeypatch):
    deps, full_llm, check, cs, state = worker(monkeypatch, keep=1000)
    full = await invoke(deps, check, cs, state)
    deps, short_llm, check, cs, state = worker(monkeypatch, keep=3)
    short = await invoke(deps, check, cs, state)
    # Without shortening the cumulative prompt crosses the cap and the last step is forced.
    assert (full.status, full.needs_review_reason) == ("needs_review", "cap_hit")
    assert full_llm.requests[-1]["tool_choice"] == "submit_finding"
    assert (short.status, short.needs_review_reason) == ("evidence_found", None)
    assert all(r["tool_choice"] != "submit_finding" for r in short_llm.requests)
    assert short.steps == 11
    assert short.tokens_in < full.tokens_in


async def test_history_complete_prompt_shortened(monkeypatch):
    deps, llm, check, cs, state = worker(monkeypatch, keep=3, searches=6, token_cap=1_000_000)
    result = await invoke(deps, check, cs, state)
    assert result.status == "evidence_found"
    results = [m for m in state.history if m.role == "tool"]
    assert len(results) == 6 and all(BIG in m.content for m in results)
    last = [m for m in llm.requests[-1]["messages"] if m.role == "tool"]
    assert [BIG in m.content for m in last] == [False, False, False, True, True, True]
    assert all(json.loads(m.content)["shortened"] for m in last[:3])
