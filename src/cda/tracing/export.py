"""Reconstruct idempotent Langfuse v2 observations from persisted run data."""

import argparse
import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from cda.config import Settings
from cda.db.models import Finding, LLMCall, Pack, Run, RunEvent
from cda.db.session import make_engine
from cda.llm.cost import ModelTable
from cda.tracing.settings import TracingSettings


class TraceClient(Protocol):
    def trace(self, **kwargs: Any) -> None: ...
    def span(self, **kwargs: Any) -> None: ...
    def generation(self, **kwargs: Any) -> None: ...
    def flush(self) -> None: ...


class LangfuseAdapter:
    """Keep SDK-specific observation creation behind a small testable interface."""

    def __init__(self, sdk: Any) -> None:
        self.sdk = sdk

    def trace(self, **kwargs: Any) -> None:
        self.sdk.trace(**kwargs)

    def span(self, **kwargs: Any) -> None:
        self.sdk.span(**kwargs)

    def generation(self, **kwargs: Any) -> None:
        self.sdk.generation(**kwargs)

    def flush(self) -> None:
        self.sdk.flush()


def make_trace_client(settings: TracingSettings) -> TraceClient:
    from langfuse import Langfuse  # type: ignore[import-untyped]

    if not settings.enabled:
        raise ValueError("Tracing is disabled")
    return LangfuseAdapter(
        Langfuse(
            host=settings.host,
            public_key=settings.public_key.get_secret_value(),
            secret_key=settings.secret_key.get_secret_value(),
            timeout=5,
            max_retries=0,
        )
    )


@dataclass(frozen=True)
class ExportSummary:
    checks: int
    generations: int
    tools: int


async def export_run(engine: AsyncEngine, run_id: UUID, client: TraceClient) -> ExportSummary:
    # Read a consistent snapshot; never query evidence, units or prompt contents.
    async with engine.connect() as connection:
        connection = await connection.execution_options(isolation_level="REPEATABLE READ")
        async with async_sessionmaker(bind=connection)() as session:
            run = (await session.scalars(select(Run).where(Run.id == run_id))).one()
            pack = (await session.scalars(select(Pack).where(Pack.id == run.pack_id))).one()
            findings = list(
                await session.scalars(
                    select(Finding).where(Finding.run_id == run_id).order_by(Finding.check_id)
                )
            )
            calls = list(
                await session.scalars(
                    select(LLMCall)
                    .where(LLMCall.run_id == run_id)
                    .order_by(LLMCall.created_at, LLMCall.step, LLMCall.id)
                )
            )
            events = list(
                await session.scalars(
                    select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.id)
                )
            )
    models = ModelTable.load()
    model = models[run.model_key]
    client.trace(
        id=str(run_id),
        name=f"run {run.method} {pack.company} {pack.reporting_year}",
        tags=[run.method, run.model_key],
        timestamp=run.created_at,
        metadata={
            "method": run.method,
            "model_key": run.model_key,
            "model_name": model.model,
            "check_set_version": run.check_set_version,
            "prompt_sha256": run.prompt_sha256,
            "budget_usd": float(run.budget_usd),
            "spent_usd": float(run.spent_usd),
            "status": run.status,
            "error": run.error,
            "created_at": run.created_at.isoformat(),
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "events": [
                {"id": e.id, "type": e.type, "created_at": e.created_at.isoformat()}
                for e in events
                if e.check_id is None
            ],
        },
    )
    by_check = {f.check_id: f for f in findings}
    check_ids = sorted(
        set(by_check)
        | {e.check_id for e in events if e.check_id is not None}
        | {c.check_id for c in calls if c.check_id is not None}
    )
    parents = {
        cid: str(by_check[cid].id if cid in by_check else uuid5(run_id, cid)) for cid in check_ids
    }
    for cid in check_ids:
        finding = by_check.get(cid)
        starts = [e.created_at for e in events if e.check_id == cid and e.type == "check_started"]
        ends = [e.created_at for e in events if e.check_id == cid and e.type == "check_finished"]
        output = (
            None
            if finding is None
            else {
                "status": finding.status,
                "confidence": finding.confidence,
                "needs_review_reason": finding.needs_review_reason,
                "steps": finding.steps,
                "tokens_in": finding.tokens_in,
                "tokens_out": finding.tokens_out,
                "cost_usd": float(finding.cost_usd),
                "rationale": finding.rationale,
            }
        )
        client.span(
            id=parents[cid],
            trace_id=str(run_id),
            name=cid,
            start_time=min(starts) if starts else None,
            end_time=max(ends) if ends else None,
            output=output,
        )
    for call in calls:
        price = next((p for p in models.root.values() if p.model == call.model), None)
        client.generation(
            id=str(call.id),
            trace_id=str(run_id),
            parent_observation_id=parents.get(call.check_id or ""),
            name=f"model call {call.step}",
            model=call.model,
            start_time=call.created_at - timedelta(milliseconds=call.latency_ms)
            if call.latency_ms is not None
            else None,
            end_time=call.created_at,
            usage={
                "input": call.tokens_in,
                "output": call.tokens_out,
                "total_cost": float(call.cost_usd),
                "unit": "TOKENS",
            },
            metadata={
                "step": call.step,
                "prompt_sha256": call.prompt_sha256,
                "tools_sha256": call.tools_sha256,
                "tokens_cached": call.tokens_cached,
                "reasoning_effort": price.reasoning_effort if price else None,
            },
        )
    tools = 0
    for event in events:
        if event.type != "tool_called":
            continue
        tools += 1
        payload = event.payload
        client.span(
            id=str(uuid5(run_id, f"tool:{event.id}")),
            trace_id=str(run_id),
            parent_observation_id=parents.get(event.check_id or ""),
            name=payload.get("tool"),
            start_time=event.created_at,
            end_time=event.created_at,
            input={k: payload.get(k) for k in ("query", "part_ids")},
            output={k: payload.get(k) for k in ("category", "hits")},
        )
    return ExportSummary(len(check_ids), len(calls), tools)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=UUID, required=True)
    args = parser.parse_args()

    async def resend() -> None:
        settings = TracingSettings()
        database_url = Settings().database_url
        if not database_url or not settings.enabled:
            raise ValueError("DATABASE_URL and all three Langfuse settings are required")
        engine = make_engine(database_url)
        try:
            client = make_trace_client(settings)
            await export_run(engine, args.run, client)
            await asyncio.to_thread(client.flush)
        finally:
            await engine.dispose()

    try:
        asyncio.run(resend())
    except Exception as exc:  # noqa: BLE001 - type-only CLI error reporting
        logging.getLogger(__name__).error("trace export failed %s", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
