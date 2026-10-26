"""Packaged, versioned prompts."""

import hashlib
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from cda.checks.models import Check

_NAMES = ("worker_system.md", "worker_check.md", "method_b_system.md", "method_b_check.md")


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


def render_method_b(check: Check, context: list[dict[str, Any]]) -> tuple[str, str]:
    values = {
        k: json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v
        for k, v in check.model_dump().items()
    }
    blocks = []
    for number, unit in enumerate(context, 1):
        blocks.append(
            f"{number}. unit_id: {unit['unit_id']}; filename: {unit['filename']}; "
            f"part: {unit['part_kind']} / {unit.get('part_label') or ''}; "
            f"pdf page: {unit['pdf_page']}; section path: {unit['section_path']}\n" + unit["text"]
        )
    values["context"] = "\n\n".join(blocks)
    return _read("method_b_system.md").decode(), _read("method_b_check.md").decode().format(
        **values
    )
