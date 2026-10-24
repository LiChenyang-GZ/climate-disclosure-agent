from fastapi import FastAPI

from cda import __version__
from cda.config import Settings


def create_app() -> FastAPI:
    app = FastAPI(title=Settings().app_name)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app


app = create_app()
