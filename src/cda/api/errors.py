"""Safe HTTP error envelopes."""

import re
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

_RUN_MESSAGES = {
    "llm_auth": "The OpenAI API key is missing or was rejected.",
    "prompt_changed": "The prompt changed since this run was created. Start a new run.",
    "check_set_changed": "The check set changed since this run was created. Start a new run.",
    "method_not_supported": "The selected method is not supported by the worker.",
    "budget_exhausted": "The run reached its budget limit.",
    "llm_unavailable": "The model service is temporarily unavailable.",
    "llm_rejected": "The model service rejected the request.",
    "internal_error": "An internal error prevented the run from completing.",
}


def run_errors(row: Mapping[str, Any]) -> dict[str, Any]:
    """Expose short worker codes while hiding arbitrary exception details."""
    code = row.get("error")
    if code is not None and (
        not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code)
    ):
        code = "internal_error"
    return {
        **row,
        "error": code,
        "error_message": _RUN_MESSAGES.get(code, "The run could not be completed.")
        if code
        else None,
    }


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        self.status, self.code, self.message, self.extra = status, code, message, extra


def install_errors(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def known(request: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"error": exc.code, "message": exc.message, **exc.extra}
        )

    @app.exception_handler(RequestValidationError)
    async def validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422, content={"error": "invalid_request", "message": "Invalid request"}
        )

    @app.exception_handler(HTTPException)
    async def http(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": "http_error", "message": "Request rejected"},
        )

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500, content={"error": "internal_error", "message": "Request failed"}
        )
