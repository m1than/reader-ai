"""Generator of reader-mlflow's model catalog (SRV-OBS FR-OBS-12 c): every model id of pricing.yaml and of the pinned
MLflow image's bundled Anthropic catalog (mlflow-bundled-anthropic.txt), every price 0, so MLflow never prices the
CLI's spans and the ledger's cost on `agent.call` is the only cost in a trace.

    uv run --project server python deploy/otel/mlflow_catalog.py --write   # regenerate the file
    uv run --project server python deploy/otel/mlflow_catalog.py --check   # exit 1 on any difference
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Final

from ruamel.yaml import YAML

HERE: Final = Path(__file__).resolve().parent
PRICING: Final = HERE.parent.parent / "server" / "src" / "reader" / "agent" / "pricing.yaml"
BUNDLED: Final = HERE / "mlflow-bundled-anthropic.txt"
OUTPUT: Final = HERE / "mlflow-catalog" / "anthropic.json"
ZERO: Final = {
    "input_per_million_tokens": 0.0,
    "output_per_million_tokens": 0.0,
    "cache_read_per_million_tokens": 0.0,
    "cache_write_per_million_tokens": 0.0,
    "cache_write_1hr_per_million_tokens": 0.0,
}


def model_ids() -> list[str]:
    priced = YAML(typ="safe").load(PRICING.read_text(encoding="utf-8"))["models"]
    lines = (line.strip() for line in BUNDLED.read_text(encoding="utf-8").splitlines())
    bundled = {line for line in lines if line and not line.startswith("#")}
    return sorted(set(map(str, priced)) | bundled)


def render() -> str:
    models = {name: {"mode": "chat", "pricing": dict(ZERO)} for name in model_ids()}
    return json.dumps({"schema_version": "1.0", "models": models}, indent=1) + "\n"


def main(argv: list[str]) -> int:
    if argv == ["--write"]:
        OUTPUT.parent.mkdir(exist_ok=True)
        OUTPUT.write_text(render(), encoding="utf-8")
        return 0
    if argv == ["--check"]:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != render():
            print(f"{OUTPUT} is stale: run deploy/otel/mlflow_catalog.py --write", file=sys.stderr)
            return 1
        return 0
    print("usage: mlflow_catalog.py --write | --check", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
