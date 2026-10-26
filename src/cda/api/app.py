"""API application; checks are exclusively executed by the worker."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from cda import __version__
from cda.api.errors import install_errors
from cda.api.service import IndexState
from cda.config import Settings
from cda.indexing.embed import BgeEmbedder, Embedder


def create_app(
    *,
    embedder: Embedder | None = None,
    engine: AsyncEngine | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    configured = settings or Settings()
    embeddings = embedder if embedder is not None else BgeEmbedder()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if isinstance(embeddings, BgeEmbedder):
            await asyncio.to_thread(embeddings.warm_up)
        yield
        if engine is None and app.state.engine is not None:
            await app.state.engine.dispose()

    app = FastAPI(title=configured.app_name, lifespan=lifespan)
    app.state.settings = configured
    app.state.engine = engine
    app.state.embedder = embeddings
    app.state.index_state = IndexState()
    app.state.indexing = app.state.index_state.indexing
    app.state.index_errors = app.state.index_state.index_errors
    app.state.document_states = app.state.index_state.document_states
    install_errors(app)
    from cda.api.routes import findings, packs, runs

    app.include_router(packs.router)
    app.include_router(runs.router)
    app.include_router(findings.router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app


app = create_app()
