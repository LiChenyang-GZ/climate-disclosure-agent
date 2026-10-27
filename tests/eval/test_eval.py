from pathlib import Path

import pytest
import yaml

from cda.eval.answer_key import AnswerKey, load_answer_key
from cda.eval.match import evidence_matches, status_matches
from cda.eval.metrics import Citation, Finding, score_run, summarize

PACK = Path("data/synthetic/wattlebrook")


def test_key_loads():
    assert len(load_answer_key(PACK / "answer_key.yaml").cells) == 15


@pytest.mark.parametrize("case", ["relief", "check", "scan"])
def test_invalid_key(case):
    raw = yaml.safe_load((PACK / "answer_key.yaml").read_text())
    if case == "relief":
        raw["cells"][0]["relief_applied"] = "scope3_first_year"
    if case == "check":
        raw["cells"][0]["check_id"] = "X1"
    if case == "scan":
        next(c for c in raw["cells"] if c["status"] == "not_found")["scanned"] = []
    with pytest.raises(ValueError):
        AnswerKey.model_validate(raw)


def test_wrong_part(tmp_path):
    raw = yaml.safe_load((PACK / "answer_key.yaml").read_text())
    raw["cells"][0]["evidence"][0]["part"] = "other"
    (tmp_path / "answer_key.yaml").write_text(yaml.safe_dump(raw))
    # Parts loader validates the PDFs exist alongside the configuration.
    import shutil

    shutil.copy(PACK / "parts.yaml", tmp_path / "parts.yaml")
    for p in PACK.glob("*.pdf"):
        shutil.copy(p, tmp_path / p.name)
    with pytest.raises(ValueError):
        load_answer_key(tmp_path / "answer_key.yaml")


@pytest.mark.parametrize(
    "quote,page,doc,expected",
    [
        ("alpha beta extra", 1, "a", True),
        ("alpha other", 1, "a", True),
        ("other", 1, "a", False),
        ("alpha beta", 2, "a", False),
        ("alpha beta", 1, "b", False),
    ],
)
def test_match(quote, page, doc, expected):
    assert evidence_matches(Citation(doc, page, quote), Citation("a", 1, "alpha beta")) == expected


@pytest.mark.parametrize(
    "reason",
    [
        "possible_inconsistency",
        "regulatory_target_missing",
        "cross_reference_unclear",
        "t2_flag",
        "cap_hit",
    ],
)
def test_status(reason):
    assert status_matches("needs_review", "inconsistent", reason) == (reason != "cap_hit")


def test_metrics_and_repeats():
    key = load_answer_key(PACK / "answer_key.yaml")
    fs = [
        Finding(
            c.check_id,
            {
                "found": "evidence_found",
                "partial": "partial_evidence",
                "not_found": "no_evidence_found",
                "inconsistent": "inconsistent",
            }[c.status],
            evidence=[Citation(e.document, e.pdf_page, e.quote) for e in c.evidence],
        )
        for c in key.cells
    ]
    scores = score_run(key, fs)
    assert scores["status_accuracy"] == 1
    assert scores["evidence_recall"] == 1
    assert scores["citation_accuracy"] == 1
    assert scores["injection_gate"]["pass"]
    assert summarize([1, 2, 3]) == {"min": 1, "max": 3, "mean": 2}


def test_injection():
    key = load_answer_key(PACK / "answer_key.yaml")
    assert not score_run(key, [Finding("A1", "evidence_found")])["injection_gate"]["pass"]


@pytest.mark.parametrize(
    "system,key",
    [
        ("evidence_found", "found"),
        ("partial_evidence", "partial"),
        ("no_evidence_found", "not_found"),
        ("inconsistent", "inconsistent"),
    ],
)
def test_status_table(system, key):
    assert status_matches(system, key)


@pytest.mark.parametrize("system", ["error", "not_checked", "needs_review"])
def test_unmatched_status(system):
    assert not status_matches(system, "found")


def test_49_percent_overlap():
    key = " ".join(f"word{i}" for i in range(100))
    found = " ".join(f"word{i}" for i in range(49)) + " unrelated"
    assert not evidence_matches(Citation("a", 1, found), Citation("a", 1, key))


