"""PreToolUse guard for Claude Code in this repo (X5, specs/tasks/X5.md).

Turns written rules (CLAUDE.md, handoff 2.6.1) into deny / ask decisions.
These are guardrails against slips, matched on text, not a security boundary.
Standard library only, so it starts fast under any Python >= 3.10.
"""

from __future__ import annotations

import fnmatch
import json
import os
import posixpath
import re
import sys
from pathlib import PurePosixPath

WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}

DENY_PATHS = [
    ("data/real/*", "Real-company files are never edited or committed (D72)."),
    ("uv.lock", "Change Python dependencies with `uv add` / `uv remove`, not by editing uv.lock."),
    (
        "web/package-lock.json",
        "Change web dependencies with `npm install`, not by editing package-lock.json.",
    ),
]
ASK_PATHS = [
    "src/cda/checks/checks.yaml",
    "src/cda/agent/prompts/*",
    "*answer_key*.yaml",
    "src/cda/eval/*",
]
FROZEN = (
    "Frozen until the note-12 / note-13 decisions (handoff 2.6.1): checks.yaml, the system "
    "prompts, answer keys and eval logic. Ask Christy first."
)

READER = re.compile(
    r"(?<![\w-])(?:cat|type|head|tail|less|more|grep|rg|sed|awk|source|get-content|gc|"
    r"select-string)(?![\w-])|(?:^|\s)\.\s",
    re.IGNORECASE,
)
ENV_FILE = re.compile(r"(?<![\w.-])\.env(?!\.example\b)(?:\.[\w-]+)?(?![\w.-])")
ATTRIBUTION = re.compile(r"co-authored-by|generated with \[?claude|\U0001f916", re.IGNORECASE)
TEXT_COMMAND = re.compile(r"\bgit\s+commit\b|\bgh\s+(?:pr|issue)\s+(?:create|edit|comment)\b")
FORCE_PUSH = re.compile(r"\bgit\s+push\b[^;&|]*?(?:\s--force(?:-with-lease)?\b|\s-f\b|\s\+\S)")
CODEX_BYPASS = re.compile(r"\bcodex\s+exec\b[^;&|]*--dangerously-bypass-approvals-and-sandbox")
CODEX = re.compile(r"\bcodex\s+exec\b")
RUN_EVAL = re.compile(r"\brun_eval\b")


def _norm(value: str) -> str:
    value = value.replace("\\", "/")
    # Git Bash spells E:\study as /e/study.
    return re.sub(r"^/([A-Za-z])/", lambda m: m[1] + ":/", value)


def relative(path: str, root: str) -> str | None:
    """Path relative to the project root with forward slashes; None if outside it."""
    if not path or not root:
        return None
    p, r = _norm(path), _norm(root)
    if not (p.startswith("/") or re.match(r"^[A-Za-z]:/", p)):
        p = r.rstrip("/") + "/" + p
    p, r = posixpath.normpath(p), posixpath.normpath(r)
    fold = re.match(r"^[A-Za-z]:", r) is not None  # Windows paths are case-insensitive
    if not (p.lower() if fold else p).startswith((r.lower() if fold else r) + "/"):
        return None
    return p[len(r) + 1 :]


def check_path(rel: str) -> tuple[str, str] | None:
    name = PurePosixPath(rel).name
    if (name == ".env" or name.startswith(".env.")) and name != ".env.example":
        return "deny", "Never edit .env files from a tool call; secrets stay out of the session."
    for pattern, reason in DENY_PATHS:
        if fnmatch.fnmatch(rel, pattern):
            return "deny", reason
    for pattern in ASK_PATHS:
        if fnmatch.fnmatch(rel, pattern):
            return "ask", FROZEN
    return None


def check_command(command: str) -> tuple[str, str] | None:
    for segment in re.split(r"[;&|\n]", command):
        if ENV_FILE.search(segment) and READER.search(segment):
            return "deny", "Do not open or print .env files (CLAUDE.md)."
    if TEXT_COMMAND.search(command) and ATTRIBUTION.search(command):
        return "deny", (
            "No AI attribution in commits, PRs or comments (CLAUDE.md): remove "
            "Co-Authored-By / 'Generated with Claude' lines."
        )
    if CODEX_BYPASS.search(command):
        return (
            "deny",
            "Never bypass the Codex sandbox (CLAUDE.md step 3); report the error instead.",
        )
    if FORCE_PUSH.search(command):
        return "ask", "Force-push only with Christy's explicit approval (CLAUDE.md)."
    if CODEX.search(command):
        return "ask", "Codex uses the OpenAI key and may spend credits; confirm first."
    if RUN_EVAL.search(command) and "--dry-run" not in command:
        return "ask", "run_eval can spend OpenAI credits; confirm first or use --dry-run."
    return None


def decide(event: dict[str, object], root: str) -> tuple[str, str] | None:
    tool = event.get("tool_name")
    data = event.get("tool_input")
    if not isinstance(tool, str) or not isinstance(data, dict):
        return None
    if tool in WRITE_TOOLS:
        path = data.get("file_path") or data.get("notebook_path")
        rel = relative(path, root) if isinstance(path, str) else None
        return check_path(rel) if rel is not None else None
    if tool in SHELL_TOOLS:
        command = data.get("command")
        return check_command(command) if isinstance(command, str) else None
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (ValueError, UnicodeDecodeError):
        return 0
    if not isinstance(event, dict):
        return 0
    root = os.environ.get("CLAUDE_PROJECT_DIR") or str(event.get("cwd") or os.getcwd())
    result = decide(event, root)
    if result is None:
        return 0
    decision, reason = result
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
