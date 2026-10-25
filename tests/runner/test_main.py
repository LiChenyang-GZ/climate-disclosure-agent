import logging

from langgraph.checkpoint.memory import InMemorySaver

from cda.config import Settings
from cda.llm.openai_client import OpenAIClient
from cda.runner import main as worker
from cda.runner.deps import build_run_deps


def test_build_run_deps_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    deps = build_run_deps(
        Settings(database_url="postgresql+asyncpg://localhost/cda_test"), InMemorySaver()
    )
    assert isinstance(deps.llm, OpenAIClient)


def test_main_signal_stop(monkeypatch, caplog):
    def signal_stub(signum, handler):
        if callable(handler):
            handler(signum, None)

    monkeypatch.setattr(worker.signal, "getsignal", lambda _: worker.signal.SIG_IGN)
    monkeypatch.setattr(worker.signal, "signal", signal_stub)
    with caplog.at_level(logging.INFO):
        assert worker.main() == 0
    assert "worker started" in caplog.text
    assert "worker stopped" in caplog.text
    assert "postgresql://" not in caplog.text
