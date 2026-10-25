"""Long-running, single-run worker process."""

import asyncio
import logging
import selectors
import signal
import sys
import threading
from collections.abc import Awaitable, Callable
from contextlib import suppress
from types import FrameType
from typing import Any
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncEngine

from cda.agent.checkpoint import open_checkpointer
from cda.agent.run_graph import RunDeps, run_run
from cda.config import Settings
from cda.db.session import make_engine
from cda.runner.claim import claim_next
from cda.runner.deps import build_run_deps
from cda.runner.events import EventSink
from cda.runner.lease import hold_lease, release_lease

logger = logging.getLogger(__name__)
DepsFactory = Callable[[Settings, BaseCheckpointSaver[Any]], RunDeps]
RunFunction = Callable[..., Awaitable[str]]


async def serve(
    engine: AsyncEngine,
    settings: Settings,
    *,
    deps_factory: DepsFactory = build_run_deps,
    run_fn: RunFunction = run_run,
    stop: asyncio.Event,
) -> None:
    if stop.is_set():
        return
    url = settings.database_url
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    wake = asyncio.Event()
    raw = await engine.raw_connection()
    listener = raw.driver_connection
    if listener is None:
        raw.close()
        raise RuntimeError("Worker requires asyncpg")

    def notified(*args: Any) -> None:
        wake.set()

    async def process(run_id: UUID, deps: RunDeps) -> None:
        async with hold_lease(
            engine, run_id, settings.worker_id, settings.lease_seconds, settings.heartbeat_seconds
        ):
            try:
                status = await run_fn(engine, run_id, deps=deps)
                logger.info("finished %s %s", run_id, status)
            except Exception as exc:  # noqa: BLE001 - retain worker safety with safe type-only logs
                logger.error("run failed %s", type(exc).__name__)

    stop_task = asyncio.create_task(stop.wait())
    try:
        await listener.add_listener(settings.notify_channel, notified)
        async with open_checkpointer(url) as saver:
            deps = deps_factory(settings, saver)
            try:
                while not stop.is_set():
                    wake.clear()
                    run_id = await claim_next(engine, settings.worker_id, settings.lease_seconds)
                    if run_id is not None:
                        logger.info("claimed %s", run_id)
                        task = asyncio.create_task(process(run_id, deps))
                        try:
                            done, _ = await asyncio.wait(
                                (task, stop_task), return_when=asyncio.FIRST_COMPLETED
                            )
                            if stop_task in done:
                                task.cancel()
                            with suppress(asyncio.CancelledError):
                                await task
                        finally:
                            if not task.done():
                                task.cancel()
                                await asyncio.gather(task, return_exceptions=True)
                            await release_lease(engine, run_id, settings.worker_id)
                    else:
                        waiter = asyncio.create_task(wake.wait())
                        try:
                            await asyncio.wait(
                                (waiter, stop_task),
                                timeout=settings.poll_seconds,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                        finally:
                            waiter.cancel()
                            await asyncio.gather(waiter, return_exceptions=True)
            finally:
                sink = getattr(deps.emit, "__self__", None)
                if isinstance(sink, EventSink) and sink.engine is not engine:
                    await sink.engine.dispose()
    finally:
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        await listener.remove_listener(settings.notify_channel, notified)
        raw.close()


def main() -> int:
    def loop_factory() -> asyncio.AbstractEventLoop:
        if sys.platform == "win32":
            return asyncio.SelectorEventLoop(selectors.SelectSelector())
        return asyncio.new_event_loop()

    with asyncio.Runner(loop_factory=loop_factory) as runner:
        stop = asyncio.Event()
        loop = runner.get_loop()
        original_handlers: dict[signal.Signals, Any] = {}

        def request_stop(_signum: int, _frame: FrameType | None) -> None:
            stop.set()
            loop.call_soon_threadsafe(stop.set)

        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                original_handlers[sig] = signal.signal(sig, request_stop)

        async def run() -> None:
            if stop.is_set():
                return
            settings = Settings()
            engine = make_engine(settings.database_url)
            try:
                await serve(engine, settings, stop=stop)
            finally:
                await engine.dispose()

        logger.info("worker started")
        try:
            runner.run(run())
        finally:
            for sig, handler in original_handlers.items():
                signal.signal(sig, handler)
            logger.info("worker stopped")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
