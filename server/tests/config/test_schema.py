"""JSON Schema export and its committed copy.

@trace FR-CONFIG-4
"""

import json
import subprocess
import sys
from pathlib import Path

COMMITTED = Path(__file__).resolve().parents[2] / "src" / "reader" / "config" / "reader.schema.json"


def test_committed_schema_equals_the_generated_one() -> None:
    from reader.config import export_json_schema

    assert COMMITTED.is_file()
    assert json.loads(COMMITTED.read_text(encoding="utf-8")) == export_json_schema()


def test_schema_module_prints_the_schema() -> None:
    from reader.config import export_json_schema

    out = subprocess.run(
        [sys.executable, "-m", "reader.config.schema"], capture_output=True, text=True, check=True
    ).stdout
    assert json.loads(out) == export_json_schema()
