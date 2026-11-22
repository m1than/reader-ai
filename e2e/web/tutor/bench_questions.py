"""SRV-TUTOR bench on the running stack (NFR-TUTOR-1..3, VEC-TUTOR-7).

    uv run --project server python e2e/web/tutor/bench_questions.py [--lesson-ordinal 2]

Runs `reader.tutor.bench` in the worker container (the generator `questions` and the driver live there and are
re-exported here), passes the fixed question questions-v2 `v03`, and writes the numbers-only report to
`e2e/web/tutor/results/<UTC time>.json`. Spends up to n x question_cap_usd from budget.test_budget_usd; one run
per UTC day fits the normal daily cap. Prints the summary and exits 1 when an NFR misses.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from ruamel.yaml import YAML

from reader.tutor.bench import CELL_KIND, TAKE, TEMPLATE, BenchError, p95, questions, title_of

__all__ = ["CELL_KIND", "TAKE", "TEMPLATE", "BenchError", "p95", "questions", "title_of"]

REPO = Path(__file__).resolve().parents[3]
FIXED = REPO / "spikes" / "embeddings-local" / "questions-v2.yaml"
RESULTS = Path(__file__).resolve().parent / "results"


def _fixed() -> list[dict[str, str]]:
    data = YAML(typ="safe").load(FIXED.read_text(encoding="utf-8"))
    found = next(q for q in data["questions"] if q["id"] == "v03")
    return [{"id": "v03", "question": str(found["question"])}]


def _worker() -> str:
    out = subprocess.run(
        ["docker", "ps", "-q", "--filter", "label=com.docker.compose.service=worker"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    if not out:
        raise SystemExit("worker is not running (reader up)")
    return out[0]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lesson-ordinal", type=int, default=2)  # L02 of the Bashir pilot
    args = parser.parse_args(argv)
    spec = {"lesson_ordinal": args.lesson_ordinal, "fixed": _fixed()}
    run = subprocess.run(
        ["docker", "exec", "-i", _worker(), "python", "-I", "-m", "reader.tutor.bench"],
        input=json.dumps(spec),
        capture_output=True,
        text=True,
        check=False,
        timeout=60 * 60,
    )
    if run.returncode != 0:
        sys.stderr.write(run.stderr[-2000:])
        return run.returncode
    report = json.loads(run.stdout)
    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + ".json")
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(json.dumps(report["summary"]) + f"\nwritten: {path.relative_to(REPO)}\n")
    return 0 if all(report["summary"]["pass"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
