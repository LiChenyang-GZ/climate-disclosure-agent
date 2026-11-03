"""PostToolUse hook: format and lint an edited Python file with ruff (X5).

Runs `ruff check --fix` and `ruff format` through uv on the one file Claude
just wrote. If errors remain, ruff's output goes back to Claude to fix.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from guard import relative

TOOLS = {"Edit", "Write", "MultiEdit"}
RUFF = ["uv", "run", "--quiet", "ruff"]


def run(root: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*RUFF, *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


def handle(event: dict[str, object], root: str) -> dict[str, object] | None:
    if event.get("tool_name") not in TOOLS:
        return None
    data = event.get("tool_input")
    path = data.get("file_path") if isinstance(data, dict) else None
    if not isinstance(path, str) or not path.endswith(".py"):
        return None
    rel = relative(path, root)
    if rel is None or not os.path.isfile(os.path.join(root, rel)):
        return None
    # Fix first (it can leave blank lines behind), then format, then report what is left.
    run(root, "check", "--fix", "--quiet", rel)
    run(root, "format", rel)
    check = run(root, "check", rel)
    if check.returncode == 0:
        return None
    output = (check.stdout + check.stderr).strip()
    return {
        "decision": "block",
        "reason": f"ruff still reports problems in {rel} after format and --fix:\n{output}",
    }


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (ValueError, UnicodeDecodeError):
        return 0
    if not isinstance(event, dict):
        return 0
    root = os.environ.get("CLAUDE_PROJECT_DIR") or str(event.get("cwd") or os.getcwd())
    try:
        result = handle(event, root)
    except (OSError, subprocess.SubprocessError):
        return 0  # uv or ruff unavailable: never break the edit itself
    if result is not None:
        json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