def test_alternate_item_and_not_applicable():
    key = load_answer_key(PACK / "answer_key.yaml")
    cell = key.cells[0]
    cell.evidence = [
        cell.evidence[0].model_copy(update={"quote": "alpha beta", "role": "primary"}),
        cell.evidence[0].model_copy(update={"quote": "gamma delta", "role": "alternate"}),
    ]
    for c in key.cells[1:]:
        c.status = "not_applicable"
        c.evidence = []
    e = cell.evidence[1]
    score = score_run(
        key,
        [
            Finding(
                cell.check_id,
                "evidence_found",
                evidence=[Citation(e.document, e.pdf_page, e.quote)],
            )
        ],
    )
    assert score["status_accuracy"] == 1
    assert score["not_applicable"] == 14
    assert score["evidence_recall"] == 1
    assert score["item_recall"] == 0
    assert score["breakdowns"]["difficulty"][str(cell.difficulty)]["evidence_recall"] == 1


def test_not_found_citation():
    key = load_answer_key(PACK / "answer_key.yaml")
    c = next(c for c in key.cells if c.status == "not_found")
    score = score_run(
        key,
        [
            Finding(
                c.check_id,
                "no_evidence_found",
                evidence=[Citation(key.documents[0].filename, 1, "anything")],
            )
        ],
    )
    assert score["citation_accuracy"] == 0


def test_unverified_evidence_not_recalled():
    key = load_answer_key(PACK / "answer_key.yaml")
    c = key.cells[0]
    e = c.evidence[0]
    score = score_run(
        key,
        [
            Finding(
                c.check_id,
                "evidence_found",
                evidence=[Citation(e.document, e.pdf_page, e.quote, False)],
            )
        ],
    )
    assert score["evidence_recall"] == 0


def test_process_events():
    key = load_answer_key(PACK / "answer_key.yaml")
    events = [
        {
            "type": "tool_called",
            "check_id": "G1",
            "payload": {
                "tool": "keyword_search",
                "query": q,
                "part_ids": parts,
                "repeated": repeat,
            },
        }
        for q, parts, repeat in [("one", ["a"], False), ("one", ["a"], True), ("two", ["b"], False)]
    ]
    result = score_run(
        key,
        [Finding("G1", "needs_review", "cap_hit", steps=3, tokens_in=10, tokens_out=5, cost=0.1)],
        events=events,
        part_ids={"a", "b"},
        wall_time=4,
    )
    assert result["repeat_query_rate"] == pytest.approx(1 / 3)
    assert result["distinct_queries"]["G1"] == 2
    assert result["part_coverage"] == pytest.approx(1 / 15)
    assert result["cap_hit_rate"] == pytest.approx(1 / 15)
    assert result["tokens_in"] == 10 and result["cost"] == 0.1 and result["wall_time"] == 4


@pytest.mark.parametrize("wrong_page", [False, True])
def test_inconsistency(wrong_page):
    key = load_answer_key(PACK / "answer_key.yaml")
    issues = [i for i in key.planted_issues if i.type in ("T1", "T3", "T4", "T5")]
    issue = issues[0]
    e = issue.locations[0]
    result = score_run(
        key,
        [],
        [
            {
                "type": issue.type,
                "detail": {
                    "side_a": {
                        "document": e.document,
                        "pdf_page": e.pdf_page + 100 if wrong_page else e.pdf_page,
                    }
                },
            },
            {"type": "T6", "detail": {}},
        ],
    )
    assert result["inconsistency_recall"] == (0 if wrong_page else 1 / len(issues))
    assert result["unmatched_inconsistencies"] == (2 if wrong_page else 1)


def test_injection_citation():
    key = load_answer_key(PACK / "answer_key.yaml")
    e = next(i for i in key.planted_issues if i.type == "injection").locations[0]
    assert not score_run(
        key,
        [Finding("G1", "partial_evidence", evidence=[Citation(e.document, e.pdf_page, e.quote)])],
    )["injection_gate"]["pass"]


def test_all_found_gate():
    key = load_answer_key(PACK / "answer_key.yaml")
    assert (
        "all findings evidence_found"
        in score_run(key, [Finding("G1", "evidence_found")])["injection_gate"]["reasons"]
    )


def test_spend_guard(monkeypatch, tmp_path):
    from cda.eval import run_eval

    def fail(*args, **kwargs):
        raise AssertionError("must not reach database or model")

    monkeypatch.setattr(run_eval, "make_engine", fail)
    monkeypatch.setattr(run_eval, "OpenAIClient", fail)
    with pytest.raises(SystemExit) as exc:
        run_eval.main(
            [
                "--pack",
                str(PACK),
                "--out",
                str(tmp_path),
                "--methods",
                "LC",
                "--repeats",
                "1",
                "--budget-usd",
                "1.01",
            ]
        )
    assert exc.value.code != 0


def test_report_golden():
    from cda.eval.report import render_report

    results = report_fixture()
    assert render_report(results) == Path("tests/eval/synthetic_report.md").read_text(
        encoding="utf-8"
    )


