"""Production dependencies for the durable run graph."""

from importlib import import_module
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from cda.agent.hooks import Comparer, noop_comparer
from cda.agent.run_graph import RunDeps
from cda.config import Settings
from cda.db.session import make_engine
from cda.indexing.embed import BgeEmbedder
from cda.llm.cost import ModelTable
from cda.llm.openai_client import OpenAIClient
from cda.runner.events import EventSink


def build_run_deps(settings: Settings, checkpointer: BaseCheckpointSaver[Any]) -> RunDeps:
    comparer: Comparer = noop_comparer
    try:
        module = import_module("cda.compare")
    except ModuleNotFoundError as exc:
        if exc.name != "cda.compare":
            raise
    else:
        comparer = getattr(module, "compare_run", noop_comparer)
    models = ModelTable.load()
    return RunDeps(
        llm=OpenAIClient(models),
        models=models,
        embedder=BgeEmbedder(),
        checkpointer=checkpointer,
        comparer=comparer,
        emit=EventSink(make_engine(settings.database_url)).emit,
    )
