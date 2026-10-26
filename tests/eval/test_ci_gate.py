from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from cda.eval import ci_gate
from cda.eval.report import render_report


def baseline():
    return {
        "pack": "data/synthetic/wattlebrook",
        "key_version": 1,
        "embedder": "hash",
        "tolerance": 0.02,
        "metrics": {
            mode: dict.fromkeys(ci_gate.METRICS, 0.5) for mode in ("keyword", "vector", "hybrid")
        },
        "last_paid_eval": {"prompt_sha256": None, "date": None, "results": None},
    }


@pytest.mark.parametrize(
    "delta,status", [(0, "PASS"), (-0.03, "FAIL"), (-0.01, "PASS"), (0.05, "UPDATE")]
)
def test_compare(delta, status):
    base = baseline()
    actual = deepcopy(base["metrics"])
    actual["hybrid"]["mrr"] += delta
    rows = ci_gate.compare(base, actual)
    row = next(r for r in rows if r["mode"] == "hybrid" and r["metric"] == "mrr")
    assert row["status"] == status
    assert sum(r["status"] == "FAIL" for r in rows) == (status == "FAIL")
    if status == "UPDATE":
        assert "--update-baseline" in ci_gate.comparison_report(rows)


@pytest.mark.parametrize("case", ["missing", "version", "mode", "metric", "malformed"])
def test_invalid_baseline(case, tmp_path, monkeypatch, capsys):
    path = tmp_path / "baseline.yaml"
    base = baseline()
    if case == "version":
        base["key_version"] = 2
    if case == "mode":
        base["metrics"]["unknown"] = base["metrics"]["hybrid"]
    if case == "metric":
        base["metrics"]["hybrid"]["unknown"] = 0.5
    if case != "missing":
        path.write_text("[invalid" if case == "malformed" else yaml.safe_dump(base))
    monkeypatch.setattr(ci_gate, "BASELINE", path)
    assert ci_gate.main([]) == 2
    assert "baseline" in capsys.readouterr().out.lower()


@pytest.mark.parametrize("saved,warning", [(None, True), ("different", True), ("current", False)])
def test_prompt_guard(saved, warning):
    base = baseline()
    base["last_paid_eval"]["prompt_sha256"] = saved
    line = ci_gate.prompt_guard(base, "current")
    assert ("WARNING" in line) == warning
    if warning:
        assert "run_eval" in line and "prompts changed" in line


@pytest.mark.parametrize("delta,expected", [(0, 0), (-0.03, 1), (-0.01, 0), (0.05, 0)])
@pytest.mark.parametrize("saved", [None, "different", "current"])
def test_gate_exit(delta, expected, saved, tmp_path, monkeypatch, capsys):
    base = baseline()
    base["last_paid_eval"]["prompt_sha256"] = saved
    path = tmp_path / "baseline.yaml"
    path.write_text(yaml.safe_dump(base), encoding="utf-8")
    monkeypatch.setattr(ci_gate, "BASELINE", path)
    monkeypatch.setattr(ci_gate, "prompt_sha256", lambda: "current")

    async def evaluate(url):
        result = retrieval()
        for data in result["modes"].values():
            data["overall"] = dict.fromkeys(data["overall"], 0.5)
        result["modes"]["hybrid"]["overall"]["MRR"] += delta
        return result

    monkeypatch.setattr(ci_gate, "evaluate", evaluate)
    assert ci_gate.main(["--database-url", "unused"]) == expected
    text = capsys.readouterr().out
    assert ("WARNING" in text) == (saved != "current")
    if expected:
        assert "| hybrid | mrr | 0.500 | 0.470 | -0.030 | FAIL |" in text
    if delta > 0.02:
        assert "--update-baseline" in text


