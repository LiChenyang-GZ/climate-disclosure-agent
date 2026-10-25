import logging
import signal
import threading
from types import FrameType
from typing import Any

logger = logging.getLogger(__name__)


def main(interval: float = 30.0, stop_event: threading.Event | None = None) -> int:
    event = stop_event or threading.Event()
    original_handlers: dict[signal.Signals, Any] = {}

    if stop_event is None and threading.current_thread() is threading.main_thread():

        def request_stop(_signum: int, _frame: FrameType | None) -> None:
            event.set()

        for stop_signal in (signal.SIGTERM, signal.SIGINT):
            original_handlers[stop_signal] = signal.signal(stop_signal, request_stop)

    logger.info("worker started")
    try:
        while not event.wait(interval):
            logger.info("worker idle")
    finally:
        for stop_signal, handler in original_handlers.items():
            signal.signal(stop_signal, handler)
    logger.info("worker stopped")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
