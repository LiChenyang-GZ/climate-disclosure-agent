"""PreToolUse guard rules (X5)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / ".claude" / "hooks"
sys.path.insert(0, str(HOOKS))

import guard

REPO = "E:\\study\\climate-disclosure-agent"


def write(path, tool="Edit"):
    return guard.decide({"tool_name": tool, "tool_input": {"file_path": path}}, REPO)


def shell(command, tool="Bash"):
    return guard.decide({"tool_name": tool, "tool_input": {"command": command}}, REPO)


@pytest.mark.parametrize(
    ("path", "decision"),
    [
        (".env", "deny"),
        ("E:\\study\\climate-disclosure-agent\\.env", "deny"),
        ("/e/study/climate-disclosure-agent/.env.local", "deny"),
        ("e:/Study/Climate-Disclosure-Agent/web/.env", "deny"),
        ("data/real/qbe/answer_key.yaml", "deny"),
        ("data\\real\\cba\\parts.yaml", "deny"),
        ("uv.lock", "deny"),
        ("web/package-lock.json", "deny"),
        ("src/cda/checks/checks.yaml", "ask"),
        ("src/cda/agent/prompts/worker_system.md", "ask"),
        ("evals/answer_key.yaml", "ask"),
        ("src/cda/eval/run_eval.py", "ask"),
    ],
)
def test_guarded_paths(path, decision):
    result = write(path)
    assert result is not None and result[0] == decision
    assert result[1]


@pytest.mark.parametrize(
    "path",
    [
        ".env.example",
        "data/synthetic/wattlebrook/parts.yaml",
        "specs/tasks/X5.md",
        "src/cda/api/service.py",
        "src/cda/evaluation_notes.md",
        "E:\\study\\cda-ui\\.env",  # another checkout: not this project
        "E:\\OneDrive - The University of Sydney (Students)\\Obsidian\\notes.md",
        "E:\\study\\climate-disclosure-agent\\..\\other\\.env",
    ],
)
def test_allowed_paths(path):
    assert write(path) is None


@pytest.mark.parametrize("tool", ["Write", "MultiEdit"])
def test_other_write_tools(tool):
    assert write(".env", tool)[0] == "deny"


def test_notebook_path():
    event = {"tool_name": "NotebookEdit", "tool_input": {"notebook_path": "data/real/x.ipynb"}}
    assert guard.decide(event, REPO)[0] == "deny"


@pytest.mark.parametrize(
    ("command", "decision"),
    [
        ("cat .env", "deny"),
        ("cd /e/study/climate-disclosure-agent && grep OPENAI .env", "deny"),
        ("head -3 ../climate-disclosure-agent/.env.local", "deny"),
        ("source .env && echo ok", "deny"),
        (". ./.env", "deny"),
        ("Get-Content .env", "deny"),
        ('git commit -m "Fix\n\nCo-Authored-By: Claude <noreply@anthropic.com>"', "deny"),
        (
            "gh pr create --title X --body 'Generated with [Claude Code](https://claude.com)'",
            "deny",
        ),
        ("gh pr comment 4 --body '\U0001f916 done'", "deny"),
        ("git push --force origin task/X5", "ask"),
        ("git push -f", "ask"),
        ("git push --force-with-lease origin main", "ask"),
        ("git push origin +main", "ask"),
        ("codex exec --dangerously-bypass-approvals-and-sandbox 'x'", "deny"),
        ("codex exec -m gpt-6.1-sol 'Implement' < /dev/null", "ask"),
        ("uv run python -m cda.eval.run_eval --strategy A", "ask"),
    ],
)
def test_guarded_commands(command, decision):
    result = shell(command)
    assert result is not None and result[0] == decision


@pytest.mark.parametrize(
    "command",
    [
        "cat .env.example",
        "git push origin task/X5",
        "git push -u origin task/X5",
        'git commit -m "X5: Claude Code hooks"',
        "echo Co-Authored-By",  # not a commit or PR text
        "uv run python -m cda.eval.run_eval --strategy A --dry-run",
        "grep -n env .gitignore",
        "ls -la .envoy",
        "npm run lint",
    ],
)
def test_allowed_commands(command):
    assert shell(command) is None


def test_powershell_tool():
    assert shell("Get-Content .env", tool="PowerShell")[0] == "deny"


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"tool_name": "Edit"},
        {"tool_name": "Edit", "tool_input": "x"},
        {"tool_name": "Bash", "tool_input": {"command": 3}},
        {"tool_name": "Read", "tool_input": {"file_path": ".env"}},
    ],
)
def test_malformed_or_other_tools(event):
    assert guard.decide(event, REPO) is None


def run(stdin, root=REPO):
    return subprocess.run(
        [sys.executable, str(HOOKS / "guard.py")],
        input=stdin,
        check=False,
        capture_output=True,
        text=True,
        env={"CLAUDE_PROJECT_DIR": root, "SYSTEMROOT": "C:\\Windows", "PATH": ""},
        timeout=30,
    )


def test_script_contract_deny():
    done = run(json.dumps({"tool_name": "Write", "tool_input": {"file_path": ".env"}}))
    assert done.returncode == 0
    out = json.loads(done.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    assert ".env" in out["permissionDecisionReason"]


def test_script_contract_silent():
    done = run(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}))
    assert (done.returncode, done.stdout) == (0, "")
    done = run("not json")
    assert (done.returncode, done.stdout) == (0, "")


def test_settings_point_at_scripts():
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    commands = [
        hook["command"]
        for groups in settings["hooks"].values()
        for group in groups
        for hook in group["hooks"]
    ]
    assert len(commands) == 2
    for command in commands:
        script = command.split('"$CLAUDE_PROJECT_DIR/')[1].rstrip('"')
        assert (ROOT / script).is_file()
