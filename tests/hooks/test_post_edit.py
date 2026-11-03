"""PostToolUse ruff hook (X5). Calls ruff directly so uv does not re-sync the venv."""

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".claude" / "hooks"))

import post_edit


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(post_edit, "RUFF", [sys.executable, "-m", "ruff"])
    shutil.copy(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    return tmp_path


def edit(project, name, text):
    path = project / name
    path.write_text(text, encoding="utf-8")
    return post_edit.handle(
        {"tool_name": "Edit", "tool_input": {"file_path": str(path)}}, str(project)
    )


def test_formats_and_fixes(project):
    result = edit(project, "a.py", "import os\nimport sys\nx = [1,2,\n  3]\nprint(sys.argv)\n")
    assert result is None
    assert (project / "a.py").read_text(encoding="utf-8") == (
        "import sys\n\nx = [1, 2, 3]\nprint(sys.argv)\n"
    )


def test_unfixable_error_goes_back(project):
    result = edit(project, "b.py", "def f():\n    return undefined_name\n")
    assert result is not None and result["decision"] == "block"
    assert "b.py" in result["reason"] and "F821" in result["reason"]


def test_ignores_other_files(project):
    assert edit(project, "notes.md", "# x\n") is None
    event = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
    assert post_edit.handle(event, str(project)) is None
    outside = {"tool_name": "Edit", "tool_input": {"file_path": "C:\\elsewhere\\x.py"}}
    assert post_edit.handle(outside, str(project)) is None
