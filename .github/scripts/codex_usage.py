"""Find Codex's token usage in its session logs and print a Markdown line with the cost.

Usage: python codex_usage.py <model_key>   (model_key from config/models.yaml)
Never fails the job: prints "usage unavailable" if nothing is found.
"""

import json
import os
import sys
from pathlib import Path

PRICES = {  # keep in sync with config/models.yaml
    "luna": (0.10, 0.10, 0.50),
    "sol": (2.00, 0.10, 10.00),
}


def find_usage() -> dict | None:
    roots = [Path.home() / ".codex", Path(os.environ.get("CODEX_HOME", "/nonexistent"))]
    if os.environ.get("RUNNER_TEMP"):
        roots.append(Path(os.environ["RUNNER_TEMP"]))
    files = []
    for root in roots:
        if root.is_dir():
            files += list(root.rglob("*.jsonl"))
    files.sort(key=lambda p: p.stat().st_mtime)
    best = None
    for f in files:
        for line in f.read_text(errors="ignore").splitlines():
            if "total_token_usage" not in line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            stack = [obj]
            while stack:
                cur = stack.pop()
                if isinstance(cur, dict):
                    if "total_token_usage" in cur and isinstance(cur["total_token_usage"], dict):
                        best = cur["total_token_usage"]
                    stack.extend(cur.values())
                elif isinstance(cur, list):
                    stack.extend(cur)
    return best


def main() -> None:
    key = sys.argv[1] if len(sys.argv) > 1 else "luna"
    usage = find_usage()
    if not usage:
        print(f"Token usage unavailable (model `{key}`). Check the OpenAI usage page.")
        return
    inp = int(usage.get("input_tokens", 0))
    cached = int(usage.get("cached_input_tokens", 0))
    out = int(usage.get("output_tokens", 0))
    p_in, p_cached, p_out = PRICES.get(key, PRICES["sol"])
    cost = ((inp - cached) * p_in + cached * p_cached + out * p_out) / 1_000_000
    print(
        f"Model `{key}` · input {inp:,} (cached {cached:,}) · output {out:,} · "
        f"estimated cost **${cost:.3f}**"
    )


if __name__ == "__main__":
    main()
