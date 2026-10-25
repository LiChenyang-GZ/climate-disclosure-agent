import logging
import threading
import time

from cda.runner.main import main


def test_main_logs_start_idle_stop_and_returns_zero(caplog) -> None:
    stop_event = threading.Event()

    def stop_after_ticks() -> None:
        time.sleep(0.035)
        stop_event.set()

    stopper = threading.Thread(target=stop_after_ticks)
    with caplog.at_level(logging.INFO):
        stopper.start()
        result = main(interval=0.01, stop_event=stop_event)
        stopper.join()

    assert result == 0
    messages = [record.getMessage() for record in caplog.records]
    assert "worker started" in messages
    assert messages.count("worker idle") >= 1
    assert "worker stopped" in messages
