"""JSON Schema generator for reader.yaml (FR-CONFIG-4).

    uv run python -m reader.config.schema           # print the schema
    uv run python -m reader.config.schema --write   # write src/reader/config/reader.schema.json
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Final

from reader.config.model import export_json_schema

SCHEMA_FILE: Final = Path(__file__).with_name("reader.schema.json")


def render() -> str:
    return json.dumps(export_json_schema(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export the reader.yaml JSON Schema.")
    parser.add_argument("--write", action="store_true", help=f"write {SCHEMA_FILE.name} next to this module")
    args = parser.parse_args(argv)
    if args.write:
        SCHEMA_FILE.write_text(render(), encoding="utf-8")
    else:
        sys.stdout.write(render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
