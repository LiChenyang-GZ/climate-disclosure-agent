"""Free, deterministic retrieval regression gate for Wattlebrook."""

import argparse
import asyncio
import math
from pathlib import Path
from typing import Any

import yaml
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from testcontainers.community.postgres import PostgresContainer

from cda.agent.prompts import prompt_sha256
from cda.db.models import Pack
from cda.db.session import make_engine
from cda.eval.answer_key import load_answer_key
from cda.eval.pack import ingest_pack
from cda.eval.report import render_report
from cda.eval.retrieval_eval import evaluate_retrieval
from cda.indexing.embed import HashEmbedder
from cda.tools.context import ToolContext

PACK = Path("data/synthetic/wattlebrook")
BASELINE = Path("evals/ci_baseline.yaml")
METRICS = {
    "hit_at_1": "Hit@1",
    "hit_at_5": "Hit@5",
    "hit_at_10": "Hit@10",
    "hit_at_20": "Hit@20",
    "mrr": "MRR",
}
MODES = {"keyword", "vector", "hybrid"}


def load_baseline(path: Path, key_version: int) -> dict[str, Any]:
    """Reject stale or invalid configuration before doing any database work."""
    try:
        base = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(base, dict):
            raise TypeError("expected a mapping")
        if base["pack"] != PACK.as_posix() or base["embedder"] != "hash":
            raise ValueError("pack or embedder mismatch")
        if type(base["key_version"]) is not int or base["key_version"] != key_version:
            raise ValueError("key_version mismatch")
        tolerance = base["tolerance"]
        if type(tolerance) not in (float, int) or not math.isfinite(tolerance) or tolerance < 0:
            raise ValueError("invalid tolerance")
        metrics = base["metrics"]
        if not isinstance(metrics, dict) or set(metrics) != MODES:
            raise ValueError("unknown or missing mode")
        for values in metrics.values():
            if not isinstance(values, dict) or set(values) != set(METRICS):
                raise ValueError("unknown or missing metric")
            if any(
                type(v) not in (float, int) or not math.isfinite(v) or not 0 <= v <= 1
                for v in values.values()
            ):
                raise ValueError("invalid metric value")
        paid = base["last_paid_eval"]
        if not isinstance(paid, dict) or set(paid) != {"prompt_sha256", "date", "results"}:
            raise ValueError("invalid last_paid_eval")
        if any(v is not None and not isinstance(v, str) for v in paid.values()):
            raise ValueError("invalid last_paid_eval value")
        return base
    except (OSError, yaml.YAMLError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid baseline: " + str(exc)) from exc


def actual_metrics(retrieval: dict[str, Any]) -> dict[str, dict[str, float]]:
    return {
        mode: {name: data["overall"][source] for name, source in METRICS.items()}
        for mode, data in retrieval["modes"].items()
    }


def compare(base: dict[str, Any], actual: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
    rows = []
    for mode, metrics in sorted(base["metrics"].items()):
        for metric, expected in metrics.items():
            value = actual[mode][metric]
            delta = value - expected
            # Decimal boundary differences must not turn an exactly allowed drop into a failure.
            outside = abs(delta) > base["tolerance"] and not math.isclose(
                abs(delta), base["tolerance"], abs_tol=1e-12
            )
            status = ("FAIL" if delta < 0 else "UPDATE") if outside else "PASS"
            rows.append(
                {
                    "mode": mode,
                    "metric": metric,
                    "baseline": expected,
                    "actual": value,
                    "delta": delta,
                    "status": status,
                }
            )
    return rows


def comparison_report(rows: list[dict[str, Any]]) -> str:
    lines = [
        "## Baseline comparison",
        "",
        "| Mode | Metric | Baseline | Actual | Delta | Status |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['mode']} | {row['metric']} | {row['baseline']:.3f} | {row['actual']:.3f} | {row['delta']:+.3f} | {row['status']} |"
        )
    if any(r["status"] == "UPDATE" for r in rows):
        lines += ["", "NOTICE: retrieval improved; run with --update-baseline."]
    return "\n".join(lines) + "\n"


def prompt_guard(base: dict[str, Any], current: str) -> str:
    if base["last_paid_eval"]["prompt_sha256"] != current:
        return "Prompt guard: WARNING: prompts changed since the last paid evaluation; run run_eval manually before relying on new prompt behaviour."
    return "Prompt guard: unchanged since the last paid evaluation."


def append_summary(
    path: Path, retrieval: dict[str, Any], rows: list[dict[str, Any]], guard: str
) -> None:
    report = render_report(
        {
            "retrieval_only": True,
            "labeler": "generator",
            "pack": PACK.as_posix(),
            "retrieval": retrieval,
        }
    )
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            "\n# Retrieval CI gate\n\n"
            + report
            + "\n"
            + comparison_report(rows)
            + "\n"
            + guard
            + "\n"
        )


async def evaluate(url: str) -> dict[str, Any]:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    await asyncio.to_thread(command.upgrade, config, "head")
    engine = make_engine(url)
    embedder = HashEmbedder()
    try:
        pid = await ingest_pack(engine, PACK, embedder=embedder)
        async with engine.begin() as conn:
            uid = (await conn.execute(select(Pack.created_by).where(Pack.id == pid))).scalar_one()
            return await evaluate_retrieval(
                ToolContext(conn, uid, pid, embedder), load_answer_key(PACK / "answer_key.yaml")
            )
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url")
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args(argv)
    try:
        base = load_baseline(BASELINE, load_answer_key(PACK / "answer_key.yaml").key_version)
    except ValueError as exc:
        print(exc)
        return 2
    container = None
    try:
        url = args.database_url
        if url is None:
            container = PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg")
            container.start()
            url = container.get_connection_url().replace("@localhost:", "@127.0.0.1:")
        retrieval = asyncio.run(evaluate(url))
        actual = actual_metrics(retrieval)
        rows = compare(base, actual)
        guard = prompt_guard(base, prompt_sha256())
        print(comparison_report(rows))
        print(guard)
        if args.summary:
            append_summary(args.summary, retrieval, rows, guard)
        if args.update_baseline:
            base["metrics"] = actual
            BASELINE.write_text(yaml.safe_dump(base, sort_keys=False), encoding="utf-8")
            return 0
        return int(any(row["status"] == "FAIL" for row in rows))
    except Exception:  # noqa: BLE001 -- CLI errors must not expose credentials in driver messages.
        # Driver/container exceptions can contain credentials from the supplied URL.
        print("Retrieval gate could not complete (database or output error).")
        return 2
    finally:
        if container is not None:
            container.stop()


if __name__ == "__main__":
    import sys

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    raise SystemExit(main())
