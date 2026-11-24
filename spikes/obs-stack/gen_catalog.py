"""Spike round 5 (never shipped): the zero-price MLflow model catalog of SRV-OBS FR-OBS-12 (c), B1 of the design review.
Run inside the pinned MLflow image (`python -I`), so it reads that image's bundled `anthropic.json`:
    python -I gen_catalog.py OUT_DIR MODEL_ID...
writes OUT_DIR/anthropic.json listing every bundled Anthropic model plus the given ids (pricing.yaml's), every price 0,
and prints the bundled ids (one per line) so the shipped generator's committed list can be compared with the image."""

import json
import pathlib
import sys

BUNDLED = pathlib.Path("/usr/local/lib/python3.11/site-packages/mlflow/utils/model_catalog/anthropic.json")
ZERO = {"input_per_million_tokens": 0.0, "output_per_million_tokens": 0.0, "cache_read_per_million_tokens": 0.0,
        "cache_write_per_million_tokens": 0.0, "cache_write_1hr_per_million_tokens": 0.0}


def main() -> None:
    out, extra = pathlib.Path(sys.argv[1]), sys.argv[2:]
    bundled = sorted(json.loads(BUNDLED.read_text("utf-8"))["models"])
    models = {m: {"mode": "chat", "pricing": dict(ZERO)} for m in sorted(set(bundled) | set(extra))}
    (out / "anthropic.json").write_text(json.dumps({"schema_version": "1.0", "models": models}, indent=1) + "\n")
    print("\n".join(bundled))


if __name__ == "__main__":
    main()
