"""Deterministic aggregate-only Markdown reports."""

from typing import Any

from cda.eval.metrics import summarize

COLUMNS = (
    "status_accuracy",
    "evidence_recall",
    "item_recall",
    "citation_accuracy",
    "needs_review_rate",
    "error_rate",
    "mean_steps",
    "cap_hit_rate",
    "cost",
    "wall_time",
)


def render_report(results: dict[str, Any]) -> str:
    real = results.get("labeler") != "generator"

    def label(name: str) -> str:
        return (
            name.replace("_", " ").replace("accuracy", "agreement with the reference answers")
            if real
            else name.replace("_", " ")
        )

    lines = ["# Evaluation" + (" — agreement with the reference answers" if real else "")]
    for name in (
        "pack",
        "key_version",
        "model",
        "prompt_hash",
        "check_set_version",
        "git_commit",
        "date",
        "repeats",
    ):
        lines.append(f"- {label(name)}: {results.get(name, '—')}")
    methods = results.get("methods", {})
    lines += [
        "",
        "| Method | " + " | ".join(label(c) for c in COLUMNS) + " |",
        "|---|" + "---|" * len(COLUMNS),
    ]
    for method, runs in sorted(methods.items()):
        lines.append(
            "| "
            + method
            + " | "
            + " | ".join(
                f"{summarize([r[c] for r in runs])['min']:.3f}–{summarize([r[c] for r in runs])['max']:.3f} (mean {summarize([r[c] for r in runs])['mean']:.3f})"
                for c in COLUMNS
            )
            + " |"
        )
    lines += ["", "## Injection gate" + (" — agreement with the reference answers" if real else "")]
    for method, runs in sorted(methods.items()):
        lines.append(
            f"- {method}: PASS {sum(r['injection_gate']['pass'] for r in runs)}/{len(runs)}"
        )
        for i, r in enumerate(runs, 1):
            if not r["injection_gate"]["pass"]:
                lines.append(f"  - Run {i}: FAIL ({', '.join(r['injection_gate']['reasons'])})")
    if not real:
        lines += ["", "## Inconsistency recall"]
        for method, runs in sorted(methods.items()):
            stats = summarize([r["inconsistency_recall"] for r in runs])
            lines.append(
                f"- {method}: {stats['min']:.3f}–{stats['max']:.3f} (mean {stats['mean']:.3f}); unmatched rows: "
                + str([r["unmatched_inconsistencies"] for r in runs])
            )
    for dimension in ("difficulty", "status", "index_table"):
        if dimension == "index_table" and not real:
            continue
        lines += [
            "",
            f"## {label(dimension)}" + (" — agreement with the reference answers" if real else ""),
        ]
        for method, runs in sorted(methods.items()):
            groups = sorted({g for r in runs for g in r["breakdowns"].get(dimension, {})})
            for group in groups:
                values = []
                for metric in ("status_accuracy", "evidence_recall"):
                    stats = summarize(
                        [
                            r["breakdowns"][dimension][group][metric]
                            for r in runs
                            if group in r["breakdowns"].get(dimension, {})
                        ]
                    )
                    values.append(
                        f"{label(metric)} {stats['min']:.3f}–{stats['max']:.3f} (mean {stats['mean']:.3f})"
                    )
                lines.append(f"- {method} / {group}: " + ", ".join(values))
    retrieval = results.get("retrieval", {})
    lines += [
        "",
        "## Retrieval" + (" — agreement with the reference answers" if real else ""),
        f"Embedder: {retrieval.get('embedder', '—')}",
    ]
    if retrieval.get("note"):
        lines.append(retrieval["note"])
    lines += [
        "",
        "| Mode / difficulty | Hit@1 | Hit@3 | Hit@5 | Hit@10 | Hit@20 | MRR |",
        "|---|---|---|---|---|---|---|",
    ]
    for mode, data in sorted(retrieval.get("modes", {}).items()):
        for group, values in [("overall", data["overall"]), *sorted(data["difficulty"].items())]:
            lines.append(
                f"| {mode} / {group} | "
                + " | ".join(
                    f"{values[c]:.3f}"
                    for c in ("Hit@1", "Hit@3", "Hit@5", "Hit@10", "Hit@20", "MRR")
                )
                + " |"
            )
    return "\n".join(lines) + "\n"
