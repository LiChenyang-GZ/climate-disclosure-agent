"""The Claude skill must describe the MCP server as it is (X4)."""

import re
from pathlib import Path

import yaml
from mcp.shared.memory import create_connected_server_and_client_session

from cda.mcp_server.server import MAX_BUDGET_USD, build_server

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / ".claude" / "skills" / "climate-disclosure-check" / "SKILL.md"
# Tool names from PRD 6.8's first draft that were never built (note 13, item 56).
NEVER_BUILT = [
    "list_documents",
    "search_documents",
    "search_across_packs",
    "read_unit",
    "get_run_status",
    "get_finding_detail",
]


def parts() -> tuple[dict[str, str], str]:
    text = SKILL.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    assert match, "SKILL.md must start with YAML frontmatter"
    return yaml.safe_load(match.group(1)), match.group(2)


def test_frontmatter():
    meta, body = parts()
    assert meta["name"] == SKILL.parent.name
    assert re.fullmatch(r"[a-z0-9-]{1,64}", meta["name"])
    assert 50 < len(meta["description"]) <= 1024
    assert body.strip()
    assert b"\r\n" not in SKILL.read_bytes()


async def test_mentions_exactly_the_server_tools(monkeypatch):
    monkeypatch.setenv("CDA_MCP_USER", "sarah")
    async with create_connected_server_and_client_session(build_server()) as session:
        tools = {t.name for t in (await session.list_tools()).tools}
    meta, body = parts()
    for name in tools:
        assert f"`{name}`" in body, name
        assert name in meta["description"], name
    for name in NEVER_BUILT:
        assert name not in body, name


def test_budget_and_errors_match_the_code():
    _, body = parts()
    assert MAX_BUDGET_USD == 1
    assert "at most 1 USD" in body
    source = "".join(
        (ROOT / "src" / "cda" / path).read_text(encoding="utf-8")
        for path in ("mcp_server/server.py", "api/service.py")
    )
    table = body.split("| Error |", 1)[1].split("\n\n", 1)[0]
    codes = re.findall(r"^\| `([a-z_]+)` \|", table, re.MULTILINE)
    assert len(codes) == 7
    for code in codes:
        assert f'"{code}"' in source, code


def test_statuses_match_the_schema():
    from cda.db.enums import FINDING_STATUSES, RUN_STATUSES

    _, body = parts()
    for status in (*RUN_STATUSES, *FINDING_STATUSES):
        assert f"`{status}`" in body, status