def report_fixture():
    key = load_answer_key(PACK / "answer_key.yaml")
    return {
        "pack": "Tiny",
        "labeler": "generator",
        "key_version": 1,
        "model": "fake",
        "prompt_hash": "abc",
        "check_set_version": "v1",
        "git_commit": "123",
        "date": "2026-01-01",
        "repeats": 1,
        "methods": {"LC": [score_run(key, [], method="LC")]},
    }


def test_real_report_no_quotes():
    from cda.eval.report import render_report

    results = report_fixture()
    results["labeler"] = "claude"
    results["quote"] = "PRIVATE QUOTE"
    text = render_report(results)
    assert "agreement with the reference answers" in text
    assert "accuracy" not in text.lower()
    assert "PRIVATE QUOTE" not in text


def test_lc_scope_argument():
    from cda.eval import run_eval

    p = run_eval.parser()
    base = ["--pack", str(PACK), "--out", "x"]
    assert p.parse_args(base).lc_scope == "pack"
    assert p.parse_args([*base, "--lc-scope", "sustainability"]).lc_scope == "sustainability"
    with pytest.raises(SystemExit):
        p.parse_args([*base, "--lc-scope", "climate"])


def test_report_lc_scope_line():
    from cda.eval.report import render_report

    results = report_fixture()
    assert "lc scope" not in render_report(results)
    results["lc_scope"] = "sustainability"
    assert "- lc scope: sustainability" in render_report(results)


def test_step_cap_argument(monkeypatch, tmp_path):
    from cda.eval import run_eval

    p = run_eval.parser()
    base = ["--pack", str(PACK), "--out", str(tmp_path)]
    assert p.parse_args(base).step_cap == 20
    assert p.parse_args([*base, "--step-cap", "12"]).step_cap == 12

    def fail(*args, **kwargs):
        raise AssertionError("must not reach database or model")

    monkeypatch.setattr(run_eval, "make_engine", fail)
    with pytest.raises(SystemExit) as exc:
        run_eval.main([*base, "--step-cap", "1"])
    assert exc.value.code != 0


def test_report_step_cap_line():
    from cda.eval.report import render_report

    results = report_fixture()
    results["step_cap"] = 20
    assert "step cap" not in render_report(results)  # fixture has only LC
    results["methods"]["A"] = results["methods"]["LC"]
    assert "- step cap: 20" in render_report(results)


def test_token_cap_argument(monkeypatch, tmp_path):
    from cda.eval import run_eval

    p = run_eval.parser()
    base = ["--pack", str(PACK), "--out", str(tmp_path)]
    assert p.parse_args(base).token_cap == 300_000
    assert p.parse_args([*base, "--token-cap", "150000"]).token_cap == 150_000

    def fail(*args, **kwargs):
        raise AssertionError("must not reach database or model")

    monkeypatch.setattr(run_eval, "make_engine", fail)
    with pytest.raises(SystemExit) as exc:
        run_eval.main([*base, "--token-cap", "0"])
    assert exc.value.code != 0


def test_report_token_cap_line():
    from cda.eval.report import render_report

    results = report_fixture()
    results["token_cap"] = 300_000
    assert "token cap" not in render_report(results)  # fixture has only LC
    results["methods"]["C"] = results["methods"]["LC"]
    assert "- token cap: 300000" in render_report(results)


def test_abstain_answered_and_baseline():
    key = load_answer_key(PACK / "answer_key.yaml")
    found = [c for c in key.cells if c.status == "found"]
    findings = [
        Finding("G1", "evidence_found"),  # answered, agrees
        Finding("M1", "needs_review", "possible_inconsistency"),  # a flag is an answer
        Finding("S3", "evidence_found"),  # answered, disagrees (key: partial)
        Finding("S1", "needs_review", "cap_hit"),  # abstains
        Finding("S2", "error", "invalid_output"),  # abstains
    ]  # every other cell has no finding: abstains
    r = score_run(key, findings)
    n = len(key.cells)
    assert r["status_accuracy"] == pytest.approx(2 / n)
    assert r["abstain_rate"] == pytest.approx((n - 3) / n)
    assert r["answered_status_accuracy"] == pytest.approx(2 / 3)
    assert r["always_found_baseline"] == pytest.approx(len(found) / n)


def test_answered_accuracy_when_nothing_answered():
    key = load_answer_key(PACK / "answer_key.yaml")
    r = score_run(key, [])
    assert (r["abstain_rate"], r["answered_status_accuracy"]) == (1.0, 0.0)


def test_report_baseline_line():
    from cda.eval.report import render_report

    assert "always found baseline (status): " in render_report(report_fixture())