@pytest.mark.parametrize("failure", [None, "start", "evaluation"])
def test_container_cleanup(failure, tmp_path, monkeypatch):
    path = tmp_path / "baseline.yaml"
    path.write_text(yaml.safe_dump(baseline()), encoding="utf-8")
    monkeypatch.setattr(ci_gate, "BASELINE", path)
    events = []

    class Container:
        def __init__(self, image, driver):
            assert image == "pgvector/pgvector:pg16" and driver == "asyncpg"

        def start(self):
            events.append("start")
            if failure == "start":
                raise RuntimeError("start failed")

        def get_connection_url(self):
            return "postgresql+asyncpg://localhost@localhost:1234/test"

        def stop(self):
            events.append("stop")

    async def evaluate(url):
        assert "@127.0.0.1:" in url
        if failure == "evaluation":
            raise RuntimeError("evaluation failed")
        return retrieval()

    monkeypatch.setattr(ci_gate, "PostgresContainer", Container)
    monkeypatch.setattr(ci_gate, "evaluate", evaluate)
    assert ci_gate.main([]) == (2 if failure else 0)
    assert events == ["start", "stop"]


def test_update(tmp_path, monkeypatch):
    path = tmp_path / "baseline.yaml"
    base = baseline()
    base["last_paid_eval"]["results"] = "manual note"
    path.write_text(yaml.safe_dump(base))
    monkeypatch.setattr(ci_gate, "BASELINE", path)

    async def evaluate(url):
        return retrieval()

    monkeypatch.setattr(ci_gate, "evaluate", evaluate)
    assert ci_gate.main(["--database-url", "unused", "--update-baseline"]) == 0
    updated = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert updated["metrics"]["hybrid"]["mrr"] == 0.75
    assert updated["tolerance"] == base["tolerance"]
    assert updated["last_paid_eval"] == base["last_paid_eval"]


def retrieval():
    return {
        "embedder": "HashEmbedder",
        "modes": {
            mode: {
                "overall": dict.fromkeys(
                    ("Hit@1", "Hit@3", "Hit@5", "Hit@10", "Hit@20", "MRR"), 0.75
                ),
                "difficulty": {},
            }
            for mode in ("keyword", "vector", "hybrid")
        },
    }


def test_summary(tmp_path):
    path = tmp_path / "summary.md"
    base = baseline()
    rows = ci_gate.compare(base, ci_gate.actual_metrics(retrieval()))
    for _ in range(2):
        ci_gate.append_summary(path, retrieval(), rows, "Prompt guard: WARNING")
    text = path.read_text(encoding="utf-8")
    assert text.count("# Retrieval CI gate") == 2
    assert text.count("| hybrid / overall |") == 2
    assert "| hybrid | mrr | 0.500 | 0.750 | +0.250 | UPDATE |" in text
    assert text.count("Prompt guard: WARNING") == 2


def test_retrieval_report():
    result = {
        "retrieval_only": True,
        "labeler": "generator",
        "pack": "Tiny",
        "key_version": 1,
        "retrieval": retrieval(),
    }
    text = render_report(result)
    assert "Retrieval only (no model calls)" in text
    for forbidden in (
        "| Method |",
        "Injection",
        "Inconsistency",
        "## difficulty",
        "- model:",
        "- repeats:",
        "- prompt hash:",
        "- check set version:",
    ):
        assert forbidden not in text
    assert (
        "Vector scores use HashEmbedder, a hashed bag-of-words (lexical) baseline, not semantic embeddings; run with `--embedder bge` for semantic retrieval."
        in text
    )
    assert text == Path("tests/eval/retrieval_report.md").read_text(encoding="utf-8")


@pytest.mark.db
def test_database_gate(pg_url, monkeypatch, capsys):
    from cda.llm.openai_client import OpenAIClient

    def fail(*args, **kwargs):
        raise AssertionError("no model client allowed")

    monkeypatch.setattr(OpenAIClient, "__init__", fail)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert ci_gate.main(["--database-url", pg_url]) == 0
    first = capsys.readouterr().out
    assert ci_gate.main(["--database-url", pg_url]) == 0
    assert capsys.readouterr().out == first
