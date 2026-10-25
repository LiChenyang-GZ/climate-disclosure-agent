"""Packaged, versioned prompts."""

import hashlib
import json
from importlib.resources import files
from pathlib import Path

from cda.checks.models import Check

_NAMES = ("worker_system.md", "worker_check.md")


def _read(name: str, directory: Path | None = None) -> bytes:
    if directory is not None:
        return (directory / name).read_bytes()
    return files("cda.agent").joinpath("prompts", name).read_bytes()


def prompt_sha256(directory: Path | None = None) -> str:
    return hashlib.sha256(b"".join(_read(n, directory) for n in _NAMES)).hexdigest()


def render(check: Check, min_searches: int) -> tuple[str, str]:
    values = {
        k: json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v
        for k, v in check.model_dump().items()
    }
    values["min_searches"] = min_searches
    return _read(_NAMES[0]).decode(), _read(_NAMES[1]).decode().format(**values)
