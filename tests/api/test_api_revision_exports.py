from datetime import date
from pathlib import Path

import pytest

from cda.api import export


@pytest.mark.parametrize("final", [False, True])
async def test_markdown_report_golden(monkeypatch, final):
    async def pack(*args):
        return {"company": "Example *Energy*", "reporting_year": 2026}

    async def rows(*args):
        return [
            {
                "id": "finding",
                "check_id": "G1",
                "title": "Board oversight",
                "status": "evidence_found",
                "relief_applied": True,
            },
            {
                "id": None,
                "check_id": "G2",
                "title": "Management role",
                "status": "not_checked",
                "relief_applied": False,
            },
        ]

    async def detail(*args):
        return {
            "evidence": [
                {
                    "filename": "annual.pdf",
                    "pdf_page": 19,
                    "quote": "* | [x](y) <script>\nnext line",
                },
                {"filename": "supplement.pdf", "pdf_page": 2, "quote": "More evidence"},
            ],
            "reviews": [{"decision": "accept", "note": "Checked | carefully\nthanks"}],
        }

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 26)

    monkeypatch.setattr(export.deps, "visible_pack", pack)
    monkeypatch.setattr(export, "finding_rows", rows)
    monkeypatch.setattr(export, "finding_detail", detail)

    async def unreadable(*args):
        return [
            {"document": "annual.pdf", "pages_without_text": [1, 12, 13, 14]},
            {"document": "supplement.pdf", "pages_without_text": []},
        ]

    monkeypatch.setattr(export, "date", FixedDate, raising=False)
    monkeypatch.setattr(export, "unreadable_pages", unreadable)
    response = await export.export_response(
        None,
        {},
        {
            "id": "run",
            "pack_id": "pack",
            "method": "A",
            "model_key": "luna",
            "prompt_sha256": "abcdef012345" * 5 + "abcd",
            "check_set_version": "2026.10.1",
            "status": "final" if final else "queued",
            "settings": {"step_cap": 20, "token_cap": 300_000, "lc_scope": "pack"},
        },
        "md",
    )
    expected = Path(__file__).with_name("report.md").read_text(encoding="utf-8")
    if final:
        expected = expected.replace("**Status:** Draft", "**Status:** Final")
    assert response.body.decode() == expected


@pytest.mark.parametrize(
    "stored,expected",
    [
        ("llm_auth", "llm_auth"),
        ("IntegrityError", "internal_error"),
        ("future_code", "future_code"),
        ("x" * 65, "internal_error"),
        ("bad/code", "internal_error"),
        ("llm_auth\n", "internal_error"),
        (None, None),
    ],
)
def test_safe_run_error_codes(stored, expected):
    from cda.api import errors

    result = errors.run_errors({"error": stored})
    assert result["error"] == expected
    if expected is None:
        assert result["error_message"] is None
    elif expected == "llm_auth":
        assert result["error_message"] == "The model provider's API key is missing or was rejected."
    else:
        assert result["error_message"]


@pytest.mark.parametrize(
    "row,expected",
    [
        ({"status": "evidence_found"}, "Evidence found"),
        ({"status": "needs_review", "proposed_status": None}, "Needs review"),
        (
            {"status": "needs_review", "proposed_status": "partial_evidence"},
            "Needs review (model: Partial evidence)",
        ),
        (
            {"status": "needs_review", "proposed_status": "no_evidence_found"},
            "Needs review (model: No evidence found)",
        ),
    ],
)
def test_status_cell(row, expected):
    # D88: the model's own status is shown only when code overrode it.
    assert export.status_cell(row) == expected


@pytest.mark.parametrize(
    "method,settings,expected",
    [
        ("A", None, "**Settings:** not recorded"),
        ("A", {}, "**Settings:** not recorded"),
        (
            "LC",
            {"step_cap": 20, "token_cap": 300_000, "lc_scope": "sustainability"},
            "**Settings:** step cap 20 · token cap 300,000 · LC scope: sustainability part only",
        ),
        (
            "LC",
            {"step_cap": 12, "token_cap": 150_000, "lc_scope": "pack"},
            "**Settings:** step cap 12 · token cap 150,000 · LC scope: whole pack",
        ),
        (
            "B",
            {"step_cap": 20, "token_cap": 300_000, "lc_scope": "sustainability"},
            "**Settings:** step cap 20 · token cap 300,000",
        ),
    ],
)
def test_settings_line(method, settings, expected):
    # D87: older runs did not record settings; LC scope only matters for LC runs.
    assert export.settings_line({"method": method, "settings": settings}) == expected
